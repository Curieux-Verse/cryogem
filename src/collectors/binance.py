"""
# WHY: ------------------------------------------------------------------------
# Binance is the primary perp universe: the widest altcoin coverage, free and
# keyless public endpoints, and the venue whose data the case studies came from.
#
# Four implementation traps live in this file. Each one costs a day if missed,
# and three of them fail SILENTLY, which is worse than crashing:
#
#   1. The `1000` prefix. 1000PEPEUSDT prices a thousand tokens. Look up
#      "1000PEPE" on CoinGecko and you get nothing -- which reads downstream as
#      market cap 0, not as an error. Handled in src/symbols.py.
#   2. Funding interval is per-symbol. Settlement is no longer uniformly 8h;
#      it varies (8h/4h/1h). `rate * 3 * 365` mis-ranks the entire universe.
#      Derived here from consecutive fundingTime deltas, stored per symbol.
#   3. Orphan perps. Some perps have no Binance spot pair. The ratio must be
#      float('inf'), never None -- None silently passes a "> 40" filter, and a
#      perp whose hedge lives on another exchange is materially worse.
#   4. Delisted symbols vanish from exchangeInfo. That is why the universe is
#      snapshotted daily and never overwritten: today's symbol list applied to
#      last year bakes in survivorship bias, and the vanished symbols are
#      exactly the ones the screener would have flagged.
#
# Endpoint note: openInterest is ONE SYMBOL PER CALL and is the bottleneck for
# the whole collection cycle. It runs on a bounded pool, size from config.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime
from typing import Any

from src.collectors.base import BaseCollector
from src.db.connection import get_db
from src.db.writes import upsert
from src.symbols import funding_apr, parse_symbol
from src.timeutil import format_day, format_instant, from_millis, utc_now, utc_now_iso


def _f(value: Any) -> float | None:
    """Parse a numeric field that arrives as a string. None on anything unusable.

    Binance returns numbers as JSON strings throughout. A bad value returns None
    rather than 0.0 -- a zero would be indistinguishable from a real zero and
    would quietly satisfy or violate a threshold.
    """
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None  # NaN check


def derive_funding_interval_hours(funding_history: list[dict]) -> float | None:
    """Infer a symbol's settlement interval from consecutive fundingTime values.

    Returns the MODAL gap in hours, rounded to the nearest whole hour, or None
    when there is not enough history to be sure. None is deliberate: the caller
    must not fall back to 8h, because inventing 8h for a 4h symbol is precisely
    the error this function exists to prevent.
    """
    times = sorted(
        from_millis(row["fundingTime"]) for row in funding_history if row.get("fundingTime")
    )
    if len(times) < 3:
        return None

    gaps_hours = [
        round((later - earlier).total_seconds() / 3600.0)
        for earlier, later in zip(times, times[1:])
        if later > earlier
    ]
    plausible = [g for g in gaps_hours if g in (1, 2, 4, 8)]
    if not plausible:
        return None
    modal, count = Counter(plausible).most_common(1)[0]
    # Require the mode to actually dominate; an even split means the history
    # spans an interval change and we should not pick a side.
    return float(modal) if count >= len(plausible) * 0.6 else None


class BinanceUniverseCollector(BaseCollector):
    """Daily snapshot of every Binance USD-M perpetual, plus funding intervals.

    Writes one row per (day, exchange, symbol). Rows are never deleted, so the
    backtester can reconstruct the universe exactly as it stood on any past
    date -- delistings included.
    """

    name = "binance_universe"
    rate_limit_key = "binance_futures"
    tier = "C"

    async def fetch(self, as_of: datetime) -> dict[str, Any]:
        base = self.config.settings.endpoints["binance_futures"]
        async with self.client(base) as client:
            info = await self.request_json(client, "GET", "/fapi/v1/exchangeInfo")
            symbols = [
                s
                for s in info.get("symbols", [])
                if s.get("contractType") == "PERPETUAL"
                and s.get("quoteAsset") == self.config.settings.universe.quote_asset
            ]
            if not symbols:
                raise RuntimeError(
                    "exchangeInfo returned no USDT perpetuals. The endpoint shape "
                    "has probably changed -- do not write an empty universe."
                )
            intervals = await self._fetch_funding_intervals(client, symbols)
        return {"symbols": symbols, "intervals": intervals}

    async def _fetch_funding_intervals(
        self, client: Any, symbols: list[dict]
    ) -> dict[str, float | None]:
        """Derive each symbol's settlement interval from its funding history.

        Refreshed with the daily universe rather than every cycle: intervals
        change rarely, and this is one request per symbol.
        """
        semaphore = asyncio.Semaphore(self.config.settings.http.openinterest_concurrency)

        async def one(symbol: str) -> tuple[str, float | None]:
            async with semaphore:
                try:
                    history = await self.request_json(
                        client,
                        "GET",
                        "/fapi/v1/fundingRate",
                        params={"symbol": symbol, "limit": 8},
                    )
                    return symbol, derive_funding_interval_hours(history)
                except Exception as exc:  # noqa: BLE001
                    self.warn(
                        "funding_interval_unavailable", symbol=symbol, error=str(exc)[:120]
                    )
                    return symbol, None

        results = await asyncio.gather(*(one(s["symbol"]) for s in symbols))
        return dict(results)

    def transform(self, raw: dict[str, Any], as_of: datetime) -> list[dict[str, Any]]:
        fetched_at = utc_now_iso()  # ACTUAL run time, never a nominal cron slot
        snapshot_date = format_day(as_of)
        rows: list[dict[str, Any]] = []

        for entry in raw["symbols"]:
            try:
                parsed = parse_symbol(entry["symbol"], entry.get("quoteAsset"))
            except ValueError as exc:
                self.warn("unparseable_symbol", symbol=entry.get("symbol"), error=str(exc))
                continue

            onboard = entry.get("onboardDate")
            rows.append(
                {
                    "snapshot_date": snapshot_date,
                    "exchange": "binance",
                    "symbol": parsed.symbol,
                    "base_asset": parsed.base_asset,
                    "quote_asset": parsed.quote_asset,
                    "contract_type": entry.get("contractType"),
                    "onboard_date": format_instant(from_millis(onboard)) if onboard else None,
                    "status": entry.get("status"),
                    "price_multiplier": parsed.price_multiplier,
                    "funding_interval_hours": raw["intervals"].get(parsed.symbol),
                    "fetched_at_utc": fetched_at,
                }
            )
        return rows

    def write(self, rows: list[dict[str, Any]]) -> int:
        with get_db() as db:
            return upsert(db, "universe_snapshot", rows)


class BinanceDerivativesCollector(BaseCollector):
    """Mark/index price, funding, open interest and 24h volume for every perp.

    Tier B (hourly on Actions) or Tier A (5-minute on a host). This is the data
    that cannot be reconstructed later: Binance keeps only ~30 days of OI
    history, so a cycle not recorded is a cycle permanently lost.
    """

    name = "binance_derivatives"
    rate_limit_key = "binance_futures"
    tier = "B"

    async def fetch(self, as_of: datetime) -> dict[str, Any]:
        base = self.config.settings.endpoints["binance_futures"]
        symbols, intervals = self._load_universe()
        if not symbols:
            raise RuntimeError(
                "no universe rows found. Run `collect binance_universe` first: "
                "derivatives rows must be attributable to a known symbol list."
            )

        async with self.client(base) as client:
            # Two all-symbol calls, then the per-symbol bottleneck.
            premium = await self.request_json(client, "GET", "/fapi/v1/premiumIndex")
            tickers = await self.request_json(client, "GET", "/fapi/v1/ticker/24hr")
            open_interest = await self._fetch_open_interest(client, symbols)

        return {
            "premium": premium,
            "tickers": tickers,
            "open_interest": open_interest,
            "intervals": intervals,
            "symbols": set(symbols),
        }

    def _load_universe(self) -> tuple[list[str], dict[str, float | None]]:
        """Most recent universe snapshot: which symbols to poll, and their intervals."""
        with get_db() as db:
            latest = db.scalar(
                "SELECT MAX(snapshot_date) FROM universe_snapshot WHERE exchange='binance'"
            )
            if not latest:
                return [], {}
            rows = db.query(
                "SELECT symbol, funding_interval_hours FROM universe_snapshot "
                "WHERE exchange='binance' AND snapshot_date=? AND status='TRADING'",
                (latest,),
            )
        return (
            [r["symbol"] for r in rows],
            {r["symbol"]: r["funding_interval_hours"] for r in rows},
        )

    async def _fetch_open_interest(self, client: Any, symbols: list[str]) -> dict[str, float]:
        """One call per symbol -- the bottleneck. Bounded concurrency from config.

        OI is returned IN CONTRACTS. That is the field to rank on. USD OI is
        contracts x price, so it rises during a rally with zero new positioning;
        ranking on it would flag every asset that simply went up.
        """
        semaphore = asyncio.Semaphore(self.config.settings.http.openinterest_concurrency)
        results: dict[str, float] = {}

        async def one(symbol: str) -> None:
            async with semaphore:
                try:
                    payload = await self.request_json(
                        client, "GET", "/fapi/v1/openInterest", params={"symbol": symbol}
                    )
                    value = _f(payload.get("openInterest"))
                    if value is not None:
                        results[symbol] = value
                except Exception as exc:  # noqa: BLE001
                    self.warn("open_interest_unavailable", symbol=symbol, error=str(exc)[:120])

        await asyncio.gather(*(one(s) for s in symbols))
        return results

    def transform(self, raw: dict[str, Any], as_of: datetime) -> list[dict[str, Any]]:
        fetched_at = utc_now_iso()
        ts = format_instant(as_of)
        wanted: set[str] = raw["symbols"]
        volumes = {
            t["symbol"]: _f(t.get("quoteVolume"))
            for t in raw["tickers"]
            if t.get("symbol") in wanted
        }

        rows: list[dict[str, Any]] = []
        for entry in raw["premium"]:
            symbol = entry.get("symbol")
            if symbol not in wanted:
                continue
            try:
                parsed = parse_symbol(symbol, self.config.settings.universe.quote_asset)
            except ValueError:
                continue

            mark = _f(entry.get("markPrice"))
            index = _f(entry.get("indexPrice"))
            rate = _f(entry.get("lastFundingRate"))
            interval = raw["intervals"].get(symbol)
            oi_contracts = raw["open_interest"].get(symbol)
            next_funding = entry.get("nextFundingTime")

            # Only annualise when the interval is KNOWN. An unknown interval
            # yields a null APR, which the screener treats as missing data --
            # never as 8h.
            apr = None
            if rate is not None and interval:
                apr = funding_apr(rate, interval)
            elif rate is not None and not interval:
                self.warn("funding_interval_unknown", symbol=symbol)

            rows.append(
                {
                    "ts_utc": ts,
                    "exchange": "binance",
                    "symbol": symbol,
                    "base_asset": parsed.base_asset,
                    "mark_price": mark,
                    "index_price": index,
                    "open_interest_base": oi_contracts,
                    "open_interest_usd": (
                        oi_contracts * mark if oi_contracts is not None and mark else None
                    ),
                    "funding_rate": rate,
                    "funding_interval_hours": interval,
                    "funding_apr": apr,
                    "next_funding_ts": (
                        format_instant(from_millis(next_funding)) if next_funding else None
                    ),
                    "premium": ((mark - index) / index) if mark and index else None,
                    "volume_24h_usd": volumes.get(symbol),
                    "fetched_at_utc": fetched_at,
                }
            )
        return rows

    def write(self, rows: list[dict[str, Any]]) -> int:
        with get_db() as db:
            return upsert(db, "derivatives_snapshot", rows)


class BinanceSpotCollector(BaseCollector):
    """Daily spot 24h volume for assets that have a Binance spot pair.

    Two jobs, and the second matters more than the first:
      1. Supply the denominator of the perp/spot volume ratio (L1 check 4).
      2. Establish EXISTENCE. An asset absent from this table has no Binance
         spot market. That is the orphan-perp condition, and the screener turns
         it into a ratio of infinity rather than a null.
    """

    name = "binance_spot"
    rate_limit_key = "binance_spot"
    tier = "C"

    async def fetch(self, as_of: datetime) -> list[dict]:
        base = self.config.settings.endpoints["binance_spot"]
        async with self.client(base) as client:
            return await self.request_json(client, "GET", "/api/v3/ticker/24hr")

    def transform(self, raw: list[dict], as_of: datetime) -> list[dict[str, Any]]:
        fetched_at = utc_now_iso()
        snapshot_date = format_day(as_of)
        quote = self.config.settings.universe.quote_asset
        rows: list[dict[str, Any]] = []

        for entry in raw:
            symbol = entry.get("symbol", "")
            if not symbol.endswith(quote):
                continue
            try:
                parsed = parse_symbol(symbol, quote)
            except ValueError:
                continue
            rows.append(
                {
                    "snapshot_date": snapshot_date,
                    "exchange": "binance",
                    "symbol": symbol,
                    "base_asset": parsed.base_asset,
                    "price_usd": _f(entry.get("lastPrice")),
                    "volume_24h_usd": _f(entry.get("quoteVolume")),
                    "price_change_24h_pct": _f(entry.get("priceChangePercent")),
                    "fetched_at_utc": fetched_at,
                }
            )
        return rows

    def write(self, rows: list[dict[str, Any]]) -> int:
        with get_db() as db:
            return upsert(db, "spot_snapshot", rows)


class BinanceDepthCollector(BaseCollector):
    """Order-book depth for a shortlist of assets.

    Depth is the only honest answer to "how big a position can I actually
    exit?". Printed price is not that answer. Collected for L1 survivors only,
    because it is one call per symbol and the full universe would be wasteful.
    """

    name = "binance_depth"
    rate_limit_key = "binance_spot"
    tier = "B"

    #: Depth bands in percent from mid. Matches depth_snapshot's columns.
    BANDS = (0.005, 0.01, 0.02)

    def __init__(self, symbols: list[str] | None = None) -> None:
        super().__init__()
        self._symbols = symbols

    async def fetch(self, as_of: datetime) -> dict[str, Any]:
        symbols = self._symbols or self._survivor_symbols()
        if not symbols:
            self.warn("no_survivors_for_depth", note="run the screen first")
            return {}

        base = self.config.settings.endpoints["binance_spot"]
        semaphore = asyncio.Semaphore(self.config.settings.http.openinterest_concurrency)
        books: dict[str, Any] = {}

        async with self.client(base) as client:

            async def one(symbol: str) -> None:
                async with semaphore:
                    try:
                        books[symbol] = await self.request_json(
                            client,
                            "GET",
                            "/api/v3/depth",
                            params={"symbol": symbol, "limit": 500},
                        )
                    except Exception as exc:  # noqa: BLE001
                        self.warn("depth_unavailable", symbol=symbol, error=str(exc)[:120])

            await asyncio.gather(*(one(s) for s in symbols))
        return books

    def _survivor_symbols(self) -> list[str]:
        """Symbols that passed L1 most recently. Falls back to nothing, not to all."""
        with get_db() as db:
            run_date = db.scalar("SELECT MAX(run_date) FROM layer1_result")
            if not run_date:
                return []
            rows = db.query(
                "SELECT DISTINCT s.symbol FROM layer1_result l "
                "JOIN spot_snapshot s ON s.base_asset = l.base_asset "
                "WHERE l.run_date = ? AND l.passed = 1 "
                "AND s.snapshot_date = (SELECT MAX(snapshot_date) FROM spot_snapshot)",
                (run_date,),
            )
        return [r["symbol"] for r in rows]

    def transform(self, raw: dict[str, Any], as_of: datetime) -> list[dict[str, Any]]:
        fetched_at = utc_now_iso()
        ts = format_instant(as_of)
        rows: list[dict[str, Any]] = []

        for symbol, book in raw.items():
            bids = [(float(p), float(q)) for p, q in book.get("bids", [])]
            asks = [(float(p), float(q)) for p, q in book.get("asks", [])]
            if not bids or not asks:
                self.warn("empty_order_book", symbol=symbol)
                continue
            mid = (bids[0][0] + asks[0][0]) / 2.0

            row: dict[str, Any] = {
                "ts_utc": ts,
                "exchange": "binance",
                "symbol": symbol,
                "market_type": "spot",
                "fetched_at_utc": fetched_at,
            }
            for band in self.BANDS:
                # One decimal always: 0.5 -> "0p5", 1.0 -> "1p0". A "%g"
                # format yields "1", which would target a column that does
                # not exist (bid_depth_1 vs bid_depth_1p0) and fail at write.
                label = f"{band * 100:.1f}".replace(".", "p")
                row[f"bid_depth_{label}"] = sum(
                    price * qty for price, qty in bids if price >= mid * (1 - band)
                )
                row[f"ask_depth_{label}"] = sum(
                    price * qty for price, qty in asks if price <= mid * (1 + band)
                )
            rows.append(row)
        return rows

    def write(self, rows: list[dict[str, Any]]) -> int:
        with get_db() as db:
            return upsert(db, "depth_snapshot", rows)


__all__ = [
    "BinanceDepthCollector",
    "BinanceDerivativesCollector",
    "BinanceSpotCollector",
    "BinanceUniverseCollector",
    "derive_funding_interval_hours",
]
