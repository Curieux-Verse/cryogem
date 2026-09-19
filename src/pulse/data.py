"""
# WHY: ------------------------------------------------------------------------
# Pulse's market data (D-078): the hourly fetch-and-store stage that produces a
# MarketWindow (src/pulse/contract.py) for Layer 1 survivors plus the benchmark.
#
# Three endpoints per asset, all on the website host (D-070):
#
#   /fapi/v1/klines              1H bars, weight 2 at limit <= 499
#   /futures/data/openInterestHist  1H open interest; own IP limit (1000/5min)
#   /fapi/v1/fundingRate         settled funding; own IP limit (500/5min)
#
# The LOOK-AHEAD RULE is enforced here, on the way in, for every `as_of`:
#
#   * klines are requested with endTime = as_of - 1ms. Binance filters klines on
#     OPEN time, so this excludes the bar that opens at as_of -- and a bar that
#     is still forming is dropped again in the parser (open + 1h > as_of),
#     because a partial high/low/volume read as a closed bar is the one error a
#     backtest can never see.
#   * openInterestHist period=1h: a row stamped T IS the open interest at the
#     instant T. Verified 2026-09-19 against period=5m: the 1h row at T equals
#     the 5m row at T, on BTCUSDT and ONTUSDT, for four consecutive hours. Rows
#     are kept only when T <= as_of.
#   * fundingRate: settlement stamps carry a few ms of jitter (08:00:00.002).
#     Binance's own endTime filter treats 08:00:00.002 as 08:00, so the parser
#     floors each stamp to the whole second and keeps it when <= as_of.
#
# 1000-prefixed contracts are de-multiplied exactly as klines.py does for
# price_daily: prices are divided by the contract multiplier (per token), OI in
# contracts is multiplied back to tokens, and quote-asset (USDT) amounts are not
# touched -- they are already notional. So oi_contracts * close ~= oi_usd holds
# for 1000PEPE exactly as it does for BTC.
#
# STORAGE. Turso meters rows read AND written, so the hourly write is
# incremental against series_cursor (one read of every cursor, one query):
# only bars/OI newer than the asset's cursor are written. An asset with no
# cursor, or whose contract symbol changed, gets the whole fetched window --
# which is the ~21-day backfill, for free, on first sight. Steady state is one
# bar row, one OI row and two cursor rows per asset per hour. Funding is not
# stored: derivatives_snapshot already records it hourly.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from src.collectors.base import BaseCollector, CollectorRunResult, IPBannedError
from src.collectors.klines import check_failure_share
from src.db.connection import get_db
from src.db.writes import upsert
from src.pulse.contract import BAR_1H, BAR_COLUMNS, OI_COLUMNS, MarketWindow
from src.timeutil import format_day, format_instant, millis_from, parse_instant, utc_now_iso

#: Binance kline array positions, verified against a live BTCUSDT 1h response
#: on 2026-09-19. Named because a positional array is exactly where a silent
#: column shift attaches one field to another.
K_OPEN_TIME = 0
K_OPEN = 1
K_HIGH = 2
K_LOW = 3
K_CLOSE = 4
K_VOLUME = 5
K_CLOSE_TIME = 6
K_QUOTE_VOLUME = 7
K_TRADES = 8
K_TAKER_BUY_BASE = 9
K_TAKER_BUY_QUOTE = 10
#: A bar shorter than this is a changed response shape, never parsed.
KLINE_MIN_FIELDS = K_TAKER_BUY_QUOTE + 1

HOUR_MS = 3_600_000

#: series_cursor.series values.
SERIES_BARS = "bar_1h"
SERIES_OI = "oi_1h"

#: settings.rate_limits keys for the two separately metered endpoint families.
LIMITER_FUTURES_DATA = "binance_futures_data"
LIMITER_FUNDING = "binance_funding"

KLINES_PATH = "/fapi/v1/klines"
OI_HIST_PATH = "/futures/data/openInterestHist"
FUNDING_PATH = "/fapi/v1/fundingRate"

#: Marker on each transform() row saying which table it belongs to.
_TABLE = "_table"


# -- time ----------------------------------------------------------------------
def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def hour_floor(dt: datetime) -> datetime:
    """`dt` in UTC with minutes, seconds and microseconds zeroed. Naive = UTC."""
    return _utc(dt).replace(minute=0, second=0, microsecond=0)


def _ts(dt: datetime) -> pd.Timestamp:
    return pd.Timestamp(_utc(dt))


# -- parsing (pure) -------------------------------------------------------------
def _num(value: Any) -> float | None:
    """A Binance numeric string as float. None on anything unusable, never 0."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and parsed not in (float("inf"), float("-inf")) else None


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            as_float = float(value)
        except (TypeError, ValueError):
            return None
        return int(as_float) if as_float.is_integer() else None


def _empty_bars() -> pd.DataFrame:
    frame = pd.DataFrame(
        {c: pd.Series(dtype="int64" if c == "trades" else "float64") for c in BAR_COLUMNS},
        index=pd.DatetimeIndex([], tz="UTC", name="ts_open_utc"),
    )
    return frame


def _empty_oi() -> pd.DataFrame:
    return pd.DataFrame(
        {c: pd.Series(dtype="float64") for c in OI_COLUMNS},
        index=pd.DatetimeIndex([], tz="UTC", name="ts_utc"),
    )


def _parse_klines(
    payload: Any, as_of: datetime, price_multiplier: float = 1
) -> tuple[pd.DataFrame, int]:
    """(closed bars, rows refused as malformed). See bars_from_klines."""
    cutoff = _ts(as_of)
    scale = float(price_multiplier or 1)
    records: list[dict[str, Any]] = []
    index: list[pd.Timestamp] = []
    malformed = 0

    for row in payload or []:
        if not isinstance(row, (list, tuple)) or len(row) < KLINE_MIN_FIELDS:
            malformed += 1
            continue
        open_ms = _int(row[K_OPEN_TIME])
        close_ms = _int(row[K_CLOSE_TIME])
        # A 1H bar opens on the hour and closes 1ms before the next. Anything
        # else is another interval or a changed shape, not a bar to trust.
        if (
            open_ms is None
            or close_ms is None
            or open_ms % HOUR_MS != 0
            or close_ms != open_ms + HOUR_MS - 1
        ):
            malformed += 1
            continue
        opened = pd.Timestamp(open_ms, unit="ms", tz="UTC")
        if opened + BAR_1H > cutoff:
            # Still forming at as_of (or in the future): never a closed bar.
            continue
        values = {
            "open": _num(row[K_OPEN]),
            "high": _num(row[K_HIGH]),
            "low": _num(row[K_LOW]),
            "close": _num(row[K_CLOSE]),
            "quote_volume": _num(row[K_QUOTE_VOLUME]),
            "taker_buy_quote": _num(row[K_TAKER_BUY_QUOTE]),
        }
        trades = _int(row[K_TRADES])
        if trades is None or any(v is None for v in values.values()):
            # A bar with a hole is dropped whole: a NaN taker-buy inside a sum
            # would bias the flow ratio rather than show as missing.
            malformed += 1
            continue
        for key in ("open", "high", "low", "close"):
            values[key] = values[key] / scale  # per token; USDT amounts untouched
        values["trades"] = trades
        records.append(values)
        index.append(opened)

    if not records:
        return _empty_bars(), malformed
    frame = pd.DataFrame(records, index=pd.DatetimeIndex(index, name="ts_open_utc"))
    frame = frame[list(BAR_COLUMNS)].astype(
        {c: ("int64" if c == "trades" else "float64") for c in BAR_COLUMNS}
    )
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    return frame, malformed


def bars_from_klines(
    payload: Any, as_of: datetime, price_multiplier: float = 1
) -> pd.DataFrame:
    """Closed 1H bars from a /fapi/v1/klines payload, as of `as_of`.

    Index: bar OPEN time, tz-aware UTC, sorted, unique. Columns: BAR_COLUMNS,
    float64 with `trades` int64. A bar is kept only when open + 1h <= as_of.
    Prices are divided by `price_multiplier` (1000 for 1000PEPEUSDT); quote
    volume and taker-buy quote are USDT notional and are not.
    """
    return _parse_klines(payload, as_of, price_multiplier)[0]


def _parse_oi(
    payload: Any, as_of: datetime, contract_multiplier: float = 1
) -> tuple[pd.DataFrame, int]:
    cutoff = _ts(as_of)
    scale = float(contract_multiplier or 1)
    records: list[dict[str, float]] = []
    index: list[pd.Timestamp] = []
    malformed = 0

    for row in payload or []:
        if not isinstance(row, dict):
            malformed += 1
            continue
        stamp = _int(row.get("timestamp"))
        contracts = _num(row.get("sumOpenInterest"))
        if stamp is None or contracts is None:
            malformed += 1
            continue
        observed = pd.Timestamp(stamp, unit="ms", tz="UTC").floor("s")
        if observed > cutoff:
            continue
        usd = _num(row.get("sumOpenInterestValue"))
        records.append(
            {
                # Contracts of a 1000-prefixed perp are thousands of tokens.
                "oi_contracts": contracts * scale,
                "oi_usd": float("nan") if usd is None else usd,
            }
        )
        index.append(observed)

    if not records:
        return _empty_oi(), malformed
    frame = pd.DataFrame(records, index=pd.DatetimeIndex(index, name="ts_utc"))
    frame = frame[list(OI_COLUMNS)].astype("float64")
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    return frame, malformed


def oi_from_hist(
    payload: Any, as_of: datetime, contract_multiplier: float = 1
) -> pd.DataFrame:
    """1H open interest from a /futures/data/openInterestHist payload.

    Index: observation instant (Binance `timestamp`, which is the OI AT that
    instant), tz-aware UTC, sorted, unique; only rows with ts <= as_of.
    Columns: OI_COLUMNS. sumOpenInterest -> oi_contracts, multiplied by
    `contract_multiplier` into tokens; sumOpenInterestValue -> oi_usd.
    """
    return _parse_oi(payload, as_of, contract_multiplier)[0]


def funding_from_history(payload: Any, as_of: datetime) -> pd.Series:
    """Settled funding rates from /fapi/v1/fundingRate, indexed by funding time.

    Stamps are floored to the whole second (settlement jitter of a few ms), and
    kept only when <= as_of.
    """
    cutoff = _ts(as_of)
    values: list[float] = []
    index: list[pd.Timestamp] = []
    for row in payload or []:
        if not isinstance(row, dict):
            continue
        stamp = _int(row.get("fundingTime"))
        rate = _num(row.get("fundingRate"))
        if stamp is None or rate is None:
            continue
        settled = pd.Timestamp(stamp, unit="ms", tz="UTC").floor("s")
        if settled > cutoff:
            continue
        values.append(rate)
        index.append(settled)
    series = pd.Series(
        values,
        index=pd.DatetimeIndex(index, tz="UTC", name="funding_time"),
        dtype="float64",
        name="funding_rate",
    )
    return series[~series.index.duplicated(keep="last")].sort_index()


def _one_contract_per_asset(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """base_asset -> universe row, preferring the unmultiplied contract (D-046),
    the same rule as screening.pipeline._one_contract_per_asset."""
    chosen: dict[str, dict[str, Any]] = {}
    for row in sorted(rows, key=lambda r: r["symbol"]):
        current = chosen.get(row["base_asset"])
        if current is None or (row.get("price_multiplier") or 1) < (
            current.get("price_multiplier") or 1
        ):
            chosen[row["base_asset"]] = row
    return chosen


# -- the collector ----------------------------------------------------------------
class BinancePulseCollector(BaseCollector):
    """1H bars, 1H open interest and settled funding for Pulse (D-078).

    Not in any tier: src/pulse/run.py runs it and then reads `window`, which is
    a MarketWindow when the run succeeded or was partial, and None when it
    failed. `collect binance_pulse` runs it by hand.

    Failure rules (D-051, D-052):
      * klines failed for an asset -> the asset is in window.fetch_errors, with
        no bars, OI or funding; the run is partial. Above 20% of fetched assets
        the run is failed.
      * OI or funding failed for an asset -> warn() (partial), and the asset is
        simply absent from window.oi / window.funding. It keeps its bars and is
        NOT in fetch_errors: features score what was measured (D-075).
      * 418 -> IPBannedError stops every request at once; the run is failed.
    """

    name = "binance_pulse"
    rate_limit_key = "binance_futures"
    tier = "B"
    #: Every endpoint serves history and every parse cuts at as_of, so a past
    #: hour (within Binance's 30 days of OI) is recorded honestly.
    accepts_past_as_of = True

    def __init__(self) -> None:
        super().__init__()
        #: Set by a run that did not fail; None otherwise.
        self.window: MarketWindow | None = None

    async def run(self, as_of: datetime | None = None) -> CollectorRunResult:
        self.window = None
        result = await super().run(as_of)
        if not result.ok:
            # A failed run never leaves a half-built window for the orchestrator.
            self.window = None
        elif self.window is not None:
            result.details = {
                "as_of": format_instant(self.window.as_of),
                "survivors": len(self.window.survivors),
                "assets_with_bars": len(self.window.bars),
                "assets_with_oi": len(self.window.oi),
                "fetch_errors": len(self.window.fetch_errors),
            }
        return result

    # -- targets -------------------------------------------------------------
    def _load_targets(
        self, cutoff: datetime
    ) -> tuple[list[str], dict[str, dict[str, Any]], str]:
        """(survivors, base_asset -> universe row, universe date).

        Point in time: the newest Layer 1 run and universe snapshot dated on or
        before the as_of day. ORDER BY ... LIMIT 1 walks the date index and
        stops, where MAX() with a second predicate can scan (Turso meters reads).
        """
        day = format_day(cutoff)
        benchmark = self.config.settings.pulse.benchmark_asset
        with get_db() as db:
            run_date = db.scalar(
                "SELECT run_date FROM layer1_result WHERE run_date <= ? "
                "ORDER BY run_date DESC LIMIT 1",
                (day,),
            )
            survivors: list[str] = []
            if run_date:
                survivors = [
                    r["base_asset"]
                    for r in db.query(
                        "SELECT base_asset FROM layer1_result "
                        "WHERE run_date = ? AND passed = 1 ORDER BY base_asset",
                        (run_date,),
                    )
                ]
            universe_date = db.scalar(
                "SELECT snapshot_date FROM universe_snapshot "
                "WHERE snapshot_date <= ? AND exchange = 'binance' "
                "ORDER BY snapshot_date DESC LIMIT 1",
                (day,),
            )
            if not universe_date:
                raise RuntimeError(
                    "no Binance universe_snapshot on or before "
                    f"{day}: run `collect binance_universe` first"
                )
            wanted = sorted(set(survivors) | {benchmark})
            placeholders = ", ".join("?" for _ in wanted)
            rows = db.query(
                "SELECT symbol, base_asset, price_multiplier FROM universe_snapshot "
                "WHERE snapshot_date = ? AND exchange = 'binance' AND status = 'TRADING' "
                f"AND base_asset IN ({placeholders})",
                (universe_date, *wanted),
            )
        if not run_date:
            self.warn("pulse_no_layer1_run", note="only the benchmark is fetched")
        elif not survivors:
            self.warn("pulse_no_survivors", run_date=run_date)
        return survivors, _one_contract_per_asset(rows), universe_date

    # -- fetch ---------------------------------------------------------------
    async def fetch(self, as_of: datetime) -> dict[str, Any]:
        self.window = None
        cutoff = hour_floor(as_of)
        cfg = self.config.settings.pulse
        survivors, contracts, universe_date = self._load_targets(cutoff)
        benchmark = cfg.benchmark_asset

        fetch_errors: dict[str, str] = {}
        for asset in sorted(set(survivors) | {benchmark}):
            if asset not in contracts:
                fetch_errors[asset] = (
                    f"no TRADING Binance USDT-M perp in universe_snapshot {universe_date}"
                )
                self.warn("pulse_asset_unmapped", asset=asset, universe_date=universe_date)

        cutoff_ms = millis_from(cutoff)
        families = {
            # endTime = as_of - 1ms: Binance filters klines on OPEN time, so
            # this excludes the bar opening at as_of (still forming).
            "klines": (
                KLINES_PATH,
                None,
                lambda s: {
                    "symbol": s,
                    "interval": "1h",
                    "limit": cfg.klines_limit,
                    "endTime": cutoff_ms - 1,
                },
            ),
            "oi": (
                OI_HIST_PATH,
                LIMITER_FUTURES_DATA,
                lambda s: {
                    "symbol": s,
                    "period": "1h",
                    "limit": cfg.oi_limit,
                    "endTime": cutoff_ms,
                },
            ),
            "funding": (
                FUNDING_PATH,
                LIMITER_FUNDING,
                lambda s: {"symbol": s, "limit": cfg.funding_limit, "endTime": cutoff_ms},
            ),
        }
        payloads: dict[str, dict[str, Any]] = {f: {} for f in families}
        errors: dict[str, dict[str, str]] = {f: {} for f in families}
        symbols = sorted(row["symbol"] for row in contracts.values())
        semaphores = {f: asyncio.Semaphore(cfg.concurrency) for f in families}
        base = self.config.settings.endpoints["binance_futures"]

        async with self.client(base) as client:

            async def one(family: str, symbol: str) -> None:
                path, limiter_key, params = families[family]
                async with semaphores[family]:
                    try:
                        payloads[family][symbol] = await self.request_json(
                            client, "GET", path, limiter_key=limiter_key, params=params(symbol)
                        )
                    except IPBannedError:
                        raise  # D-052: stops the whole run, never one symbol
                    except Exception as exc:  # noqa: BLE001 - one symbol is not the run
                        errors[family][symbol] = f"{type(exc).__name__}: {str(exc)[:160]}"

            try:
                async with asyncio.TaskGroup() as group:
                    for family in families:
                        for symbol in symbols:
                            group.create_task(one(family, symbol))
            except BaseExceptionGroup as failure:
                # TaskGroup cancels every sibling on the first error, so no
                # further request leaves after a 418. Re-raise the ban itself.
                banned = failure.subgroup(IPBannedError)
                if banned is not None:
                    raise _first_leaf(banned) from None
                raise

        by_symbol = {row["symbol"]: asset for asset, row in contracts.items()}
        for symbol, error in errors["klines"].items():
            fetch_errors[by_symbol[symbol]] = f"klines: {error}"
            self.warn("pulse_klines_failed", asset=by_symbol[symbol], error=error)
        for family in ("oi", "funding"):
            for symbol, error in errors[family].items():
                self.warn(f"pulse_{family}_failed", asset=by_symbol[symbol], error=error)
        # D-051: bars are what Pulse is. Too many missing = failed, not partial.
        check_failure_share(len(errors["klines"]), len(symbols))
        for family in ("oi", "funding"):
            if symbols and len(errors[family]) / len(symbols) > 0.2:
                self.warn(
                    f"pulse_{family}_mostly_unavailable",
                    failed=len(errors[family]),
                    total=len(symbols),
                )

        return {
            "as_of": cutoff,
            "survivors": survivors,
            "benchmark": benchmark,
            "contracts": contracts,
            "payloads": payloads,
            "fetch_errors": fetch_errors,
        }

    # -- transform -----------------------------------------------------------
    def transform(self, raw: dict[str, Any], as_of: datetime) -> list[dict[str, Any]]:
        cutoff: datetime = raw["as_of"]
        fetched_at = utc_now_iso()
        payloads = raw["payloads"]
        window = MarketWindow(
            as_of=cutoff,
            survivors=list(raw["survivors"]),
            benchmark=raw["benchmark"],
            fetch_errors=dict(raw["fetch_errors"]),
        )
        rows: list[dict[str, Any]] = []

        for asset in sorted(raw["contracts"]):
            contract = raw["contracts"][asset]
            symbol = contract["symbol"]
            multiplier = contract.get("price_multiplier") or 1
            window.symbols[asset] = symbol
            if symbol not in payloads["klines"]:
                continue  # its failure is already in fetch_errors

            bars, bad = _parse_klines(payloads["klines"][symbol], cutoff, multiplier)
            if bad:
                self.warn("pulse_klines_malformed", asset=asset, rows=bad)
            window.bars[asset] = bars
            for ts, bar in bars.iterrows():
                rows.append(
                    {
                        _TABLE: "bar_1h",
                        "base_asset": asset,
                        "ts_open_utc": format_instant(ts.to_pydatetime()),
                        "symbol": symbol,
                        "open": float(bar["open"]),
                        "high": float(bar["high"]),
                        "low": float(bar["low"]),
                        "close": float(bar["close"]),
                        "quote_volume": float(bar["quote_volume"]),
                        "taker_buy_quote": float(bar["taker_buy_quote"]),
                        "trades": int(bar["trades"]),
                        "fetched_at_utc": fetched_at,
                    }
                )

            if symbol in payloads["oi"]:
                oi, bad = _parse_oi(payloads["oi"][symbol], cutoff, multiplier)
                if bad:
                    self.warn("pulse_oi_malformed", asset=asset, rows=bad)
                window.oi[asset] = oi
                for ts, obs in oi.iterrows():
                    usd = obs["oi_usd"]
                    rows.append(
                        {
                            _TABLE: "oi_1h",
                            "base_asset": asset,
                            "ts_utc": format_instant(ts.to_pydatetime()),
                            "symbol": symbol,
                            "oi_contracts": float(obs["oi_contracts"]),
                            "oi_usd": None if pd.isna(usd) else float(usd),
                            "fetched_at_utc": fetched_at,
                        }
                    )

            if symbol in payloads["funding"]:
                window.funding[asset] = funding_from_history(payloads["funding"][symbol], cutoff)

        self.window = window
        return rows

    # -- write ---------------------------------------------------------------
    def write(self, rows: list[dict[str, Any]]) -> int:
        """Upsert only what is newer than each asset's cursor, then advance it."""
        bars = [r for r in rows if r.get(_TABLE) == "bar_1h"]
        oi = [r for r in rows if r.get(_TABLE) == "oi_1h"]
        now = utc_now_iso()
        with get_db() as db:
            # ONE read of every Pulse cursor; never MAX() over bar_1h.
            cursors = {
                (r["series"], r["base_asset"]): r
                for r in db.query(
                    "SELECT series, base_asset, symbol, last_ts_utc FROM series_cursor "
                    "WHERE series IN (?, ?)",
                    (SERIES_BARS, SERIES_OI),
                )
            }
            new_bars, bar_cursors = select_new_rows(bars, "ts_open_utc", SERIES_BARS, cursors, now)
            new_oi, oi_cursors = select_new_rows(oi, "ts_utc", SERIES_OI, cursors, now)
            written = upsert(db, "bar_1h", new_bars) + upsert(db, "oi_1h", new_oi)
            # Cursors last: a crash before this line only means the next run
            # rewrites the same rows, which the upsert makes harmless.
            upsert(db, "series_cursor", bar_cursors + oi_cursors)
        self.log.info(
            "pulse_rows_written",
            bars=len(new_bars),
            oi=len(new_oi),
            cursors=len(bar_cursors) + len(oi_cursors),
            skipped_known=len(bars) + len(oi) - len(new_bars) - len(new_oi),
        )
        return written


def select_new_rows(
    rows: list[dict[str, Any]],
    ts_key: str,
    series: str,
    cursors: dict[tuple[str, str], dict[str, Any]],
    now: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(rows to write, cursor rows to upsert) for one series. Pure.

    A row is new when its timestamp is after the asset's cursor. No cursor, or
    a cursor recorded under another contract symbol, means the whole window is
    new. A cursor only moves when something is written, and never backwards
    under the same symbol (a replay of a past hour writes nothing).
    """
    by_asset: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_asset.setdefault(row["base_asset"], []).append(row)

    out: list[dict[str, Any]] = []
    cursor_rows: list[dict[str, Any]] = []
    for asset in sorted(by_asset):
        asset_rows = by_asset[asset]
        symbol = asset_rows[0]["symbol"]
        cursor = cursors.get((series, asset))
        last: str | None = None
        if cursor and cursor.get("symbol") == symbol and cursor.get("last_ts_utc"):
            last = format_instant(parse_instant(cursor["last_ts_utc"]))
        fresh = [
            {k: v for k, v in r.items() if k != _TABLE}
            for r in asset_rows
            if last is None or r[ts_key] > last
        ]
        if not fresh:
            continue
        out.extend(fresh)
        cursor_rows.append(
            {
                "series": series,
                "base_asset": asset,
                "symbol": symbol,
                "last_ts_utc": max(r[ts_key] for r in fresh),
                "updated_utc": now,
            }
        )
    return out, cursor_rows


def _first_leaf(group: BaseExceptionGroup) -> BaseException:
    first = group.exceptions[0]
    return _first_leaf(first) if isinstance(first, BaseExceptionGroup) else first


__all__ = [
    "BinancePulseCollector",
    "FUNDING_PATH",
    "KLINES_PATH",
    "OI_HIST_PATH",
    "SERIES_BARS",
    "SERIES_OI",
    "bars_from_klines",
    "funding_from_history",
    "hour_floor",
    "oi_from_hist",
    "select_new_rows",
]
