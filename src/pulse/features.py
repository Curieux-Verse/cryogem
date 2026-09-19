"""
# WHY: ------------------------------------------------------------------------
# MarketWindow -> one feature row per survivor (contract.FEATURE_COLUMNS).
# Pure: no I/O, no clock. Everything is anchored on `window.as_of`.
#
# CLOSED BARS ONLY. data.py already filters; this module strips again (and
# logs) any 1H bar with open + 1h > as_of and any OI/funding stamp > as_of. A
# look-ahead bug is the one defect a backtest can never reveal, so it is
# guarded twice.
#
# TIME, NOT ROWS (the D-060 lesson). Bars are laid on an hourly grid ending at
# the last bar that can have closed (anchor - 1h, anchor = as_of floored to the
# hour). "24h ago" is 24 grid slots back, and a gap is a NaN, never a silently
# shorter window. A series that stopped printing therefore measures nothing
# current: its returns and last-bar features are NaN, and under D-075 NaN
# scores nothing.
#
# Choices, each documented in D-079:
#   * Windowed sums (flow, volatility) need >= 75% of their bars present.
#   * Own-history statistics (flow_24h_z, vamom_24h_z, oi24_own_pctile) use
#     the last zscore_lookback_days of hourly values, EXCLUDING the current
#     one, and need >= 72 of them. Funding uses every settlement supplied
#     (8-hourly settlements over 20 days are only 60 points) and needs >= 30.
#   * "Own percentile" = share of the asset's own past values <= the current
#     one, 0-100. "Extreme" is >= extreme_own_pctile.
#   * An OI reading may be carried forward one hour, no more: OI "now" is at
#     most 1h stale, as in Layer 3's 90-minute tolerance.
#   * sigma for "no move" / "flat price" is the std of 1H log returns over the
#     lookback, scaled by sqrt(hours). Own history, not the same window: a
#     quiet day would otherwise define its own quietness away.
#   * OI "extreme" also means OI ROSE by at least oi_flat_change: a 95th-
#     percentile change that is a decline is not a build-up.
#   * R1 also needs funding > 0: crowding is longs paying shorts.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from src.config import PulseThresholds, get_config
from src.logging_setup import get_logger
from src.pulse import contract as C
from src.pulse.structure import min_structure_bars, structure_state

log = get_logger("pulse.features")

#: A windowed sum or std needs at least this share of its bars present.
MIN_WINDOW_FILL = 0.75
#: Fewest own-history values behind a z-score or an own percentile (3 days hourly).
MIN_OWN_HISTORY = 72
#: Fewest past funding settlements behind funding_own_pctile.
MIN_FUNDING_HISTORY = 30
#: Hours an OI reading may be carried forward on the hourly grid.
OI_CARRY_HOURS = 1

_AGG = {
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "quote_volume": "sum",
    "taker_buy_quote": "sum",
    "trades": "sum",
}


# ==============================================================================
# 4H bars
# ==============================================================================
def resample_4h(bars_1h: pd.DataFrame) -> pd.DataFrame:
    """1H bars -> complete 4H bars aligned to 00/04/08/12/16/20 UTC.

    Stamped with the bucket's OPEN time (label/closed left). A bucket is kept
    only when all four of its 1H bars are present: an incomplete bucket is
    either still forming or has a hole, and either way its close is not the
    4H close. Volumes and trades are sums. Same columns as the input.
    """
    if bars_1h is None or bars_1h.empty:
        return pd.DataFrame(columns=list(bars_1h.columns) if bars_1h is not None else list(C.BAR_COLUMNS),
                            index=pd.DatetimeIndex([], tz="UTC"))
    frame = bars_1h.sort_index()
    frame = frame[~frame.index.duplicated(keep="last")]
    agg = {col: _AGG.get(col, "last") for col in frame.columns}
    # origin="epoch" is 1970-01-01 00:00 UTC, and 4h divides 24h, so buckets
    # sit on 00/04/.../20 UTC whatever the first bar loaded.
    opts = {"label": "left", "closed": "left", "origin": "epoch"}
    out = frame.resample("4h", **opts).agg(agg)
    counts = frame["close"].resample("4h", **opts).count()
    out = out[counts.reindex(out.index).fillna(0) == 4]
    return out[list(frame.columns)]


# ==============================================================================
# Helpers
# ==============================================================================
def _need(n: int) -> int:
    return int(math.ceil(MIN_WINDOW_FILL * n))


def _anchor(as_of) -> pd.Timestamp:
    ts = pd.Timestamp(as_of)
    if ts.tzinfo is None:
        raise ValueError("MarketWindow.as_of must be tz-aware UTC")
    return ts.tz_convert("UTC").floor("h")


def _closed_bars(asset: str, bars: pd.DataFrame | None, as_of: pd.Timestamp) -> pd.DataFrame:
    if bars is None or bars.empty:
        return pd.DataFrame(columns=list(C.BAR_COLUMNS), index=pd.DatetimeIndex([], tz="UTC"))
    frame = bars.sort_index()
    frame = frame[~frame.index.duplicated(keep="last")]
    open_bars = frame.index + C.BAR_1H > as_of
    if open_bars.any():
        log.warning("pulse_lookahead_stripped", asset=asset, kind="bar_1h", rows=int(open_bars.sum()))
        frame = frame[~open_bars]
    return frame.astype(float)


def _visible(asset: str, kind: str, obj, as_of: pd.Timestamp):
    if obj is None or len(obj) == 0:
        return None
    obj = obj.sort_index()
    late = obj.index > as_of
    if late.any():
        log.warning("pulse_lookahead_stripped", asset=asset, kind=kind, rows=int(late.sum()))
        obj = obj[~late]
    return obj if len(obj) else None


def _zscore(series: pd.Series, lookback: int) -> float:
    """Current (last) value against the previous `lookback` values."""
    if series.empty or pd.isna(series.iloc[-1]):
        return np.nan
    hist = series.iloc[-(lookback + 1) : -1].dropna()
    if len(hist) < MIN_OWN_HISTORY:
        return np.nan
    sd = float(hist.std(ddof=1))
    if not sd > 0:
        return np.nan
    return float((series.iloc[-1] - hist.mean()) / sd)


def _own_pctile(current: float, hist: pd.Series, min_count: int) -> float:
    """Share (0-100) of the asset's own past values at or below `current`."""
    hist = hist.dropna()
    if pd.isna(current) or len(hist) < min_count:
        return np.nan
    return float((hist <= current).mean() * 100.0)


def _window_flow(qv: pd.Series, tb: pd.Series, n: int) -> pd.Series:
    """Rolling (2*taker_buy - volume) / volume over n grid bars."""
    vol = qv.rolling(n, min_periods=_need(n)).sum()
    buy = tb.rolling(n, min_periods=_need(n)).sum()
    return ((2.0 * buy - vol) / vol.where(vol > 0)).astype(float)


def _thrust(grid: pd.DataFrame, baseline_bars: int) -> float:
    """log(volume of the last bar / median volume of the prior N) * sign(bar return)."""
    if len(grid) < 2:
        return np.nan
    last = grid.iloc[-1]
    vol, o, c = last["quote_volume"], last["open"], last["close"]
    if pd.isna(vol) or pd.isna(o) or pd.isna(c) or vol <= 0:
        return np.nan
    prior = grid["quote_volume"].iloc[-(baseline_bars + 1) : -1].dropna()
    if len(prior) < _need(baseline_bars):
        return np.nan
    base = float(prior.median())
    if not base > 0:
        return np.nan
    return float(math.log(vol / base) * np.sign(c - o))


def _ret(close: pd.Series, h: int) -> float:
    if len(close) <= h:
        return np.nan
    now, then = close.iloc[-1], close.iloc[-1 - h]
    if pd.isna(now) or pd.isna(then) or then <= 0:
        return np.nan
    return float(now / then - 1.0)


def _vamom(ret: float, logret: pd.Series, h: int) -> float:
    window = logret.iloc[-h:].dropna()
    if pd.isna(ret) or len(window) < _need(h):
        return np.nan
    sd = float(window.std(ddof=1))
    if not sd > 0:
        return np.nan
    return float(ret / (sd * math.sqrt(h)))


def oi_quadrant(
    ret: float,
    oi_chg: float,
    flow: float,
    sigma: float,
    oi_pctile: float,
    cfg: PulseThresholds,
) -> str | None:
    """F4: OI (contracts) x price x taker flow at one horizon (D-074).

    None when price, OI change or sigma is unmeasured. Only confirm_long scores
    above neutral, and it needs price UP (beyond the flat band) AND flow > 0 AND
    OI up: OI confirms price and flow, it never leads them.
    """
    if pd.isna(ret) or pd.isna(oi_chg) or pd.isna(sigma):
        return None
    flat = cfg.oi_flat_change
    oi_up, oi_down = oi_chg >= flat, oi_chg <= -flat
    if abs(ret) < cfg.no_move_sigma * sigma:
        if oi_up and not pd.isna(oi_pctile) and oi_pctile >= cfg.extreme_own_pctile:
            return C.OI_LEVERAGE_BUILD
        return C.OI_NEUTRAL
    buying = not pd.isna(flow) and flow > 0
    selling = not pd.isna(flow) and flow < 0
    if ret > 0:
        if oi_up:
            return C.OI_CONFIRM_LONG if buying else C.OI_NEUTRAL
        if oi_down:
            return C.OI_SHORT_COVERING
        return C.OI_NEUTRAL
    if oi_up:
        return C.OI_NEW_SHORTS if selling else C.OI_NEUTRAL
    if oi_down:
        return C.OI_LONG_LIQUIDATION
    return C.OI_NEUTRAL


def _oi_grid(oi: pd.DataFrame | None, anchor: pd.Timestamp) -> pd.Series | None:
    if oi is None or "oi_contracts" not in oi.columns:
        return None
    s = pd.to_numeric(oi["oi_contracts"], errors="coerce").dropna()
    if s.empty:
        return None
    s.index = s.index.floor("h")
    s = s[~s.index.duplicated(keep="last")]
    grid = pd.date_range(s.index[0], anchor, freq="h")
    if len(grid) == 0:
        return None
    return s.reindex(grid).ffill(limit=OI_CARRY_HOURS).where(lambda v: v > 0)


# ==============================================================================
# Features
# ==============================================================================
def _asset_features(asset: str, window: C.MarketWindow, cfg: PulseThresholds) -> dict:
    as_of = pd.Timestamp(window.as_of).tz_convert("UTC")
    anchor = _anchor(window.as_of)
    lookback = int(cfg.zscore_lookback_days * 24)
    row: dict = {col: np.nan for col in C.FEATURE_COLUMNS}
    for col in ("oi_quadrant_4h", "oi_quadrant_24h", "state_4h", "state_1h"):
        row[col] = None
    flags: list[str] = []

    bars = _closed_bars(asset, window.bars.get(asset), as_of)
    bars.index = bars.index.floor("h")
    bars = bars[~bars.index.duplicated(keep="last")]
    row["n_bars_1h"] = float(len(bars))

    sigma_1h = np.nan
    if len(bars):
        row["last_close"] = float(bars["close"].iloc[-1])
        grid_index = pd.date_range(bars.index[0], anchor - C.BAR_1H, freq="h")
        g = bars.reindex(grid_index)
        close, qv, tb = g["close"], g["quote_volume"], g["taker_buy_quote"]
        logret = np.log(close.where(close > 0)).diff()

        row["quote_volume_24h"] = float(qv.iloc[-24:].sum(min_count=1))
        for h, col in ((1, "ret_1h"), (4, "ret_4h"), (24, "ret_24h"), (168, "ret_7d")):
            row[col] = _ret(close, h)

        # F1 taker flow.
        flow24 = _window_flow(qv, tb, 24)
        row["flow_24h"] = float(flow24.iloc[-1])
        row["flow_4h"] = float(_window_flow(qv, tb, 4).iloc[-1])
        row["flow_24h_z"] = _zscore(flow24, lookback)

        # F2 thrust, 1H and 4H (the last COMPLETE 4H bucket, or nothing).
        row["thrust_1h"] = _thrust(g, cfg.thrust_baseline_bars)
        b4 = resample_4h(bars)
        expected_4h = anchor.floor("4h") - C.BAR_4H
        if len(b4) and b4.index[-1] == expected_4h:
            g4 = b4.reindex(pd.date_range(b4.index[0], expected_4h, freq="4h"))
            row["thrust_4h"] = _thrust(g4, cfg.thrust_baseline_bars)

        # F3 vol-adjusted momentum.
        row["vamom_24h"] = _vamom(row["ret_24h"], logret, 24)
        row["vamom_7d"] = _vamom(row["ret_7d"], logret, 168)
        ret24s = close / close.shift(24) - 1.0
        sd24s = logret.rolling(24, min_periods=_need(24)).std(ddof=1)
        vam24s = ret24s / (sd24s.where(sd24s > 0) * math.sqrt(24))
        row["vamom_24h_z"] = _zscore(vam24s, lookback)

        hist_lr = logret.iloc[-lookback:].dropna()
        if len(hist_lr) >= MIN_OWN_HISTORY:
            sigma_1h = float(hist_lr.std(ddof=1))

        # F5 / F6 structure.
        need = min_structure_bars(cfg.structure)
        if len(b4) >= need:
            state, since, inval = structure_state(b4, cfg.structure)
            row["state_4h"], row["invalidation_4h"] = state, (np.nan if inval is None else inval)
            row["bars_since_4h"] = np.nan if since is None else float(since)
        if len(bars) >= need:
            state, since, _ = structure_state(bars, cfg.structure)
            row["state_1h"] = state
            row["bars_since_1h"] = np.nan if since is None else float(since)

    # F4 open interest, in contracts.
    oi_pctile_4h = np.nan
    oi = _oi_grid(_visible(asset, "oi", window.oi.get(asset), as_of), anchor)
    if oi is not None and len(oi):
        cur = oi.iloc[-1]
        if len(oi) > 4:
            row["oi_chg_4h"] = float(cur / oi.iloc[-5] - 1.0) if not pd.isna(oi.iloc[-5]) else np.nan
        if len(oi) > 24:
            row["oi_chg_24h"] = float(cur / oi.iloc[-25] - 1.0) if not pd.isna(oi.iloc[-25]) else np.nan
        chg24 = oi / oi.shift(24) - 1.0
        chg4 = oi / oi.shift(4) - 1.0
        row["oi24_own_pctile"] = _own_pctile(
            row["oi_chg_24h"], chg24.iloc[-(lookback + 1) : -1], MIN_OWN_HISTORY
        )
        oi_pctile_4h = _own_pctile(row["oi_chg_4h"], chg4.iloc[-(lookback + 1) : -1], MIN_OWN_HISTORY)
    sigma = {h: (sigma_1h * math.sqrt(h) if not pd.isna(sigma_1h) else np.nan) for h in (4, 24)}
    row["oi_quadrant_4h"] = oi_quadrant(
        row["ret_4h"], row["oi_chg_4h"], row["flow_4h"], sigma[4], oi_pctile_4h, cfg
    )
    row["oi_quadrant_24h"] = oi_quadrant(
        row["ret_24h"], row["oi_chg_24h"], row["flow_24h"], sigma[24], row["oi24_own_pctile"], cfg
    )

    # Funding: risk input only. Nothing in Pulse lets it raise a score.
    funding = _visible(asset, "funding", window.funding.get(asset), as_of)
    if funding is not None:
        funding = pd.to_numeric(funding, errors="coerce").dropna()
        if len(funding):
            row["funding_last"] = float(funding.iloc[-1])
            row["funding_own_pctile"] = _own_pctile(
                row["funding_last"], funding.iloc[:-1], MIN_FUNDING_HISTORY
            )

    # Risk flags.
    ext = cfg.extreme_own_pctile
    oi_extreme = (
        not pd.isna(row["oi24_own_pctile"])
        and row["oi24_own_pctile"] >= ext
        and row["oi_chg_24h"] >= cfg.oi_flat_change
    )
    if (
        oi_extreme
        and not pd.isna(row["funding_own_pctile"])
        and row["funding_last"] > 0
        and row["funding_own_pctile"] >= ext
    ):
        flags.append(C.FLAG_CROWDING)
    if (
        oi_extreme
        and not pd.isna(row["ret_24h"])
        and not pd.isna(sigma[24])
        and abs(row["ret_24h"]) < cfg.no_move_sigma * sigma[24]
    ):
        flags.append(C.FLAG_LEVERAGE_NO_MOVE)
    if pd.isna(row["quote_volume_24h"]) or row["quote_volume_24h"] < cfg.min_quote_volume_24h_usd:
        flags.append(C.FLAG_THIN_BOOK)
    if row["n_bars_1h"] < cfg.min_bars_1h:
        flags.append(C.FLAG_INSUFFICIENT)
    row["flags"] = flags
    return row


def compute_features(window: C.MarketWindow, cfg: PulseThresholds | None = None) -> pd.DataFrame:
    """One row per survivor, in window.survivors order, columns FEATURE_COLUMNS.

    Numeric columns are float with NaN for "could not measure", never 0.
    """
    cfg = cfg if cfg is not None else get_config().thresholds.pulse
    rows = [_asset_features(asset, window, cfg) for asset in window.survivors]
    frame = pd.DataFrame(rows, columns=list(C.FEATURE_COLUMNS),
                         index=pd.Index(list(window.survivors), name="base_asset"))
    for col in C.FEATURE_COLUMNS:
        if col not in ("oi_quadrant_4h", "oi_quadrant_24h", "state_4h", "state_1h", "flags"):
            frame[col] = pd.to_numeric(frame[col], errors="coerce").astype(float)
    return frame


__all__ = ["compute_features", "oi_quadrant", "resample_4h"]
