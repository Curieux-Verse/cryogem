"""
# WHY: ------------------------------------------------------------------------
# Layer 3: structure, timing, and positioning risk. ADVISORY ONLY.
#
# Layer 3 cannot promote anything. It receives assets that already survived the
# L1 kill switch and were ranked by L2, and its output is "if you were to act
# on this, here is where you would be wrong, and here is what the derivatives
# positioning says about how fragile the move is". Nothing here creates a
# candidate; it only annotates and vetoes one.
#
# THE RULE THAT DEFINES THIS MODULE: derivatives are a RISK CHECK, NEVER A BUY
# TRIGGER. The case that fixed this in the spec is TRB -- negative funding was
# read as a bullish crowd-positioning signal hours before a 78% collapse. So
# funding and OI appear here as fragility flags on an already-ranked asset, and
# there is deliberately no code path in which a funding reading raises a score.
#
# Second rule: EVERY CANDIDATE EMITS AN INVALIDATION LEVEL OR IS NOT A
# CANDIDATE. A setup without a price at which the thesis is dead is not a
# setup, it is a hope. `analyse` refuses to mark setup_detected without one.
#
# On timeframe: breaks are confirmed on a CLOSE, on resampled 3D or weekly
# bars, never on a wick and never on a daily bar. A wick through a level is
# what a stop-hunt looks like; a weekly close beyond it is what a change in
# control looks like.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from src.config import get_config
from src.db.connection import Database, get_db
from src.db.writes import json_dump, upsert
from src.logging_setup import get_logger
from src.timeutil import add_days, utc_now_iso

log = get_logger("screening.layer3")

#: Resampling rule per timeframe. 3D and weekly only: the spec's structure
#: rules are defined on those, and running them on daily bars produces far more
#: "breaks", almost all of them noise.
TIMEFRAMES = {"3D": "3D", "1W": "W-MON"}

#: Bars of resampled history required before structure detection will answer.
#: Below this the fit is meaningless and the honest output is "insufficient".
MIN_BARS = 30


@dataclass
class Trendline:
    """A descending resistance line fitted to swing highs.

    `slope` is price per bar and is negative by construction -- an ascending
    line is a different setup with different rules, and returning one from a
    function named for descending lines is how the two get conflated.
    """

    slope: float
    intercept: float
    touches: int
    first_bar: int
    last_bar: int
    first_date: str
    last_date: str
    tolerance_pct: float

    def value_at(self, bar: int) -> float:
        return self.slope * bar + self.intercept


@dataclass
class BreakEvent:
    bar: int
    date: str
    close: float
    line_value: float
    #: How far beyond the line the close settled, as a share of the line.
    margin_pct: float


@dataclass
class RetestEvent:
    bar: int
    date: str
    low: float
    line_value: float
    held: bool


@dataclass
class Zone:
    """A fair-value gap or order block. Both are price ranges, not levels."""

    kind: str
    low: float
    high: float
    date: str
    bar: int

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2.0


@dataclass
class Layer3Result:
    base_asset: str
    setup_detected: bool = False
    setup_type: str | None = None
    invalidation_price: float | None = None
    trendline: Trendline | None = None
    break_event: BreakEvent | None = None
    retest: RetestEvent | None = None
    fvgs: list[Zone] = field(default_factory=list)
    order_blocks: list[Zone] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    depth_2pct_usd: float | None = None
    funding_pctile: float | None = None
    oi_change_pctile: float | None = None


# ==============================================================================
# Bars
# ==============================================================================
def resample(daily: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Daily OHLC to 3D or weekly bars.

    `label='left'` and `closed='left'` so a bar is stamped with the date it
    OPENED. The alternative stamps a bar with a date after the data it
    contains, and a "break on 2026-09-14" that actually used bars through
    2026-09-20 is look-ahead bias wearing a timestamp.
    """
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"unsupported timeframe {timeframe!r}; expected one of {list(TIMEFRAMES)}")
    frame = daily.sort_index()
    out = frame.resample(TIMEFRAMES[timeframe], label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    return out.dropna(subset=["close"])


def load_bars(db: Database, base_asset: str, as_of: str, days: int = 1100) -> pd.DataFrame:
    """Daily OHLC up to and including `as_of`. Never beyond it.

    The upper bound is the whole point. Layer 3 is run historically by the
    backtest harness, and a structure detector that can see one bar past its
    as_of date will find every break perfectly.
    """
    rows = db.query(
        "SELECT snapshot_date, open_usd, high_usd, low_usd, close_usd, volume_usd "
        "FROM price_daily WHERE base_asset = ? AND snapshot_date BETWEEN ? AND ? "
        "AND high_usd IS NOT NULL AND low_usd IS NOT NULL "
        "ORDER BY snapshot_date",
        (base_asset, add_days(as_of, -days), as_of),
    )
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    frame = pd.DataFrame(rows)
    frame["snapshot_date"] = pd.to_datetime(frame["snapshot_date"])
    frame = frame.set_index("snapshot_date").rename(
        columns={
            "open_usd": "open",
            "high_usd": "high",
            "low_usd": "low",
            "close_usd": "close",
            "volume_usd": "volume",
        }
    )
    return frame[["open", "high", "low", "close", "volume"]].astype(float)


# ==============================================================================
# Structure
# ==============================================================================
def swing_highs(high: pd.Series, window: int) -> list[int]:
    """Positions of local maxima with `window` bars strictly lower each side.

    A pivot needs bars on BOTH sides, so the last `window` bars can never be
    pivots. That is correct rather than inconvenient: a high with nothing after
    it is not yet known to be a high, and treating it as one is the most common
    way a trendline detector fits itself to the present.
    """
    values = high.to_numpy(dtype=float)
    out: list[int] = []
    for i in range(window, len(values) - window):
        left = values[i - window : i]
        right = values[i + 1 : i + 1 + window]
        if values[i] > left.max() and values[i] > right.max():
            out.append(i)
    return out


def detect_descending_trendline(
    bars: pd.DataFrame, min_touches: int | None = None, tolerance_pct: float | None = None
) -> Trendline | None:
    """Fit a descending line to swing highs, requiring >= min_touches.

    Every pair of descending swing highs defines a candidate line. A candidate
    is valid only if no CLOSE between its endpoints finished above it -- a line
    that price closed through is not resistance, it is a line drawn through
    old data. Among the valid candidates, the most touches wins, then the most
    recent, because a line last touched two years ago is not describing the
    current market.
    """
    cfg = get_config().thresholds.layer3
    min_touches = min_touches if min_touches is not None else cfg.trendline_min_touches
    tolerance = tolerance_pct if tolerance_pct is not None else cfg.trendline_tolerance_pct

    if len(bars) < MIN_BARS:
        return None

    pivots = swing_highs(bars["high"], cfg.swing_window_bars)
    if len(pivots) < min_touches:
        return None

    highs = bars["high"].to_numpy(dtype=float)
    closes = bars["close"].to_numpy(dtype=float)
    dates = [d.strftime("%Y-%m-%d") for d in bars.index]
    best: Trendline | None = None

    for a_idx in range(len(pivots) - 1):
        for b_idx in range(a_idx + 1, len(pivots)):
            i, j = pivots[a_idx], pivots[b_idx]
            if highs[j] >= highs[i]:
                continue  # not descending
            slope = (highs[j] - highs[i]) / (j - i)
            intercept = highs[i] - slope * i

            line = slope * np.arange(len(highs)) + intercept
            # Valid only while unbroken between the endpoints.
            if np.any(closes[i : j + 1] > line[i : j + 1] * (1 + tolerance)):
                continue

            near = [
                p
                for p in pivots
                if i <= p <= j and abs(highs[p] - line[p]) <= abs(line[p]) * tolerance
            ]
            touches = len(near)
            if touches < min_touches:
                continue

            candidate = Trendline(
                slope=float(slope),
                intercept=float(intercept),
                touches=touches,
                first_bar=i,
                last_bar=j,
                first_date=dates[i],
                last_date=dates[j],
                tolerance_pct=tolerance,
            )
            if best is None or (candidate.touches, candidate.last_bar) > (
                best.touches,
                best.last_bar,
            ):
                best = candidate
    return best


def detect_break(bars: pd.DataFrame, trendline: Trendline) -> BreakEvent | None:
    """The first CLOSE above the line, after the line's last touch.

    Close-based, not wick-based, and the distinction is the entire value of the
    function. A wick through resistance is what a stop-hunt looks like; a
    weekly close beyond it is what a change in control looks like. Using highs
    here would report a break on almost every asset in the universe.
    """
    closes = bars["close"].to_numpy(dtype=float)
    dates = [d.strftime("%Y-%m-%d") for d in bars.index]
    for bar in range(trendline.last_bar + 1, len(closes)):
        line_value = trendline.value_at(bar)
        if line_value <= 0:
            # The extrapolated line has descended through zero. Beyond that
            # point every price is "above the line" and the concept is
            # meaningless, so stop rather than report a break.
            return None
        if closes[bar] > line_value * (1 + trendline.tolerance_pct):
            return BreakEvent(
                bar=bar,
                date=dates[bar],
                close=float(closes[bar]),
                line_value=float(line_value),
                margin_pct=float((closes[bar] - line_value) / line_value),
            )
    return None


def detect_retest(bars: pd.DataFrame, trendline: Trendline, brk: BreakEvent) -> RetestEvent | None:
    """A pullback into the broken line, and whether it held.

    "Held" means the bar CLOSED back above the line after trading into it.
    Broken resistance becoming support is the confirmation the setup is built
    on; a close back below it is the failed-break case, and calling that a
    retest would invert the signal.
    """
    lows = bars["low"].to_numpy(dtype=float)
    closes = bars["close"].to_numpy(dtype=float)
    dates = [d.strftime("%Y-%m-%d") for d in bars.index]
    tolerance = trendline.tolerance_pct

    for bar in range(brk.bar + 1, len(lows)):
        line_value = trendline.value_at(bar)
        if line_value <= 0:
            return None
        if lows[bar] <= line_value * (1 + tolerance):
            return RetestEvent(
                bar=bar,
                date=dates[bar],
                low=float(lows[bar]),
                line_value=float(line_value),
                held=bool(closes[bar] > line_value),
            )
    return None


def detect_fvg(bars: pd.DataFrame, limit: int = 5) -> list[Zone]:
    """Three-bar imbalances: a gap between bar 1's high and bar 3's low.

    A bullish FVG is a range price moved through so fast that no trading
    happened inside it. Only UNFILLED gaps are returned -- a gap price has
    since traded back through is no longer an imbalance, and keeping it would
    litter the chart with resolved history.
    """
    high = bars["high"].to_numpy(dtype=float)
    low = bars["low"].to_numpy(dtype=float)
    dates = [d.strftime("%Y-%m-%d") for d in bars.index]
    zones: list[Zone] = []

    for i in range(len(bars) - 2):
        gap_low, gap_high = high[i], low[i + 2]
        if gap_high <= gap_low:
            continue
        # Unfilled: no later low has traded back down into the gap.
        later_lows = low[i + 3 :]
        if len(later_lows) and later_lows.min() <= gap_low:
            continue
        zones.append(
            Zone(kind="fvg_bullish", low=float(gap_low), high=float(gap_high), date=dates[i + 1], bar=i + 1)
        )
    return zones[-limit:]


def detect_order_block(bars: pd.DataFrame, limit: int = 5) -> list[Zone]:
    """The last down bar before an impulsive up move that takes its high out.

    Deliberately mechanical: last opposing candle, then a displacement bar
    whose close clears the opposing candle's high. Order blocks are drawn by
    hand in most of the literature, which means any implementation is one
    interpretation -- so this one states its rule rather than implying there is
    a canonical answer.
    """
    open_ = bars["open"].to_numpy(dtype=float)
    high = bars["high"].to_numpy(dtype=float)
    low = bars["low"].to_numpy(dtype=float)
    close = bars["close"].to_numpy(dtype=float)
    dates = [d.strftime("%Y-%m-%d") for d in bars.index]
    zones: list[Zone] = []

    for i in range(len(bars) - 1):
        is_down = close[i] < open_[i]
        if not is_down:
            continue
        if close[i + 1] > high[i]:
            zones.append(
                Zone(
                    kind="order_block_bullish",
                    low=float(low[i]),
                    high=float(high[i]),
                    date=dates[i],
                    bar=i,
                )
            )
    # Only blocks price has not since closed below: a violated block is not a
    # zone of interest, it is a level that failed.
    surviving = [z for z in zones if close[z.bar + 1 :].min() > z.low]
    return surviving[-limit:]


# ==============================================================================
# Invalidation -- mandatory
# ==============================================================================
def compute_invalidation(
    bars: pd.DataFrame,
    trendline: Trendline,
    brk: BreakEvent,
    retest: RetestEvent | None,
) -> float | None:
    """The price at which the thesis is dead. No level, no candidate.

    Priority, most to least specific:
      1. The retest bar's low. Price came back, held, and turned: below that
         low the "broken resistance is now support" reading is simply wrong.
      2. The break bar's low, when there is no retest yet.
      3. None -- and a None here means the asset is NOT reported as a setup.

    Returning a fallback such as "10% below current price" would be worse than
    returning nothing: it looks like a computed level and is an arbitrary one,
    and position sizing divides by exactly this number.
    """
    lows = bars["low"].to_numpy(dtype=float)
    if retest is not None and retest.held:
        return float(lows[retest.bar])
    if brk.bar < len(lows):
        return float(lows[brk.bar])
    return None


# ==============================================================================
# Positioning risk -- advisory, never a trigger
# ==============================================================================
def funding_percentile(db: Database, base_asset: str, as_of: str, days: int = 90) -> float | None:
    """Where today's interval-normalised funding sits in the asset's OWN history.

    Two things this deliberately does NOT do:

      * It does not compare across assets. Funding levels are venue- and
        asset-specific; a cross-sectional funding percentile mostly ranks how
        crowded an asset's perp is relative to unrelated markets.
      * It does not treat +x and -x as mirror images. The Binance/Hyperliquid
        formula is `F = premium + clamp(interest - premium, -0.05%, +0.05%)`
        with interest fixed at 0.01% per 8h, which gives funding a STRUCTURAL
        POSITIVE BIAS: it only reaches zero when the average premium index is
        -0.05%, and only goes negative below that. Negative funding is
        therefore a materially stronger reading than positive funding of the
        same magnitude, and the caller flags the two asymmetrically.

    Returns None when there is not enough history, which early in the
    project's life is the normal answer.
    """
    rows = db.query(
        "SELECT funding_apr FROM derivatives_snapshot WHERE base_asset = ? "
        "AND ts_utc BETWEEN ? AND ? AND funding_apr IS NOT NULL ORDER BY ts_utc",
        (base_asset, f"{add_days(as_of, -days)}T00:00:00Z", f"{as_of}T23:59:59Z"),
    )
    values = [r["funding_apr"] for r in rows]
    if len(values) < 20:
        return None
    series = pd.Series(values, dtype=float)
    return float(series.rank(pct=True).iloc[-1] * 100.0)


def oi_change_percentiles(
    db: Database, base_asset: str, as_of: str, windows_hours=(1, 4, 12, 24)
) -> dict[str, float | None]:
    """OI change over several windows, each as a percentile of its own history.

    Multi-window because a single 24h reading cannot distinguish a steady build
    from a violent one-hour spike, and those are opposite risk pictures. OI is
    measured in CONTRACTS where available -- a notional OI series rises when
    price rises even with position count flat, which would flag every rally as
    a leverage build.
    """
    rows = db.query(
        "SELECT ts_utc, open_interest_base, open_interest_usd FROM derivatives_snapshot "
        "WHERE base_asset = ? AND ts_utc BETWEEN ? AND ? ORDER BY ts_utc",
        (base_asset, f"{add_days(as_of, -30)}T00:00:00Z", f"{as_of}T23:59:59Z"),
    )
    out: dict[str, float | None] = {f"{h}h": None for h in windows_hours}
    if len(rows) < 20:
        return out

    frame = pd.DataFrame(rows)
    frame["ts"] = pd.to_datetime(frame["ts_utc"])
    frame = frame.set_index("ts").sort_index()
    # Prefer contracts; fall back to notional and say so in the key.
    column = "open_interest_base" if frame["open_interest_base"].notna().any() else "open_interest_usd"
    series = pd.to_numeric(frame[column], errors="coerce").dropna()
    if len(series) < 20:
        return out

    for hours in windows_hours:
        change = series.pct_change(periods=max(1, hours))
        change = change.replace([np.inf, -np.inf], np.nan).dropna()
        if len(change) < 20:
            continue
        out[f"{hours}h"] = float(change.rank(pct=True).iloc[-1] * 100.0)
    return out


def spot_depth_2pct(db: Database, base_asset: str, as_of: str) -> float | None:
    """Bid depth within 2% of mid, in USD. The only honest answer to "can I exit".

    Bid side only, and on purpose: exiting a long consumes bids. Averaging both
    sides makes an asset with a deep ask and a hollow bid look tradeable, which
    is precisely the asset this check exists to veto.
    """
    row = db.query_one(
        "SELECT bid_depth_2p0 FROM depth_snapshot d "
        "JOIN universe_snapshot u ON u.symbol = d.symbol "
        "WHERE u.base_asset = ? AND d.ts_utc <= ? AND d.bid_depth_2p0 IS NOT NULL "
        "ORDER BY d.ts_utc DESC LIMIT 1",
        (base_asset, f"{as_of}T23:59:59Z"),
    )
    return row["bid_depth_2p0"] if row else None


def cvd_divergence(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    """NOT IMPLEMENTED, and not stubbed with a proxy. See docs/DECISIONS.md D-015.

    Spot-vs-perp CVD needs per-trade data with an aggressor flag. This project
    collects snapshots, not trade streams, so the input does not exist.

    The tempting substitute -- inferring buy and sell pressure from where a bar
    closed inside its range -- is not CVD. It correlates with the price move it
    is meant to explain, so it would confirm whatever the chart already showed
    while carrying the authority of a microstructure metric.

    Three caveats worth recording even so, because they bound how much the real
    thing would be worth here:
      * Without an aggressor flag, tools fall back on the tick rule, which
        degrades badly in fast markets where the book moves between prints.
      * CVD works poorly on thin altcoin perps -- exactly this universe.
      * Divergence can persist through an entire trend. It is not a timer.
    """
    return {
        "available": False,
        "reason": "requires per-trade data with an aggressor flag; not collected",
    }


# ==============================================================================
# Orchestration
# ==============================================================================
def analyse(db: Database, base_asset: str, as_of: str, timeframe: str = "1W") -> Layer3Result:
    """Structure and risk for one asset. Never promotes, only annotates."""
    cfg = get_config().thresholds.layer3
    result = Layer3Result(base_asset=base_asset)

    daily = load_bars(db, base_asset, as_of)
    if daily.empty:
        result.notes.append("no OHLC history: structure cannot be assessed")
        return result

    bars = resample(daily, timeframe)
    if len(bars) < MIN_BARS:
        result.notes.append(
            f"only {len(bars)} {timeframe} bars, need {MIN_BARS}: too little history "
            "for a trendline to mean anything"
        )
        return result

    # -- risk first. These are vetoes, and a veto is cheaper than a fit. -----
    result.depth_2pct_usd = spot_depth_2pct(db, base_asset, as_of)
    result.funding_pctile = funding_percentile(db, base_asset, as_of)
    oi = oi_change_percentiles(db, base_asset, as_of)
    result.oi_change_pctile = oi.get("24h")

    if result.depth_2pct_usd is None:
        # A FLAG, not just a note. An unmeasured exit cost that appears only in
        # prose reads as "no flags" everywhere the flags are what gets shown --
        # the report table, the dashboard, the journal's stored layer3_values.
        # Unknown is not acceptable; it is unknown, and it has to look it.
        result.risk_flags.append("L3_DEPTH_UNKNOWN")
        result.notes.append(
            "no depth measurement: the exit cost of a position is unknown, "
            "which is not the same as acceptable"
        )
    elif result.depth_2pct_usd < cfg.min_depth_2pct_usd:
        result.risk_flags.append("L3_THIN_DEPTH")
        result.notes.append(
            f"bid depth within 2% is ${result.depth_2pct_usd:,.0f}, below "
            f"${cfg.min_depth_2pct_usd:,.0f}: there is no exit at size"
        )

    if result.funding_pctile is not None:
        if result.funding_pctile <= cfg.funding_negative_extreme_pctile:
            # Asymmetric BY DESIGN: the funding formula's structural positive
            # bias makes a low reading the stronger of the two. Flagged as
            # FRAGILE, never as bullish -- TRB's negative funding preceded a
            # 78% collapse by hours.
            result.risk_flags.append("L3_FUNDING_NEGATIVE_EXTREME")
            result.notes.append(
                "funding is at the bottom of its own 90-day range. Funding has a "
                "structural positive bias, so this is a strong reading -- of "
                "CROWDING, not of direction. It is a fragility flag."
            )
        elif result.funding_pctile >= 100.0 - cfg.funding_negative_extreme_pctile:
            result.risk_flags.append("L3_FUNDING_POSITIVE_EXTREME")

    for window, pctile in oi.items():
        if pctile is not None and pctile >= cfg.oi_change_extreme_pctile:
            result.risk_flags.append(f"L3_OI_SPIKE_{window.upper()}")

    # -- structure ----------------------------------------------------------
    trendline = detect_descending_trendline(bars)
    result.fvgs = detect_fvg(bars)
    result.order_blocks = detect_order_block(bars)

    if trendline is None:
        result.notes.append("no descending trendline with the required touches")
        return result
    result.trendline = trendline

    brk = detect_break(bars, trendline)
    if brk is None:
        result.notes.append(
            f"trendline intact: {trendline.touches} touches, last "
            f"{trendline.last_date}. No close above it yet."
        )
        return result
    result.break_event = brk
    result.retest = detect_retest(bars, trendline, brk)

    # RECENCY. A setup is a present-tense claim, and a break is only one while
    # it is still the most recent thing that happened to the level. On
    # 2026-09-09 this check reclassified MINA (broken 27 weeks earlier) and
    # IOST (30 weeks) out of the live list -- both were being reported with
    # invalidation levels from March, which position sizing would have divided
    # by. Beyond the bound the structure is history, and the honest label says
    # how old it is rather than omitting the age.
    bars_since = len(bars) - 1 - brk.bar
    if bars_since > cfg.max_bars_since_break:
        result.setup_type = "break_stale"
        result.notes.append(
            f"trendline broke {bars_since} {timeframe} bars ago ({brk.date}), beyond "
            f"the {cfg.max_bars_since_break}-bar window. Recorded as structure, not "
            "reported as a setup: an invalidation level that old describes a "
            "different market."
        )
        return result

    invalidation = compute_invalidation(bars, trendline, brk, result.retest)
    if invalidation is None:
        # The rule, enforced rather than documented: no level, no setup.
        result.notes.append(
            "break detected but no invalidation level could be computed, so this "
            "is not reported as a setup"
        )
        return result

    result.invalidation_price = invalidation
    last_close = float(bars["close"].iloc[-1])
    if invalidation >= last_close:
        # The stop is already above price: the setup has failed, and reporting
        # it as live would hand the reader a negative-risk trade.
        result.notes.append(
            f"invalidation {invalidation:,.6g} is at or above the last close "
            f"{last_close:,.6g}: the setup is already invalidated"
        )
        result.risk_flags.append("L3_ALREADY_INVALIDATED")
        return result

    if result.retest is not None and not result.retest.held:
        result.setup_type = "break_failed_retest"
        result.notes.append(
            "price returned to the broken line and closed back below it. This is "
            "the failed-break case, not a confirmation."
        )
        result.risk_flags.append("L3_FAILED_RETEST")
        return result

    result.setup_detected = True
    result.setup_type = (
        "trendline_break_retest_held" if result.retest is not None else "trendline_break"
    )
    result.notes.append(
        f"descending trendline ({trendline.touches} touches, last touch "
        f"{trendline.last_date}) broken on the {brk.date} {timeframe} close, "
        f"{brk.margin_pct:.1%} above the line."
        + _retest_note(bars, trendline, result.retest)
    )
    return result


def _retest_note(bars: pd.DataFrame, trendline: Trendline, retest: RetestEvent | None) -> str:
    """Say whether a retest is pending or impossible. They are not the same.

    "No retest yet" implies one may still come. But the line keeps descending
    while price is above it, so once the gap is wide enough a retest can never
    occur -- and telling the reader to wait for one would mean telling them to
    wait forever.
    """
    if retest is not None:
        return f" Retested {retest.date} and held."
    last_bar = len(bars) - 1
    line_now = trendline.value_at(last_bar)
    last_low = float(bars["low"].iloc[-1])
    if line_now <= 0:
        return " The extrapolated line has descended through zero: no retest is possible."
    distance = (last_low - line_now) / line_now
    if distance > 0.25:
        return (
            f" No retest, and none is likely: price sits {distance:.0%} above the "
            "descending line, which keeps moving away. Do not wait for one."
        )
    return " No retest yet: entry on a retest is unconfirmed."


def run_layer3(
    db: Database, run_date: str, assets: list[str], timeframe: str = "1W"
) -> list[dict[str, Any]]:
    """Analyse the ranked assets and persist to layer3_result.

    Takes an explicit asset list rather than reading the ranking itself: Layer 3
    must be runnable over an arbitrary set (a backtest date, one asset from the
    CLI) and must never be in a position to decide which assets exist.
    """
    if not assets:
        log.warning("layer3_no_assets", run_date=run_date)
        return []

    fetched_at = utc_now_iso()
    rows: list[dict[str, Any]] = []
    setups = 0

    for asset in assets:
        result = analyse(db, asset, run_date, timeframe)
        setups += 1 if result.setup_detected else 0
        rows.append(
            {
                "run_date": run_date,
                "base_asset": asset,
                "setup_detected": 1 if result.setup_detected else 0,
                "setup_type": result.setup_type,
                "invalidation_price": result.invalidation_price,
                "trendline_slope": result.trendline.slope if result.trendline else None,
                "trendline_touches": result.trendline.touches if result.trendline else None,
                "break_confirmed": 1 if result.break_event else 0,
                "retest_confirmed": (
                    1 if (result.retest is not None and result.retest.held) else 0
                ),
                "fvg_count": len(result.fvgs),
                "order_block_count": len(result.order_blocks),
                "risk_flags": json_dump(result.risk_flags),
                "depth_2pct_usd": result.depth_2pct_usd,
                "funding_pctile": result.funding_pctile,
                "oi_change_pctile": result.oi_change_pctile,
                "fetched_at_utc": fetched_at,
            }
        )

    upsert(db, "layer3_result", rows)
    log.info(
        "layer3_complete",
        run_date=run_date,
        assets=len(assets),
        setups=setups,
        timeframe=timeframe,
        note="a setup is an observation about structure, not a recommendation",
    )
    return rows


def run_layer3_for_ranked(run_date: str, top_n: int | None = None, timeframe: str = "1W") -> dict[str, Any]:
    """Convenience entry point for the CLI: analyse today's ranked assets."""
    limit = top_n if top_n is not None else get_config().thresholds.journal.top_n_to_journal
    with get_db() as db:
        assets = [
            r["base_asset"]
            for r in db.query(
                "SELECT base_asset FROM layer2_result WHERE run_date = ? ORDER BY rank LIMIT ?",
                (run_date, limit),
            )
        ]
        rows = run_layer3(db, run_date, assets, timeframe)
    return {
        "run_date": run_date,
        "analysed": len(rows),
        "setups": sum(r["setup_detected"] for r in rows),
        "with_invalidation": sum(1 for r in rows if r["invalidation_price"] is not None),
        "flagged": sum(1 for r in rows if r["risk_flags"] not in (None, "[]")),
    }


__all__ = [
    "BreakEvent",
    "Layer3Result",
    "MIN_BARS",
    "RetestEvent",
    "Trendline",
    "Zone",
    "analyse",
    "compute_invalidation",
    "cvd_divergence",
    "detect_break",
    "detect_descending_trendline",
    "detect_fvg",
    "detect_order_block",
    "detect_retest",
    "funding_percentile",
    "load_bars",
    "oi_change_percentiles",
    "resample",
    "run_layer3",
    "run_layer3_for_ranked",
    "spot_depth_2pct",
    "swing_highs",
]
