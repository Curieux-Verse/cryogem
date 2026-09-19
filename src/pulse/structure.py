"""
# WHY: ------------------------------------------------------------------------
# F5 / F6: the 4H and 1H structure state of one asset (D-079).
#
#     structure_state(bars, cfg) -> (state, bars_since, invalidation)
#
# Two readings, and the event beats the trend:
#
#   1. TRENDLINE BREAK (the event). A descending resistance line through at
#      least `trendline_min_touches` swing highs, broken on a CLOSE within the
#      last `max_bars_since_break` bars, and still broken at the newest close
#      -> bull_break. Its mirror -- an ASCENDING support line through swing lows
#      broken on a close -> bear_break. `invalidation` is the broken line's
#      value at the newest bar: back through it, the event is void.
#   2. EMA STACK (the trend). EMA fast above slow, both rising, close above the
#      fast -> bull_trend; the exact mirror -> bear_trend; anything else is
#      neutral.
#
# Why not reuse Layer 3's detector. The semantics are the same and are kept the
# same (pivot = `window` strictly lower bars each side; a line is valid only if
# no close crossed it between its endpoints; most touches, then most recent;
# a break is a close beyond line * (1 + tolerance); a line that descends
# through zero stops). But layer3_structure reads its swing window, touch count
# and tolerance from `thresholds.layer3` (tuned for 3D/1W bars), requires
# MIN_BARS=30 resampled daily bars, formats DATES from the index, and imports
# the database layer. Pulse needs the same geometry parameterised by
# `PulseStructure` and free of I/O, so the geometry is restated here, pure.
#
# Two refinements over Layer 3, both deliberate:
#   * A break must still hold at the newest close. Layer 3 reports a setup and
#     leaves the reader to judge a failed break; an hourly SCORE cannot, and a
#     break that closed back inside the line is a failed break, not an event.
#   * When a bull and a bear break are both live, the more recent one wins; on
#     the same bar the bearish reading wins (a risk-first tie-break).
#
# Too little history RAISES rather than answering "neutral": neutral scores 50,
# and D-075 says missing data must score nothing. Call `min_structure_bars`
# first, as features.compute_features does.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.config import PulseStructure
from src.pulse.contract import BEAR_BREAK, BEAR_TREND, BULL_BREAK, BULL_TREND, NEUTRAL

#: "Rising" / "falling" EMA = higher / lower than this many bars earlier. One
#: bar is noise at 1H; three bars is 3h at 1H and 12h at 4H.
SLOPE_BARS = 3


def min_structure_bars(cfg: PulseStructure) -> int:
    """Fewest bars structure_state will answer on.

    ema_slow + ema_fast: the slow EMA (seeded at the first close, adjust=False)
    has run a full fast span past its own span, so the seed's weight in it is
    about 6% for 20/50. Fewer bars and the "stack" is mostly the seed.
    """
    return int(cfg.ema_slow + cfg.ema_fast)


@dataclass(frozen=True)
class Line:
    """A fitted line in bar-position space: value = slope * bar + intercept."""

    slope: float
    intercept: float
    touches: int
    first_bar: int
    last_bar: int

    def value_at(self, bar: int | np.ndarray) -> float | np.ndarray:
        return self.slope * bar + self.intercept


# ==============================================================================
# Pivots
# ==============================================================================
def swing_highs(high: np.ndarray, window: int) -> list[int]:
    """Positions with `window` bars strictly lower on BOTH sides (Layer 3 rule).

    The last `window` bars can never be pivots: a high with nothing after it is
    not yet known to be a high.
    """
    values = np.asarray(high, dtype=float)
    out: list[int] = []
    for i in range(window, len(values) - window):
        left = values[i - window : i]
        right = values[i + 1 : i + 1 + window]
        if values[i] > left.max() and values[i] > right.max():
            out.append(i)
    return out


def swing_lows(low: np.ndarray, window: int) -> list[int]:
    """Mirror of swing_highs: `window` bars strictly HIGHER on both sides."""
    return swing_highs(-np.asarray(low, dtype=float), window)


# ==============================================================================
# Lines
# ==============================================================================
def descending_resistance(
    high: np.ndarray, close: np.ndarray, window: int, min_touches: int, tolerance: float
) -> Line | None:
    """Layer 3's descending-line fit, restated pure (see module header)."""
    return _fit(np.asarray(high, float), np.asarray(close, float), window, min_touches,
                tolerance, bullish_line=True)


def ascending_support(
    low: np.ndarray, close: np.ndarray, window: int, min_touches: int, tolerance: float
) -> Line | None:
    """Mirror: an ascending line through swing lows that no close fell through."""
    return _fit(np.asarray(low, float), np.asarray(close, float), window, min_touches,
                tolerance, bullish_line=False)


def _fit(
    extreme: np.ndarray,
    close: np.ndarray,
    window: int,
    min_touches: int,
    tolerance: float,
    bullish_line: bool,
) -> Line | None:
    """bullish_line=True: descending resistance over highs. False: ascending support over lows."""
    pivots = swing_highs(extreme, window) if bullish_line else swing_lows(extreme, window)
    if len(pivots) < min_touches:
        return None
    positions = np.arange(len(extreme))
    best: Line | None = None
    for a in range(len(pivots) - 1):
        for b in range(a + 1, len(pivots)):
            i, j = pivots[a], pivots[b]
            if bullish_line and not extreme[j] < extreme[i]:
                continue  # resistance must descend
            if not bullish_line and not extreme[j] > extreme[i]:
                continue  # support must ascend
            slope = (extreme[j] - extreme[i]) / (j - i)
            intercept = extreme[i] - slope * i
            line = slope * positions + intercept
            seg = slice(i, j + 1)
            # Valid only while no CLOSE crossed it between its endpoints.
            if bullish_line and np.any(close[seg] > line[seg] * (1 + tolerance)):
                continue
            if not bullish_line and np.any(close[seg] < line[seg] * (1 - tolerance)):
                continue
            touches = sum(
                1 for p in pivots
                if i <= p <= j and abs(extreme[p] - line[p]) <= abs(line[p]) * tolerance
            )
            if touches < min_touches:
                continue
            cand = Line(float(slope), float(intercept), touches, i, j)
            if best is None or (cand.touches, cand.last_bar) > (best.touches, best.last_bar):
                best = cand
    return best


def first_break(close: np.ndarray, line: Line, tolerance: float, upward: bool) -> int | None:
    """Position of the first CLOSE beyond the line after its last touch.

    upward=True: close > line * (1 + tol) (resistance broken). False: close <
    line * (1 - tol) (support broken). A line at or below zero stops the scan,
    as in Layer 3: beyond that point "above the line" means nothing.
    """
    for bar in range(line.last_bar + 1, len(close)):
        value = float(line.value_at(bar))
        if value <= 0:
            return None
        if upward and close[bar] > value * (1 + tolerance):
            return bar
        if not upward and close[bar] < value * (1 - tolerance):
            return bar
    return None


# ==============================================================================
# State
# ==============================================================================
def _ema(close: pd.Series, span: int) -> np.ndarray:
    return close.ewm(span=span, adjust=False).mean().to_numpy(dtype=float)


def _ema_state(close: pd.Series, cfg: PulseStructure) -> tuple[str, int | None]:
    fast = _ema(close, cfg.ema_fast)
    slow = _ema(close, cfg.ema_slow)
    last = len(close) - 1
    c = float(close.iloc[-1])
    k = SLOPE_BARS
    fast_up, fast_dn = fast[last] > fast[last - k], fast[last] < fast[last - k]
    slow_up, slow_dn = slow[last] > slow[last - k], slow[last] < slow[last - k]

    # The stack must hold on one side through the whole slope window: a series
    # chopping around flat flips fast/slow every bar, and "rising over k bars"
    # then reads float noise as a trend.
    window = fast[last - k : last + 1] - slow[last - k : last + 1]
    held_above, held_below = bool(np.all(window > 0)), bool(np.all(window < 0))

    if held_above and fast_up and slow_up and c > fast[last]:
        state = BULL_TREND
    elif held_below and fast_dn and slow_dn and c < fast[last]:
        state = BEAR_TREND
    else:
        return NEUTRAL, None

    # Bars since the stack last flipped to its current side. Counted from the
    # bar after warm-up (ema_slow), so a stack that never flipped reports the
    # full evaluable span: a lower bound, not an exact age.
    sign = np.sign(fast - slow)
    start = min(cfg.ema_slow, last)
    since = 0
    for pos in range(last, start, -1):
        if sign[pos - 1] != sign[last]:
            break
        since += 1
    return state, since


def structure_state(
    bars: pd.DataFrame, cfg: PulseStructure
) -> tuple[str, int | None, float | None]:
    """(state in contract.STATES, bars_since, invalidation) for CLOSED bars.

    `bars` needs high, low, close, oldest first. The EMA stack reads every bar
    given (more history = better converged EMAs); the trendline search reads
    only the last `lookback_bars`. `bars_since` is 0 when the event happened on
    the newest bar. `invalidation` is set for breaks only.

    Raises ValueError with fewer than min_structure_bars(cfg) bars.
    """
    need = min_structure_bars(cfg)
    if len(bars) < need:
        raise ValueError(f"structure_state needs >= {need} bars, got {len(bars)}")
    frame = bars[["high", "low", "close"]].astype(float)
    if frame.isna().any().any():
        frame = frame.dropna()
        if len(frame) < need:
            raise ValueError(f"structure_state needs >= {need} complete bars, got {len(frame)}")

    recent = frame.iloc[-cfg.lookback_bars :]
    high = recent["high"].to_numpy()
    low = recent["low"].to_numpy()
    close = recent["close"].to_numpy()
    last = len(close) - 1
    tol = cfg.trendline_tolerance_pct

    events: list[tuple[int, int, str, float]] = []  # (break bar, bear-first key, state, inval)
    res = descending_resistance(high, close, cfg.swing_window_bars, cfg.trendline_min_touches, tol)
    if res is not None:
        bar = first_break(close, res, tol, upward=True)
        if bar is not None and last - bar <= cfg.max_bars_since_break:
            line_now = float(res.value_at(last))
            if line_now > 0 and close[last] > line_now:  # the break still holds
                events.append((bar, 0, BULL_BREAK, line_now))
    sup = ascending_support(low, close, cfg.swing_window_bars, cfg.trendline_min_touches, tol)
    if sup is not None:
        bar = first_break(close, sup, tol, upward=False)
        if bar is not None and last - bar <= cfg.max_bars_since_break:
            line_now = float(sup.value_at(last))
            if line_now > 0 and close[last] < line_now:
                events.append((bar, 1, BEAR_BREAK, line_now))
    if events:
        bar, _, state, invalidation = max(events)  # most recent; same bar -> bear
        return state, last - bar, invalidation

    state, since = _ema_state(frame["close"], cfg)
    return state, since, None


__all__ = [
    "Line",
    "SLOPE_BARS",
    "ascending_support",
    "descending_resistance",
    "first_break",
    "min_structure_bars",
    "structure_state",
    "swing_highs",
    "swing_lows",
]
