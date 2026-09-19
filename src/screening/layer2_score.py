"""
# WHY: ------------------------------------------------------------------------
# Layer 2: rank the survivors. Applied ONLY to assets that cleared Layer 1.
#
# THE CARDINAL RULE:
#
#     Percentile-rank every metric WITHIN today's surviving universe.
#     Never use an absolute threshold in Layer 2.
#
# Two reasons, and the second is the one people miss:
#   * Absolute cutoffs break the moment market regime changes. "Revenue above
#     $10M" means something different in a bull market and a bear one.
#   * Funding-rate research found predictive power limited for single-asset
#     prediction (explaining ~12.5% of 7-day price variation, declining after)
#     but noted the data is materially more useful applied CROSS-SECTIONALLY.
#     The signal is in the ordering, not the level.
#
# MISSING DATA SCORES NOTHING (D-075). The seven weights sum to 110
# deliberately and are normalised over the blocks LIVE in the run. A block is
# live when it is measured for at least `live_min_assets` survivors; a live
# block carries its weight for EVERY asset, and an asset with no reading on it
# earns 0 there. A block measured for (almost) nobody -- attention, while
# LunarCrush is unpaid -- is dark, and drops out for everyone at once.
#
# This replaced per-asset renormalisation, which redistributed an unmeasured
# block's weight across whatever the asset DID have. That rewarded missing
# data: a coin measured on two strong blocks outranked one measured on five,
# and on 2026-09-19 ten of the top fifteen were scored on supply and drawdown
# alone. The owner's decision, verbatim in intent: "Don't let missing data
# score anything, it's just manipulating and inflating a coin's potential."
# A neutral fill at 50 was proposed and rejected for the same reason -- 50 is
# still a score for something nobody measured.
#
# The None/zero distinction survives where it is honest: a block with nothing
# measured for an asset is still None in the row (so `coverage` can say how
# much of the score rests on data), and it is never written as a measured 0.
# It simply earns nothing at the composite. The same rule runs one level down,
# inside each block, metric by metric (_combine).
#
# Dark sources are dropped for everyone rather than zeroed for everyone, so a
# universally dark block neither reorders the ranking nor deflates every
# absolute score by its weight.
#
# REDUNDANCY. After 30 days, correlate the seven block scores. Any pair above
# |rho| 0.8 is measuring one thing twice: applying iterative factor selection to
# 36 crypto return-predictive factors found two to three factors eliminated all
# significant portfolio alphas. A seven-block score with high internal
# correlation is a three-block score wearing a costume. Momentum and drawdown
# are expected to pull against each other (D-077); that report settles it.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from src.config import get_config
from src.db.connection import Database
from src.db.writes import json_dump, upsert
from src.events.features import compute_all
from src.logging_setup import get_logger
from src.timeutil import add_days, utc_now_iso

log = get_logger("screening.layer2")

BLOCKS = (
    "fundamental", "supply", "momentum", "sector", "events", "attention", "drawdown"
)

#: The method every row written before D-076 was produced by. A NULL
#: score_version reads as this, so old and new cohorts never blend.
LEGACY_SCORE_VERSION = "gem-v1"

#: Fewest assets past the attention gate before any of them is ranked. One asset
#: past the gate ranked first of one and scored 100 (D-059). The live rule
#: (D-075, `live_min_assets`) is the stricter floor on any universe of five or
#: more; this stays as the percentile's own guard for smaller ones.
MIN_GATED_EXTREMES = 3

#: Momentum windows (D-077), in daily bars. Definitional, like the component
#: weights: changing one changes what the metric IS, so it is a new
#: score_version, not a tuning knob.
FLOW_BARS = 7
VAMOM_SHORT = (7, 30)   # (return horizon, volatility window)
VAMOM_LONG = (30, 60)
TREND_EMA_FAST, TREND_EMA_SLOW = 20, 50
TREND_MIN_BARS = 60
MOMENTUM_LOOKBACK_DAYS = 90
#: Share of a volatility window's daily returns that must exist. One missed
#: collection day must not blank an asset for two months; a young listing
#: with half a window must not be scored on it.
VOL_MIN_SHARE = 0.8


def live_floor(n_assets: int, live_min_assets: int) -> int:
    """Measured assets a block or metric needs to count as live (D-075).

    Below `live_min_assets` survivors the floor is the whole universe: with
    three assets, a block measured for one of them is not a cross-section.
    """
    return max(1, min(live_min_assets, n_assets))


def composite(
    blocks: pd.DataFrame, weights: dict[str, float], live_min_assets: int
) -> tuple[pd.Series, pd.Series, pd.Series, list[str]]:
    """Total score, coverage, measured-live-block count, and the live blocks.

    D-075. A block measured for at least the live floor is live; every asset
    carries its weight, and a missing reading contributes 0. Blocks below the
    floor drop out for everyone. An asset measured on NO live block has a NaN
    total: it is unscorable and omitted, never written as a measured zero.
    """
    floor = live_floor(len(blocks), live_min_assets)
    counts = blocks.notna().sum()
    live = [b for b in blocks.columns if counts[b] >= floor and counts[b] > 0]
    nan = pd.Series(np.nan, index=blocks.index, dtype=float)
    if not live:
        return nan, nan.copy(), pd.Series(0, index=blocks.index), []

    weight_vector = pd.Series({b: float(weights[b]) for b in live})
    live_frame = blocks[live]
    measured = live_frame.notna()
    denominator = float(weight_vector.sum())
    total = (live_frame.fillna(0.0) * weight_vector).sum(axis=1) / denominator
    coverage = (measured * weight_vector).sum(axis=1) / denominator
    total = total.where(coverage > 0).astype(float)
    return total, coverage.astype(float), measured.sum(axis=1), live


def cross_sectional_percentile(
    series: pd.Series, higher_is_better: bool = True, min_count: int = 1
) -> pd.Series:
    """Rank 0-100 within today's universe.

    NaN-safe and deliberately so: a NaN stays NaN and must NOT become 0. An
    asset we could not measure is not an asset that measured badly, and
    collapsing the two is the most common way a scoring layer quietly develops
    a bias against anything with incomplete data.
    """
    numeric = pd.to_numeric(series, errors="coerce")
    if numeric.notna().sum() < min_count:
        # Too few measured values for an ordering to mean anything.
        return pd.Series(np.nan, index=series.index, dtype=float)
    if not higher_is_better:
        numeric = -numeric
    return numeric.rank(pct=True, na_option="keep") * 100.0


def _ema(values: np.ndarray, span: int) -> np.ndarray:
    """Exponential moving average seeded on the first value (pandas adjust=False)."""
    return pd.Series(values).ewm(span=span, adjust=False).mean().to_numpy()


def momentum_features(bars: pd.DataFrame, run_date: str) -> pd.DataFrame:
    """Raw momentum inputs per asset, from COMPLETE daily bars only (D-077).

    Which bars are complete. Binance daily klines open at 00:00 UTC and close
    at 23:59:59.999. The screen runs at ~03:10 UTC on `run_date`, after
    collect-daily, and the klines collector never writes the bar that is still
    forming (transform drops `day >= today`). So the newest complete bar at
    screen time is run_date - 1, and it is always on file by then. The
    `run_date` row itself is either CoinGecko's 03:10 point-in-time price (a
    close-only snapshot, not a bar) or, on a re-screen of a past date, a kline
    that closed AFTER the screen would have run. Both are excluded: bars are
    read strictly before run_date, and from `binance_klines` only (D-047).

    Every metric also requires the run_date - 1 bar itself. An asset whose
    newest bar is older has a stale series, and momentum on it would describe
    last week.

    Metrics, all higher-is-better before percentile ranking:
      * flow_7d   (2*taker buy - volume) / volume over the last 7 bars; every
                  bar must carry taker buy volume. Range -1..1.
      * vamom_7d  7-day return / (std of 30 daily log returns * sqrt 7).
      * vamom_30d 30-day return / (std of 60 daily log returns * sqrt 30).
      * trend_1d  EMA20/EMA50 state on closes: 100 bull (close > EMA20 > EMA50,
                  EMA20 rising), 0 bear (the mirror), else 50. >= 60 bars.

    Research basis:
      * Liu, Tsyvinski & Wu, "Common Risk Factors in Cryptocurrency", Journal
        of Finance 2022: momentum is one of three factors pricing the crypto
        cross-section; the effect is strongest at 1-4 weeks.
      * Fieberg et al., "A Trend Factor for the Cross Section of
        Cryptocurrency Returns", JFQA 2025 (CTREND): price and volume trend
        signals across horizons predict returns over 3,000+ coins, survive
        costs, and hold in large, liquid coins.
      * Anastasopoulos, Gradojevic, Liu, Maynard & Tsiakas, "Order Flow and
        Cryptocurrency Returns", Journal of Financial Markets 2026: signed
        order flow predicts the cross-section of 82 coins, with a permanent
        price effect. Kline taker-buy volume is aggressor-flagged by Binance
        itself, which is what makes bar-level flow honest here (revising D-015
        for bars; tick-level CVD stays out of scope).
    Volatility scaling follows the risk-adjusted momentum convention: a 20%
    move in a coin that routinely moves 20% a day is not the same signal as
    the same move in one that moves 2%.
    """
    columns = ["flow_7d", "vamom_7d", "vamom_30d", "trend_1d"]
    if bars.empty:
        return pd.DataFrame(columns=columns, dtype=float)

    last = add_days(run_date, -1)
    calendar = [add_days(last, -offset) for offset in range(MOMENTUM_LOOKBACK_DAYS - 1, -1, -1)]
    out: dict[str, dict[str, float]] = {}

    for asset, group in bars.groupby("base_asset"):
        series = (
            group.drop_duplicates("snapshot_date", keep="last")
            .set_index("snapshot_date")
            .reindex(calendar)
        )
        close = pd.to_numeric(series["close_usd"], errors="coerce")
        close = close.where(close > 0)
        row = dict.fromkeys(columns, np.nan)
        if pd.isna(close.iloc[-1]):
            out[str(asset)] = row
            continue

        volume = pd.to_numeric(series["volume_usd"], errors="coerce").iloc[-FLOW_BARS:]
        taker = pd.to_numeric(series["taker_buy_usd"], errors="coerce").iloc[-FLOW_BARS:]
        if volume.notna().all() and taker.notna().all() and volume.sum() > 0:
            row["flow_7d"] = float((2.0 * taker.sum() - volume.sum()) / volume.sum())

        log_returns = np.log(close).diff()
        for key, (horizon, window) in (("vamom_7d", VAMOM_SHORT), ("vamom_30d", VAMOM_LONG)):
            start = close.iloc[-1 - horizon]
            recent = log_returns.iloc[-window:].dropna()
            if pd.isna(start) or len(recent) < math.ceil(VOL_MIN_SHARE * window):
                continue
            sigma = float(recent.std(ddof=1))
            if not sigma > 0:
                # A flat series has no volatility to scale by; a ratio over
                # zero is not a measurement.
                continue
            simple = float(close.iloc[-1] / start - 1.0)
            row[key] = simple / (sigma * math.sqrt(horizon))

        closes = close.dropna().to_numpy()
        if len(closes) >= TREND_MIN_BARS:
            fast = _ema(closes, TREND_EMA_FAST)
            slow = _ema(closes, TREND_EMA_SLOW)
            price, f_now, f_prev, s_now = closes[-1], fast[-1], fast[-2], slow[-1]
            if price > f_now > s_now and f_now > f_prev:
                row["trend_1d"] = 100.0
            elif price < f_now < s_now and f_now < f_prev:
                row["trend_1d"] = 0.0
            else:
                row["trend_1d"] = 50.0
        out[str(asset)] = row

    return pd.DataFrame.from_dict(out, orient="index", columns=columns).astype(float)


def load_momentum_bars(db: Database, run_date: str, survivors: list[str]) -> pd.DataFrame:
    """One query: the survivors' complete kline bars over the lookback window."""
    if not survivors:
        return pd.DataFrame()
    placeholders = ",".join("?" for _ in survivors)
    rows = db.query(
        "SELECT base_asset, snapshot_date, close_usd, volume_usd, taker_buy_usd "
        "FROM price_daily WHERE source = 'binance_klines' "
        "AND snapshot_date >= ? AND snapshot_date <= ? "
        f"AND base_asset IN ({placeholders})",
        [add_days(run_date, -MOMENTUM_LOOKBACK_DAYS), add_days(run_date, -1), *survivors],
    )
    return pd.DataFrame(rows)


class Layer2Scorer:
    """Builds the cross-sectional score for one run date."""

    def __init__(self, run_date: str) -> None:
        cfg = get_config()
        self.run_date = run_date
        self.weights = cfg.thresholds.layer2_weights.as_dict()
        self.t = cfg.thresholds.layer2
        self.sectors = cfg.sectors
        self.log = log.bind(run_date=run_date)

    # -- blocks --------------------------------------------------------------
    def score_fundamental(self, df: pd.DataFrame) -> tuple[pd.Series, dict[str, pd.Series]]:
        """Revenue growth, fee acceleration, cheapness, TVL. Weight 30.

        The reference case is VVV: revenue moving from a ~$70M to a ~$100M
        annualised run-rate inside one month, visible here before it was
        visible in price.

        D-075. `active_addresses` is gone: `active_addresses_24h` has no writer
        (DefiLlama writes a literal None), so it was a weight-1.5 metric that
        was None for every asset -- the D-072 pattern. The column stays.
        """
        metrics: dict[str, pd.Series] = {}

        growth = (df["revenue_30d_usd"] - df["revenue_prev_30d_usd"]) / df[
            "revenue_prev_30d_usd"
        ].abs().replace(0, np.nan)
        metrics["revenue_growth_30d"] = cross_sectional_percentile(growth)

        # Fee acceleration: last 7 days against the trailing 30-day weekly rate.
        acceleration = df["fees_7d_usd"] / (df["fees_30d_usd"] / 4.0).replace(0, np.nan)
        metrics["fee_acceleration"] = cross_sectional_percentile(acceleration)

        # Price-to-sales: LOWER is better, so the rank is inverted. Only for
        # positive revenue: a loss-maker's negative ratio ranked as the cheapest
        # asset in the universe (D-059).
        revenue = df["revenue_annualised"]
        ps_ratio = df["market_cap_usd"] / revenue.where(revenue > 0)
        metrics["price_to_sales"] = cross_sectional_percentile(ps_ratio, higher_is_better=False)

        # TVL is a CAPITAL SNAPSHOT, not activity: it rises when prices rise
        # with no new deposits. Included for context, weighted low.
        metrics["tvl"] = cross_sectional_percentile(df["tvl_usd"])

        component_weights = {
            "revenue_growth_30d": 3.0,
            "fee_acceleration": 2.0,
            "price_to_sales": 2.0,
            "tvl": 0.5,
        }
        scores = self._combine(df.index, metrics, component_weights)
        # An asset with no revenue model is unmeasured here: None, not a
        # measured zero. Under D-075 it earns 0 at the composite once the
        # block is live, like any other unmeasured block. The mask can only
        # remove a reading, so it cannot inflate anything.
        scores[df["has_fundamentals"] == 0] = np.nan
        return scores, metrics

    def score_supply(self, df: pd.DataFrame) -> tuple[pd.Series, dict[str, pd.Series]]:
        """Net issuance, its direction, float, and distance from the last cliff. Weight 20.

        Reference case: VVV cut emissions 10M -> 8M -> 6M -> 3M -> 2.5M -> 2M
        per year while burning ~33.87M tokens, about 41.85% of total supply --
        which is low NET issuance, falling. Both halves are measured here.

        D-072. `net_issuance` is circulating-supply growth over the last window,
        annualised, from CoinGecko's own supply history: a burn is negative
        issuance, so burned share needs no metric of its own. Burned and staked
        share were metrics here with no writer anywhere -- permanently None,
        listed beside real ones -- and are gone rather than left looking live.
        """
        metrics: dict[str, pd.Series] = {}

        # LOWER is better: supply growing more slowly than the universe's.
        metrics["net_issuance"] = cross_sectional_percentile(
            df["emissions_annual"], higher_is_better=False
        )
        metrics["emissions_trajectory"] = df["emissions_trajectory"].map(
            {"falling": 100.0, "flat": 50.0, "rising": 0.0}
        )
        metrics["days_since_unlock"] = cross_sectional_percentile(df["days_since_last_major_unlock"])
        metrics["float_ratio"] = cross_sectional_percentile(
            df["circulating_supply"] / df["total_supply"].replace(0, np.nan)
        )

        scores = self._combine(
            df.index,
            metrics,
            {
                "net_issuance": 3.0,
                "emissions_trajectory": 1.5,
                "days_since_unlock": 2.0,
                "float_ratio": 1.5,
            },
        )

        # The inverse signal: every known cliff has passed, so supply pressure
        # structurally ends. Plausibly the strongest single positive feature in
        # the system -- measured in the journal rather than assumed.
        #
        # D-075 audit: the bonus is added to a MEASURED supply score only. NaN
        # plus the bonus stays NaN, so an asset with no live supply metric
        # cannot be lifted to 25 by the flag alone -- the flag is a reading
        # about unlocks, not about issuance or float.
        bonus = df["unlock_overhang_cleared"].astype("boolean").fillna(False).astype(bool)
        scores = (scores + bonus * self.t.unlock_overhang_cleared_bonus).clip(upper=100.0)
        metrics["unlock_overhang_cleared"] = bonus.astype(float) * 100.0
        return scores, metrics

    def score_sector(self, df: pd.DataFrame) -> tuple[pd.Series, dict[str, pd.Series]]:
        """The asset's sector's relative strength against BTC. Weight 10.

        Sector flows dominate price action over weeks and months even when an
        individual project's fundamentals are sound. A good chart in a bleeding
        sector should not rank highly, and this block is what prevents it.
        """
        metrics: dict[str, pd.Series] = {}
        sector_of = df.index.map(self.sectors.sector_of)

        for window in ("7d", "30d"):
            returns = df[f"return_{window}"]
            by_sector = returns.groupby(sector_of).agg(["mean", "count"])
            # A sector index built from one or two members is noise wearing a
            # sector's name.
            usable = by_sector[by_sector["count"] >= self.sectors.min_members_for_index]["mean"]

            # "Relative strength" means relative to the BENCHMARK. With the
            # benchmark missing, the earlier code quietly substituted the
            # cross-sectional mean, which measures something else entirely --
            # a sector's strength against the screened universe, not against
            # BTC -- while still labelling the metric sector_rs. Every sector
            # then scores relative to a pool that BTC is not even in, and on a
            # day when BTC's own return failed to load, the whole block
            # silently changes its meaning.
            #
            # A missing benchmark now yields NaN for the whole window: the
            # metric is unmeasured for everyone, drops out as dark (D-075), and
            # nothing pretends to be a BTC comparison that is not one.
            # From price history, not the survivors: BTC failing Layer 1 must not
            # switch this block off for every asset (D-059). A frame without the
            # attribute falls back to the benchmark's own row.
            benchmark = df.attrs.get(
                f"benchmark_return_{window}",
                returns.get(self.sectors.benchmark_asset, np.nan),
            )
            if pd.isna(benchmark):
                log.warning(
                    "sector_rs_no_benchmark",
                    window=window,
                    benchmark=self.sectors.benchmark_asset,
                    effect="sector RS dark for this window: unmeasured for every asset",
                )
                relative = pd.Series(np.nan, index=usable.index, dtype=float)
            else:
                relative = usable - benchmark
            mapped = pd.Series(sector_of, index=df.index).map(relative)
            # 'unclassified' is not a sector: score it None, never the mean.
            # Under D-075 that None earns 0 once the metric is live: an
            # unmapped coin has no sector evidence, and no longer borrows the
            # block's weight from its other readings (D-073 mapped most of
            # the top-ranked ones).
            mapped[pd.Series(sector_of, index=df.index) == "unclassified"] = np.nan
            metrics[f"sector_rs_{window}"] = cross_sectional_percentile(mapped)

        scores = self._combine(
            df.index, metrics, {"sector_rs_7d": 1.0, "sector_rs_30d": 1.5}
        )
        return scores, metrics

    def score_events(self, df: pd.DataFrame) -> tuple[pd.Series, dict[str, pd.Series]]:
        """Distance from the next cliff, and scheduled catalysts. Weight 10."""
        metrics: dict[str, pd.Series] = {}

        # A null days_to_next_major_unlock has TWO meanings and they are
        # opposites: "we hold a vesting schedule and it has no cliff ahead"
        # (the best case in the block) and "we hold no unlock record at all"
        # (we know nothing). The earlier version filled both with 9999, so an
        # asset we had no supply data for scored the top percentile on unlock
        # distance -- ignorance rewarded as safety, which is exactly the
        # failure mode this system exists to avoid.
        #
        # The comment that justified it claimed L1 had already required event
        # data. It has not: L1_UNLOCK returns _unknown() when has_event_data
        # is false, and an unknown check passes. On the live universe that
        # branch is the common case, not the exception -- so the assumption
        # was load-bearing and wrong.
        #
        # Now only assets with an actual unlock record get the top-of-range
        # treatment. The rest stay NaN and score None on this metric, which
        # earns them 0 on it once it is live (D-075). Cast first: fillna on
        # an object column downcasts silently.
        days = pd.to_numeric(df["days_to_next_major_unlock"], errors="coerce")
        known = df["has_unlock_record"].astype("boolean").fillna(False).astype(bool)
        days = days.where(~(days.isna() & known), 9999.0)
        days = days.where(known, np.nan)
        metrics["days_to_next_unlock"] = cross_sectional_percentile(days)
        # A KNOWN catalyst is evidence; the absence of a known one is not
        # evidence of none. Filled with False, every asset we knew nothing about
        # counted as measured and the block could never be None (D-059).
        catalyst = df["positive_catalyst_30d"].astype("boolean").fillna(False).astype(bool)
        metrics["positive_catalyst"] = pd.Series(np.nan, index=df.index).mask(catalyst, 100.0)
        # D-075 audit: 100-or-NaN was an inflation path. Renormalised, an asset
        # whose ONLY events reading was a known catalyst scored the block 100.
        # Now, once live, a catalyst adds its third of the block to whatever
        # the unlock metric says, and an asset without one earns 0 on it --
        # a bonus, not a whole block. With fewer than live_min_assets known
        # catalysts in the universe the metric is dark like any other: a
        # source that knows of three catalysts is not measuring the universe.

        # The overhang is counted once, in the supply block. It was also a
        # weight-2 metric here and the drawdown interaction: three counts of one
        # flag (D-059).
        scores = self._combine(
            df.index, metrics, {"days_to_next_unlock": 2.0, "positive_catalyst": 1.0}
        )

        # A monitoring tag is a soft exchange warning for elevated-volatility
        # assets and often precedes delisting. It is a measured negative, so it
        # sets the block to zero whatever else is or is not known. D-075: the
        # tag COUNTS as a measurement, so a tagged asset is measured on events
        # (coverage says so) and the block's 0 is a real reading.
        tagged = df["monitoring_tag_active"].astype("boolean").fillna(False).astype(bool)
        metrics["monitoring_tag"] = pd.Series(np.nan, index=df.index).mask(tagged, 0.0)
        scores = scores.mask(tagged, 0.0)
        return scores, metrics

    def score_attention(self, df: pd.DataFrame) -> tuple[pd.Series, dict[str, pd.Series]]:
        """Social attention, z-scored and GATED. Weight 10.

        The gate matters as much as the metric: sentiment's predictive power
        concentrates at extreme states, so an asset sitting in the middle of its
        own distribution contributes nothing rather than contributing noise.

        D-075 audit: a gated-out asset now earns 0 on the z metric once it is
        live, which is exactly "contributes nothing". The live floor
        (live_min_assets, 5) is stricter than MIN_GATED_EXTREMES (3) on any
        real universe. While LunarCrush is unpaid the block is measured for
        nobody and is dark for everyone -- the owner's call: it stays as-is.
        """
        metrics: dict[str, pd.Series] = {}
        z = pd.to_numeric(df["social_volume_z"], errors="coerce")
        gated = z.where(z.abs() > self.t.attention_zscore_gate)
        metrics["social_volume_z"] = cross_sectional_percentile(
            gated, min_count=MIN_GATED_EXTREMES
        )
        metrics["social_dominance"] = cross_sectional_percentile(df["social_dominance"])
        scores = self._combine(
            df.index, metrics, {"social_volume_z": 2.0, "social_dominance": 1.0}
        )
        return scores, metrics

    def score_drawdown(self, df: pd.DataFrame) -> tuple[pd.Series, dict[str, pd.Series]]:
        """The fallen-angel setup. Weight 10.

        A nine-year study of 1,160 cryptocurrencies found a size effect, an
        illiquidity premium, and a distinctive REVERSAL effect that challenges
        the established momentum effect.

        Note precisely what that means, because it is easy to over-read: the
        effect is cross-sectional, on average, over weekly rebalances. It does
        NOT promise that any individual beaten-down chart will bounce. Weighted
        10 for that reason.
        """
        metrics: dict[str, pd.Series] = {}
        # More negative pct_below_ath = further from the high = better setup.
        metrics["pct_below_ath"] = cross_sectional_percentile(
            df["pct_below_ath"], higher_is_better=False
        )
        # Drawdown only. The overhang interaction always counted as measured, so
        # an asset with no ATH on file scored 0 here instead of None (D-059).
        scores = self._combine(df.index, metrics, {"pct_below_ath": 1.0})
        return scores, metrics

    def score_momentum(self, df: pd.DataFrame) -> tuple[pd.Series, dict[str, pd.Series]]:
        """7-day taker flow, vol-adjusted momentum, and daily trend. Weight 20.

        The one medium-horizon input in a score otherwise built from 7-to-90
        day quantities, so the ranking can respond to the market week by week
        (D-077). Raw inputs come from momentum_features(), which reads complete
        kline bars only; its docstring carries the definitions and research.
        """
        metrics: dict[str, pd.Series] = {}

        def column(name: str) -> pd.Series:
            if name in df.columns:
                return pd.to_numeric(df[name], errors="coerce")
            return pd.Series(np.nan, index=df.index, dtype=float)

        metrics["flow_7d"] = cross_sectional_percentile(column("flow_7d"))
        metrics["vamom_7d"] = cross_sectional_percentile(column("vamom_7d"))
        metrics["vamom_30d"] = cross_sectional_percentile(column("vamom_30d"))
        # A state (100 / 50 / 0), like emissions_trajectory: ranking three
        # values would only restate them.
        metrics["trend_1d"] = column("trend_1d")
        scores = self._combine(
            df.index,
            metrics,
            {"flow_7d": 2.0, "vamom_7d": 1.5, "vamom_30d": 1.0, "trend_1d": 1.0},
        )
        return scores, metrics

    def _combine(
        self, index: pd.Index, metrics: dict[str, pd.Series], weights: dict[str, float]
    ) -> pd.Series:
        """Weighted mean across a block's LIVE metrics, per asset (D-075).

        A metric is live when it is measured for at least the live floor of
        assets in the frame. A live metric the asset is missing contributes 0;
        a dark metric drops out for everyone. An asset with no live metric
        measured scores None, so the composite can report honest coverage --
        it still earns 0 for the block there.
        """
        frame = pd.DataFrame(metrics, index=index)
        floor = live_floor(len(frame), self.t.live_min_assets)
        counts = frame.notna().sum()
        live = [k for k in frame.columns if counts[k] >= floor and counts[k] > 0]
        if not live:
            return pd.Series(np.nan, index=index, dtype=float)
        weight_vector = pd.Series({k: weights.get(k, 1.0) for k in live})
        live_frame = frame[live]
        scores = (live_frame.fillna(0.0) * weight_vector).sum(axis=1) / float(
            weight_vector.sum()
        )
        return scores.where(live_frame.notna().any(axis=1)).astype(float)

    # -- orchestration -------------------------------------------------------
    def score(self, df: pd.DataFrame) -> pd.DataFrame:
        """Score every survivor. Returns a ranked frame."""
        block_scores: dict[str, pd.Series] = {}
        percentiles: dict[str, dict[str, pd.Series]] = {}

        for block, scorer in (
            ("fundamental", self.score_fundamental),
            ("supply", self.score_supply),
            ("momentum", self.score_momentum),
            ("sector", self.score_sector),
            ("events", self.score_events),
            ("attention", self.score_attention),
            ("drawdown", self.score_drawdown),
        ):
            scores, metrics = scorer(df)
            block_scores[block] = scores
            percentiles[block] = metrics

        blocks = pd.DataFrame(block_scores, index=df.index)[list(BLOCKS)]

        # D-075, the most consequential line in the module: missing data
        # scores nothing. Live blocks carry their weight for every asset;
        # dark blocks drop out for everyone. See the module header.
        total, coverage, available, live = composite(
            blocks, self.weights, self.t.live_min_assets
        )
        dark = [b for b in BLOCKS if b not in live]
        if dark:
            self.log.info(
                "layer2_dark_blocks",
                dark=dark,
                effect="measured for too few survivors; dropped for every asset",
            )

        out = blocks.copy()
        out["total_score"] = total
        out["coverage"] = coverage
        # Live blocks the asset was measured on. A reading on a dark block
        # moved nothing, so it is not counted as available.
        out["blocks_available"] = available
        out = out.sort_values("total_score", ascending=False, na_position="last")
        out["rank"] = range(1, len(out) + 1)
        out.attrs["percentiles"] = percentiles
        out.attrs["live_blocks"] = live
        return out


def load_scoring_frame(db: Database, run_date: str, survivors: list[str]) -> pd.DataFrame:
    """Assemble every L2 input for the survivors into one frame."""
    if not survivors:
        return pd.DataFrame()

    placeholders = ",".join("?" for _ in survivors)

    def latest(table: str, columns: str) -> pd.DataFrame:
        snapshot_date = db.scalar(
            f"SELECT MAX(snapshot_date) FROM {table} WHERE snapshot_date <= ?", (run_date,)
        )
        if not snapshot_date:
            return pd.DataFrame()
        rows = db.query(
            f"SELECT base_asset, {columns} FROM {table} "
            f"WHERE snapshot_date = ? AND base_asset IN ({placeholders})",
            [snapshot_date, *survivors],
        )
        frame = pd.DataFrame(rows)
        return frame.set_index("base_asset") if not frame.empty else frame

    market = latest(
        "market_snapshot",
        "market_cap_usd, circulating_supply, total_supply, pct_below_ath, price_usd",
    )
    fundamentals = latest(
        "fundamentals_snapshot",
        "tvl_usd, fees_7d_usd, fees_30d_usd, revenue_30d_usd, revenue_prev_30d_usd, "
        "revenue_annualised, has_fundamentals",
    )
    supply = latest("supply_metrics", "emissions_annual, emissions_trajectory")
    attention = latest("attention_snapshot", "social_volume_z, social_dominance")

    df = pd.DataFrame(index=pd.Index(survivors, name="base_asset"))
    for source in (market, fundamentals, supply, attention):
        if not source.empty:
            df = df.join(source[~source.index.duplicated(keep="first")], how="left")

    # Event features come from the point-in-time-filtered helper, never raw SQL.
    events = compute_all(db, survivors, run_date)
    df["days_to_next_major_unlock"] = [
        events[a].days_to_next_major_unlock if a in events else None for a in df.index
    ]
    df["days_since_last_major_unlock"] = [
        events[a].days_since_last_major_unlock if a in events else None for a in df.index
    ]
    df["unlock_overhang_cleared"] = [
        events[a].unlock_overhang_cleared if a in events else False for a in df.index
    ]
    df["positive_catalyst_30d"] = [
        events[a].positive_catalyst_30d if a in events else False for a in df.index
    ]
    df["monitoring_tag_active"] = [
        events[a].monitoring_tag_active if a in events else False for a in df.index
    ]
    # Whether we hold ANY unlock record, which is what makes a null
    # days_to_next_major_unlock readable. Without it the events block cannot
    # tell "nothing scheduled" from "nothing known".
    df["has_unlock_record"] = [
        events[a].has_unlock_record if a in events else False for a in df.index
    ]

    df = _attach_returns(db, df, run_date)

    # D-077: one query for every survivor's complete kline bars.
    momentum = momentum_features(load_momentum_bars(db, run_date, survivors), run_date)
    for column in ("flow_7d", "vamom_7d", "vamom_30d", "trend_1d"):
        df[column] = momentum[column].reindex(df.index) if not momentum.empty else np.nan

    for column in (
        "has_fundamentals", "tvl_usd", "fees_7d_usd", "fees_30d_usd", "revenue_30d_usd",
        "revenue_prev_30d_usd", "revenue_annualised",
        "emissions_annual", "emissions_trajectory", "social_volume_z", "social_dominance",
        "market_cap_usd", "circulating_supply", "total_supply", "pct_below_ath",
    ):
        if column not in df.columns:
            df[column] = np.nan
    df["has_fundamentals"] = df["has_fundamentals"].fillna(0)
    return df


def _attach_returns(db: Database, df: pd.DataFrame, run_date: str) -> pd.DataFrame:
    """7d and 30d returns, for the sector relative-strength block."""
    now = df["price_usd"] if "price_usd" in df.columns else pd.Series(dtype=float)
    benchmark = get_config().sectors.benchmark_asset
    benchmark_now = db.scalar(
        "SELECT price_usd FROM market_snapshot WHERE base_asset = ? AND snapshot_date = "
        "(SELECT MAX(snapshot_date) FROM market_snapshot WHERE snapshot_date <= ?)",
        (benchmark, run_date),
    )
    for window, days in (("7d", 7), ("30d", 30)):
        then = add_days(run_date, -days)
        # At most three days older than the window start: a price from weeks
        # before is not "the price 7 days ago" (D-059).
        rows = db.query(
            "SELECT base_asset, close_usd FROM price_daily WHERE snapshot_date = "
            "(SELECT MAX(snapshot_date) FROM price_daily "
            " WHERE snapshot_date <= ? AND snapshot_date >= ?)",
            (then, add_days(then, -3)),
        )
        past = {r["base_asset"]: r["close_usd"] for r in rows}
        df[f"return_{window}"] = [
            (now.get(a) - past[a]) / past[a]
            if a in past and past[a] and pd.notna(now.get(a))
            else np.nan
            for a in df.index
        ]
        then_price = past.get(benchmark)
        df.attrs[f"benchmark_return_{window}"] = (
            (benchmark_now - then_price) / then_price
            if benchmark_now and then_price
            else np.nan
        )
    return df


def run_layer2(db: Database, run_date: str, survivors: list[str]) -> list[dict[str, Any]]:
    """Score and persist the Layer-2 ranking. Returns ranked rows."""
    if not survivors:
        log.warning("layer2_no_survivors", run_date=run_date)
        return []

    df = load_scoring_frame(db, run_date, survivors)
    scored = Layer2Scorer(run_date).score(df)
    percentiles = scored.attrs.get("percentiles", {})
    fetched_at = utc_now_iso()
    # D-076: every row says which method produced it, so a journal cohort
    # scored one way is never blended with one scored another.
    score_version = get_config().thresholds.layer2.score_version

    rows: list[dict[str, Any]] = []
    unscorable: list[str] = []
    for asset, row in scored.iterrows():
        # An asset with no data in ANY live block has a NaN total. The
        # earlier `or 0.0` wrote that as a score of zero, which is a lie in the
        # one direction the system cannot afford: the row then looks measured
        # and terrible rather than unmeasured, it occupies a rank, and it
        # enters the journal as a signal whose forward return gets attributed
        # to a score that was never computed. Since total_score is NOT NULL,
        # the honest option is to omit the row and say so out loud.
        total = _clean(row["total_score"])
        if total is None:
            unscorable.append(str(asset))
            continue
        per_asset = {
            f"{block}.{metric}": _clean(series.get(asset))
            for block, metrics in percentiles.items()
            for metric, series in metrics.items()
        }
        rows.append(
            {
                "run_date": run_date,
                "base_asset": asset,
                "total_score": total,
                "score_fundamental": _clean(row["fundamental"]),
                "score_supply": _clean(row["supply"]),
                "score_momentum": _clean(row["momentum"]),
                "score_sector": _clean(row["sector"]),
                "score_drawdown": _clean(row["drawdown"]),
                "score_events": _clean(row["events"]),
                "score_attention": _clean(row["attention"]),
                "rank": int(row["rank"]),
                "universe_size": len(scored),
                "percentiles": json_dump(per_asset),
                "fetched_at_utc": fetched_at,
                # Share of live weight this asset was measured on (D-075).
                "coverage": _clean(row["coverage"]),
                "score_version": score_version,
            }
        )

    if unscorable:
        # Loud, not fatal: these assets survived L1, so they are worth a look,
        # but nothing about them was measurable today.
        log.warning(
            "layer2_unscorable_assets",
            run_date=run_date,
            count=len(unscorable),
            assets=sorted(unscorable)[:20],
            effect="omitted from the ranking rather than scored zero",
        )

    upsert(db, "layer2_result", rows)
    log.info(
        "layer2_complete",
        survivors=len(survivors),
        ranked=len(rows),
        unscorable=len(unscorable),
        live_blocks=scored.attrs.get("live_blocks", []),
        score_version=score_version,
    )
    return rows


#: layer2_result column per block, in BLOCKS order.
BLOCK_COLUMNS: dict[str, str] = {block: f"score_{block}" for block in BLOCKS}


def live_blocks_for_run(db: Database, run_date: str) -> list[str]:
    """The blocks that were live in a stored run, derived from its rows (D-075).

    Exact, not an estimate: an asset measured on any live block has a total
    and is stored, so a live block's stored count equals the count the scorer
    saw. Only unscorable assets are missing, and those were measured on dark
    blocks alone -- which can only keep a dark block dark. The floor uses the
    stored universe_size, the frame size the scorer applied it to.
    """
    columns = ", ".join(f"COUNT({c}) AS {c}" for c in BLOCK_COLUMNS.values())
    row = db.query_one(
        f"SELECT {columns}, MAX(universe_size) AS universe_size "
        "FROM layer2_result WHERE run_date = ?",
        (run_date,),
    )
    if not row or not row.get("universe_size"):
        return []
    floor = live_floor(
        int(row["universe_size"]), get_config().thresholds.layer2.live_min_assets
    )
    return [
        block
        for block, column in BLOCK_COLUMNS.items()
        if (row.get(column) or 0) >= floor and (row.get(column) or 0) > 0
    ]


def _clean(value: Any) -> float | None:
    """NaN must reach the database as NULL, never as a float that sorts oddly."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(number) else round(number, 4)


def block_correlation_report(db: Database, min_days: int | None = None) -> dict[str, Any]:
    """Correlate the seven block scores.

    Any pair above the threshold is measuring one thing twice. Applying
    iterative factor selection to 36 crypto return-predictive factors found
    two to three factors eliminated all significant portfolio alphas -- a
    seven-block score with high internal correlation is a three-block score
    wearing a costume. Momentum against drawdown is the pair D-077 expects
    to be negative; this is where that is settled.
    """
    cfg = get_config().thresholds.layer2
    required = min_days or cfg.redundancy_min_days
    rows = db.query(
        "SELECT run_date, " + ", ".join(BLOCK_COLUMNS.values()) + " FROM layer2_result"
    )
    frame = pd.DataFrame(rows)
    days = frame["run_date"].nunique() if not frame.empty else 0
    if days < required:
        return {"ready": False, "days": days, "required": required, "pairs": []}

    matrix = frame.drop(columns=["run_date"]).corr()
    flagged = [
        {"a": a, "b": b, "rho": round(float(matrix.loc[a, b]), 3)}
        for i, a in enumerate(matrix.columns)
        for b in matrix.columns[i + 1 :]
        if pd.notna(matrix.loc[a, b]) and abs(matrix.loc[a, b]) > cfg.redundancy_corr_threshold
    ]
    return {"ready": True, "days": days, "matrix": matrix.round(3).to_dict(), "pairs": flagged}


__all__ = [
    "BLOCKS",
    "BLOCK_COLUMNS",
    "LEGACY_SCORE_VERSION",
    "MIN_GATED_EXTREMES",
    "Layer2Scorer",
    "block_correlation_report",
    "composite",
    "cross_sectional_percentile",
    "live_blocks_for_run",
    "live_floor",
    "load_momentum_bars",
    "load_scoring_frame",
    "momentum_features",
    "run_layer2",
]
