"""
# WHY: ------------------------------------------------------------------------
# The shapes the Pulse pipeline passes between its stages, pinned in one place:
#
#     data.py      fetch + store      -> MarketWindow
#     features.py  MarketWindow       -> feature frame   (FEATURE_COLUMNS)
#     score.py     feature frame      -> pulse frame     (PULSE_COLUMNS)
#     run.py       pulse frame        -> pulse_result, journal, alerts, pulse.json
#
# LOOK-AHEAD RULE. Everything in a MarketWindow is visible at `as_of`:
#   * a 1H bar is included only when it has CLOSED: open time + 1h <= as_of;
#   * an OI or funding observation only when its timestamp <= as_of.
# A feature stamped `as_of` therefore uses nothing the market had not already
# printed. data.py enforces it on the way in and features.py asserts it.
#
# A 4H bar is resampled from 1H bars, aligned to 00/04/08/12/16/20 UTC as
# Binance aligns them, and exists only when all four of its 1H bars have closed.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

#: 1H bar columns. Index: bar OPEN time, tz-aware UTC DatetimeIndex, sorted,
#: unique. quote_volume and taker_buy_quote are in the quote asset (USDT).
BAR_COLUMNS: tuple[str, ...] = (
    "open",
    "high",
    "low",
    "close",
    "quote_volume",
    "taker_buy_quote",
    "trades",
)

#: Open-interest columns. Index: observation time, tz-aware UTC. oi_contracts
#: is Binance's sumOpenInterest (base units) -- the measure Pulse reasons
#: about; oi_usd (sumOpenInterestValue) moves with price and is context only.
OI_COLUMNS: tuple[str, ...] = ("oi_contracts", "oi_usd")

BAR_1H = pd.Timedelta(hours=1)
BAR_4H = pd.Timedelta(hours=4)


@dataclass
class MarketWindow:
    """Everything Pulse may see at `as_of`, for the assets it scores."""

    #: The cutoff, tz-aware UTC, on the hour. Nothing at or after it is visible
    #: except OI/funding stamped exactly at it.
    as_of: datetime
    #: base_asset -> 1H bars (BAR_COLUMNS), closed bars only.
    bars: dict[str, pd.DataFrame] = field(default_factory=dict)
    #: base_asset -> 1H open interest (OI_COLUMNS).
    oi: dict[str, pd.DataFrame] = field(default_factory=dict)
    #: base_asset -> settled funding rates, indexed by funding time.
    funding: dict[str, pd.Series] = field(default_factory=dict)
    #: base_asset -> Binance USDT-M perp symbol.
    symbols: dict[str, str] = field(default_factory=dict)
    #: The assets Pulse scores: the newest Layer 1 survivors.
    survivors: list[str] = field(default_factory=list)
    #: Always fetched for vs-benchmark returns, even if it failed Layer 1.
    benchmark: str = "BTC"
    #: base_asset -> reason, for assets whose fetch failed (never silently dropped).
    fetch_errors: dict[str, str] = field(default_factory=dict)


# -- structure states (F5 4H, F6 1H) ---------------------------------------------
BULL_BREAK = "bull_break"   # close above a descending trendline, within max_bars_since_break
BULL_TREND = "bull_trend"   # EMA fast > EMA slow, both rising, close above fast
NEUTRAL = "neutral"
BEAR_TREND = "bear_trend"   # mirror of bull_trend
BEAR_BREAK = "bear_break"   # close below an ASCENDING support line, within max_bars_since_break
STATES: tuple[str, ...] = (BULL_BREAK, BULL_TREND, NEUTRAL, BEAR_TREND, BEAR_BREAK)
#: Component score of each state (fixed, like emissions_trajectory).
STATE_SCORE: dict[str, float] = {
    BULL_BREAK: 100.0,
    BULL_TREND: 75.0,
    NEUTRAL: 50.0,
    BEAR_TREND: 25.0,
    BEAR_BREAK: 0.0,
}

# -- OI x price x flow quadrant (F4; D-074: a conditioner, never alone) ----------
OI_CONFIRM_LONG = "confirm_long"          # price up, OI up, flow buy-dominant
OI_SHORT_COVERING = "short_covering"      # price up, OI down
OI_NEW_SHORTS = "new_shorts"              # price down, OI up, flow sell-dominant
OI_LONG_LIQUIDATION = "long_liquidation"  # price down, OI down (possible exhaustion)
OI_LEVERAGE_BUILD = "leverage_build"      # price flat, OI up a lot (the TRB shape)
OI_NEUTRAL = "neutral"                    # anything else, e.g. up + OI up + selling
OI_QUADRANTS: tuple[str, ...] = (
    OI_CONFIRM_LONG,
    OI_SHORT_COVERING,
    OI_NEW_SHORTS,
    OI_LONG_LIQUIDATION,
    OI_LEVERAGE_BUILD,
    OI_NEUTRAL,
)
#: Component score of each quadrant. Only confirm_long scores above neutral,
#: and it requires price AND flow to agree already: OI confirms, never leads.
OI_QUADRANT_SCORE: dict[str, float] = {
    OI_CONFIRM_LONG: 100.0,
    OI_SHORT_COVERING: 50.0,
    OI_NEUTRAL: 50.0,
    OI_LEVERAGE_BUILD: 25.0,
    OI_NEW_SHORTS: 0.0,
    OI_LONG_LIQUIDATION: 0.0,
}

# -- risk flags ---------------------------------------------------------------------
FLAG_CROWDING = "crowding"                # R1: funding AND OI 24h at own extremes
FLAG_LEVERAGE_NO_MOVE = "leverage_no_move"  # R2: OI 24h extreme, |return| < 1 sigma
FLAG_THIN_BOOK = "thin_book"              # R3: excluded from Pulse
FLAG_INSUFFICIENT = "insufficient_history"  # fewer than min_bars_1h closed bars
RISK_FLAGS: tuple[str, ...] = (FLAG_CROWDING, FLAG_LEVERAGE_NO_MOVE)
EXCLUDING_FLAGS: tuple[str, ...] = (FLAG_THIN_BOOK, FLAG_INSUFFICIENT)

#: features.compute_features(window, cfg) -> DataFrame indexed by base_asset
#: (every survivor, in window.survivors order), with exactly these columns.
#: Numeric columns are float with NaN for "could not measure", never 0.
FEATURE_COLUMNS: tuple[str, ...] = (
    "n_bars_1h",            # closed 1H bars available
    "last_close",           # close of the newest closed 1H bar
    "quote_volume_24h",     # USDT, last 24 closed 1H bars
    "ret_1h", "ret_4h", "ret_24h", "ret_7d",  # simple returns on closes
    "flow_4h", "flow_24h",  # F1: (2*sum taker_buy - sum vol) / sum vol, in [-1, 1]
    "flow_24h_z",           # F1 vs the asset's own rolling-24h history
    "thrust_1h", "thrust_4h",  # F2: log(vol / median prior N bars) * sign(bar return)
    "vamom_24h", "vamom_7d",   # F3: return / (1H log-return std * sqrt(n))
    "vamom_24h_z",
    "oi_chg_4h", "oi_chg_24h",  # F4 inputs: fractional change in oi_contracts
    "oi_quadrant_4h", "oi_quadrant_24h",  # str, OI_QUADRANTS
    "state_4h", "bars_since_4h",  # F5: str STATES; bars since the event/cross
    "state_1h", "bars_since_1h",  # F6
    "invalidation_4h",      # the broken line's value at the last closed 4H bar, if a break
    "funding_last", "funding_own_pctile",  # R1 input, 0-100 vs own history
    "oi24_own_pctile",      # R1/R2 input: this 24h OI change vs own history, 0-100
    "flags",                # list[str] from RISK_FLAGS + EXCLUDING_FLAGS
)

#: score.score_pulse(features, gem_ranks, cfg) -> DataFrame indexed by
#: base_asset with these columns. Excluded assets (EXCLUDING_FLAGS) keep a row
#: with score NaN and rank NaN, so the page can say why they are absent.
PULSE_COLUMNS: tuple[str, ...] = (
    "score",          # 0-100 after penalties; NaN if excluded
    "rank",           # 1 = best among scored; NaN if excluded
    "components",     # dict component -> 0-100 or None (PulseWeights keys)
    "coverage",       # weight measured / weight live, 0-1 (D-075)
    "state_4h", "state_1h", "oi_quadrant",  # oi_quadrant = the 24H reading
    "flags",          # list[str]
    "penalty",        # the multiplier applied (1.0 = none)
    "gem_rank",       # newest Layer 2 rank, or None
    "aligned",        # bool
)

#: pulse.json, written to data/public/pulse.json (never committed; baked by
#: build-site from pulse_result). The dashboard's Pulse tab reads only this.
PULSE_JSON_SCHEMA_VERSION = 1
PULSE_JSON_EXAMPLE = {
    "schema_version": 1,
    "status": "ok",                 # "ok" | "unavailable" (no pulse_result in 3h)
    "as_of_utc": "2026-09-19T14:00:00Z",   # the hour scored
    "generated_at_utc": "2026-09-19T14:04:10Z",
    "score_version": "pulse-v1",
    "universe_size": 158,           # survivors considered
    "scored": 131,                  # survivors with a score
    "aligned": ["ONT"],             # ALIGNED names, best Pulse first
    "risers": [{"asset": "ONT", "delta": 12.4, "rank": 3, "prev_rank": 21}],
    "assets": [
        {
            "asset": "ONT",
            "score": 81.2,
            "rank": 1,
            "prev_rank": 4,          # rank at the previous scored hour, or null
            "delta": 6.3,            # score change vs the previous hour, or null
            "gem_rank": 3,
            "aligned": True,
            "state_4h": "bull_break",
            "bars_since_4h": 2,
            "state_1h": "bull_trend",
            "oi_quadrant": "confirm_long",
            "flags": [],
            "coverage": 1.0,
            "components": {
                "flow": 88.0, "structure_4h": 100.0, "momentum": 71.5,
                "thrust": 90.1, "oi_confirm": 100.0, "structure_1h": 75.0,
            },
            "features": {
                "flow_24h": 0.12, "flow_4h": 0.2, "ret_4h": 0.031, "ret_24h": 0.084,
                "oi_chg_24h": 0.06, "thrust_4h": 1.1, "quote_volume_24h": 4.1e7,
                "invalidation_4h": 0.1234,
            },
            "spark_1h": [0.11, 0.112],    # last 48 closed 1H closes
            "flow_1h": [0.05, -0.02],     # per-bar taker flow, same 48 bars
            "spark_4h": [0.10, 0.11],     # last 42 closed 4H closes (7 days)
            "file": "assets/ONT.json",    # the Gem detail page
        }
    ],
    "excluded": [{"asset": "XYZ", "reason": "thin_book"}],
}

__all__ = [
    "BAR_1H",
    "BAR_4H",
    "BAR_COLUMNS",
    "EXCLUDING_FLAGS",
    "FEATURE_COLUMNS",
    "MarketWindow",
    "OI_COLUMNS",
    "OI_QUADRANTS",
    "OI_QUADRANT_SCORE",
    "PULSE_COLUMNS",
    "PULSE_JSON_EXAMPLE",
    "PULSE_JSON_SCHEMA_VERSION",
    "RISK_FLAGS",
    "STATES",
    "STATE_SCORE",
]
