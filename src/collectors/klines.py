"""
# WHY: ------------------------------------------------------------------------
# Historical OHLC bars. Layer 3's only input, and the journal's only honest
# source of excursions.
#
# Two things in this system cannot be computed from a snapshot:
#
#   1. STRUCTURE. A descending trendline needs swing highs across months, and a
#      break has to be confirmed on a CLOSE. Neither exists in a table of
#      today's prices.
#
#   2. EXCURSIONS. The journal asks whether a +20% return first drew down 40%,
#      which needs the HIGH and LOW of every day in the window. The CoinGecko
#      snapshot carries a close and nothing else, so before this collector
#      existed every excursion silently degraded to a close-to-close range --
#      understating the drawdown that decides whether a trade was survivable.
#
# The design choice that matters here is that this collector BACKFILLS. Every
# other collector in the system can only ever record the present, because the
# APIs it reads do not serve history: a day not collected is a day lost
# forever. Binance klines are the exception -- they go back years -- so this is
# the one collector that can make the dataset older than the project. Structure
# detection and the first backtest are both gated on that.
#
# Weight discipline: Binance futures klines cost weight by `limit`
# (<=100 -> 1, <=500 -> 2, <=1000 -> 5, >1000 -> 10) against a 2400/min budget.
# A 528-symbol backfill at limit 1500 is 5,280 weight and would be rejected
# partway through, so the backfill paginates in weight-cheap pages and the
# daily run asks only for the handful of bars it is missing.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timezone
from typing import Any

from src.collectors.base import BaseCollector
from src.db.connection import get_db
from src.db.writes import upsert
from src.symbols import parse_symbol
from src.timeutil import (
    ISO_DAY,
    add_days,
    format_day,
    from_millis,
    millis_from,
    parse_day,
    utc_now_iso,
)

#: Bars per request. 500 keeps futures klines at weight 2 rather than 10, so a
#: full-universe pass fits inside one minute's budget with room to spare.
PAGE_LIMIT = 500

#: Daily bars kept. ~3 years is enough for a weekly trendline with several
#: touches, which is the longest lookback any Layer 3 rule needs.
BACKFILL_DAYS = 1100

#: What a routine daily run fetches. Small, and deliberately overlapping: the
#: last bar is re-fetched because the previous run may have caught it mid-day,
#: and an upsert makes replacing it free.
INCREMENTAL_DAYS = 7

#: Binance kline array positions. Named because a positional array is exactly
#: where a silent column shift attaches one asset's high to another's low.
OPEN_TIME, OPEN, HIGH, LOW, CLOSE, VOLUME = 0, 1, 2, 3, 4, 5
QUOTE_VOLUME = 7


def _scaled(value: float | None, scale: float) -> float | None:
    return None if value is None else value / scale


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


class BinanceKlinesCollector(BaseCollector):
    """Daily OHLC per universe symbol, from Binance futures klines.

    Idempotent by construction: the primary key is (snapshot_date, base_asset)
    and a re-fetched bar simply replaces itself. That is what makes a backfill
    safe to interrupt and re-run, which matters because a full-universe
    backfill is the longest-running job in the project.
    """

    name = "binance_klines"
    rate_limit_key = "binance_futures"
    tier = "C"

    def __init__(self, *args: Any, backfill: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        #: Backfill mode reaches back BACKFILL_DAYS instead of INCREMENTAL_DAYS.
        #: Off by default: a daily job that re-downloads three years of history
        #: every morning is a daily job that gets rate-limited into failure.
        self.backfill = backfill

    # -- fetch ---------------------------------------------------------------
    async def fetch(self, as_of: datetime) -> dict[str, list[list]]:
        base = self.config.settings.endpoints["binance_futures"]
        symbols = self._symbols_to_fetch()
        if not symbols:
            self.log.warning(
                "klines_no_symbols",
                effect="nothing to fetch; run the universe collector first",
            )
            return {}

        days = BACKFILL_DAYS if self.backfill else INCREMENTAL_DAYS
        start_day = add_days(format_day(as_of), -days)
        self.log.info(
            "klines_fetch_start",
            symbols=len(symbols),
            days=days,
            mode="backfill" if self.backfill else "incremental",
        )

        out: dict[str, list[list]] = {}
        concurrency = self.config.settings.http.openinterest_concurrency
        semaphore = asyncio.Semaphore(concurrency)

        async with self.client(base) as client:

            async def one(symbol: str) -> None:
                async with semaphore:
                    try:
                        out[symbol] = await self._fetch_symbol(client, symbol, start_day)
                    except Exception as exc:  # noqa: BLE001
                        # One symbol's history is not worth the whole run. A
                        # delisted or newly-listed contract legitimately 400s.
                        self.log.warning(
                            "klines_symbol_failed",
                            symbol=symbol,
                            error_type=type(exc).__name__,
                        )

            await asyncio.gather(*(one(s) for s in symbols))
        return out

    async def _fetch_symbol(self, client: Any, symbol: str, start_day: str) -> list[list]:
        """Page forward from start_day until Binance stops returning new bars.

        Paging forward rather than backward keeps `startTime` authoritative: a
        request that asks for the newest 1500 bars silently returns fewer for a
        young contract, and the caller cannot tell that from a truncated page.
        """
        # UTC explicitly. `datetime.strptime(...).timestamp()` interprets a
        # naive datetime in the MACHINE's timezone, so the same backfill would
        # start on a different bar depending on where it ran -- and the CI
        # runner is UTC while the developer's laptop is not.
        start_ms = millis_from(
            datetime.combine(parse_day(start_day), time(0, 0), tzinfo=timezone.utc)
        )
        bars: list[list] = []
        cursor = start_ms

        while True:
            page = await self.request_json(
                client,
                "GET",
                "/fapi/v1/klines",
                params={
                    "symbol": symbol,
                    "interval": "1d",
                    "startTime": cursor,
                    "limit": PAGE_LIMIT,
                },
            )
            if not page:
                break
            bars.extend(page)
            if len(page) < PAGE_LIMIT:
                break
            # +1ms so the last bar is not returned twice, which would otherwise
            # loop forever on a symbol with exactly PAGE_LIMIT bars remaining.
            cursor = int(page[-1][OPEN_TIME]) + 1
        return bars

    def _symbols_to_fetch(self) -> list[str]:
        """Tradable perp symbols from the most recent universe snapshot.

        SETTLING and PENDING_TRADING contracts are excluded from polling but
        remain in the snapshot, because a delisted symbol disappearing from the
        universe is exactly the record that prevents survivorship bias later.
        """
        with get_db() as db:
            snapshot = db.scalar("SELECT MAX(snapshot_date) FROM universe_snapshot")
            if not snapshot:
                return []
            return [
                r["symbol"]
                for r in db.query(
                    "SELECT symbol FROM universe_snapshot "
                    "WHERE snapshot_date = ? AND exchange = 'binance' AND status = 'TRADING' "
                    "ORDER BY symbol",
                    (snapshot,),
                )
            ]

    # -- transform -----------------------------------------------------------
    def transform(self, raw: dict[str, list[list]], as_of: datetime) -> list[dict[str, Any]]:
        fetched_at = utc_now_iso()
        today = format_day(as_of)
        quote = self.config.settings.universe.quote_asset
        rows: list[dict[str, Any]] = []
        skipped_partial = 0

        for symbol, bars in raw.items():
            try:
                parsed = parse_symbol(symbol, quote)
            except ValueError:
                continue

            for bar in bars:
                if len(bar) <= QUOTE_VOLUME:
                    # A short array means the response shape changed. Attaching
                    # positional fields to a shape we did not verify is how one
                    # asset's high becomes another's low.
                    self.log.warning(
                        "klines_unexpected_bar_shape", symbol=symbol, fields=len(bar)
                    )
                    continue

                day = from_millis(int(bar[OPEN_TIME])).strftime(ISO_DAY)
                if day >= today:
                    # Today's bar is still forming. Writing it would record a
                    # partial high and low, and the journal would later read
                    # that as the day's true excursion.
                    skipped_partial += 1
                    continue

                close = _f(bar[CLOSE])
                if close is None:
                    continue
                # DE-MULTIPLY. 1000PEPEUSDT quotes a THOUSAND PEPE, and
                # parse_symbol normalises its base_asset to PEPE -- so the raw
                # close lands in price_daily under PEPE at 1000x the real
                # price, beside a CoinGecko row for the same asset at 1x.
                #
                # Returns computed inside one source survive that (the scale
                # cancels), which is what makes it dangerous: it looks
                # harmless. But price_daily is keyed on (date, base_asset) and
                # CoinGecko also writes it, so a single day where klines is
                # missing and CoinGecko is not produces a 1000x step between
                # consecutive rows -- a fabricated +99,900% forward return, in
                # an append-only table, on a real journal entry.
                scale = float(parsed.price_multiplier or 1)
                rows.append(
                    {
                        "snapshot_date": day,
                        "base_asset": parsed.base_asset,
                        "open_usd": _scaled(_f(bar[OPEN]), scale),
                        "high_usd": _scaled(_f(bar[HIGH]), scale),
                        "low_usd": _scaled(_f(bar[LOW]), scale),
                        "close_usd": close / scale,
                        # Quote volume is already USD notional and is NOT
                        # per-token, so it must not be divided.
                        "volume_usd": _f(bar[QUOTE_VOLUME]),
                        "source": "binance_klines",
                        "fetched_at_utc": fetched_at,
                    }
                )

        if skipped_partial:
            self.log.info(
                "klines_partial_bars_skipped",
                bars=skipped_partial,
                reason="today's bar is still forming; a partial high/low would "
                "be read later as the day's true excursion",
            )

        # Both contracts now report the same per-token price, so a duplicate
        # is a genuine ambiguity rather than a scale error. Keep one.
        return self._resolve_multiplier_conflicts(rows)

    def _resolve_multiplier_conflicts(
        self, rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        by_key: dict[tuple[str, str], dict[str, Any]] = {}
        conflicts = 0
        for row in rows:
            key = (row["snapshot_date"], row["base_asset"])
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = row
                continue
            conflicts += 1
            # Both are de-multiplied, so prefer the deeper market: more quote
            # volume means the bar reflects more real trading.
            if (row["volume_usd"] or 0) > (existing["volume_usd"] or 0):
                by_key[key] = row
        if conflicts:
            self.log.info(
                "klines_multiplier_conflicts_resolved",
                bars=conflicts,
                rule="kept the unmultiplied contract's price",
            )
        return list(by_key.values())

    # -- write ---------------------------------------------------------------
    def write(self, rows: list[dict[str, Any]]) -> int:
        with get_db() as db:
            return upsert(db, "price_daily", rows)


__all__ = ["BACKFILL_DAYS", "INCREMENTAL_DAYS", "BinanceKlinesCollector"]
