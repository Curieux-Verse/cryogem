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
# WEIGHT RENORMALISATION. The six weights sum to 110 deliberately, then
# normalise to 100. When a block is unavailable for an asset -- most often a
# token with no revenue model -- that block scores None and its weight is
# redistributed across the rest, rather than scoring zero.
#
# That distinction decides the whole ranking. A zero says "this asset has bad
# fundamentals". A None says "this asset has no fundamentals to measure". A
# token with no revenue model is not a token with failing revenue, and scoring
# it zero would systematically bury every non-revenue asset beneath every
# revenue one, which is a bet the data has not justified.
#
# REDUNDANCY. After 30 days, correlate the six block scores. Any pair above
# |rho| 0.8 is measuring one thing twice: applying iterative factor selection to
# 36 crypto return-predictive factors found two to three factors eliminated all
# significant portfolio alphas. A six-block score with high internal correlation
# is a three-block score wearing a costume.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

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

BLOCKS = ("fundamental", "supply", "sector", "events", "attention", "drawdown")

#: Fewest assets past the attention gate before any of them is ranked. One asset
#: past the gate ranked first of one and scored 100 (D-059).
MIN_GATED_EXTREMES = 3


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


def _weighted_mean(values: dict[str, float | None], weights: dict[str, float]) -> float | None:
    """Weighted mean over available components, renormalised. None if none exist."""
    available = {k: v for k, v in values.items() if v is not None and not pd.isna(v)}
    if not available:
        return None
    total_weight = sum(weights.get(k, 1.0) for k in available)
    if total_weight <= 0:
        return None
    return sum(v * weights.get(k, 1.0) for k, v in available.items()) / total_weight


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
        """Revenue growth, fee acceleration, cheapness, usage. Weight 35.

        The reference case is VVV: revenue moving from a ~$70M to a ~$100M
        annualised run-rate inside one month, visible here before it was
        visible in price.
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
        metrics["active_addresses"] = cross_sectional_percentile(df["active_addresses_24h"])

        component_weights = {
            "revenue_growth_30d": 3.0,
            "fee_acceleration": 2.0,
            "price_to_sales": 2.0,
            "active_addresses": 1.5,
            "tvl": 0.5,
        }
        scores = self._combine(df.index, metrics, component_weights)
        # An asset with no revenue model scores None here, not zero.
        scores[df["has_fundamentals"] == 0] = np.nan
        return scores, metrics

    def score_supply(self, df: pd.DataFrame) -> tuple[pd.Series, dict[str, pd.Series]]:
        """Net issuance, its direction, float, and distance from the last cliff. Weight 25.

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
        bonus = df["unlock_overhang_cleared"].astype("boolean").fillna(False).astype(bool)
        scores = (scores + bonus * self.t.unlock_overhang_cleared_bonus).clip(upper=100.0)
        metrics["unlock_overhang_cleared"] = bonus.astype(float) * 100.0
        return scores, metrics

    def score_sector(self, df: pd.DataFrame) -> tuple[pd.Series, dict[str, pd.Series]]:
        """The asset's sector's relative strength against BTC. Weight 15.

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
            # metric scores None, the block renormalises across whatever else
            # was measured, and nothing pretends to be a BTC comparison that
            # is not one.
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
                    effect="sector RS scored None for this window, weight redistributed",
                )
                relative = pd.Series(np.nan, index=usable.index, dtype=float)
            else:
                relative = usable - benchmark
            mapped = pd.Series(sector_of, index=df.index).map(relative)
            # 'unclassified' is not a sector: score it None, never the mean.
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
        # treatment. The rest stay NaN, score None on this metric, and their
        # weight redistributes across the metrics that were measured. Cast
        # first: fillna on an object column downcasts silently.
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

        # The overhang is counted once, in the supply block. It was also a
        # weight-2 metric here and the drawdown interaction: three counts of one
        # flag (D-059).
        scores = self._combine(
            df.index, metrics, {"days_to_next_unlock": 2.0, "positive_catalyst": 1.0}
        )

        # A monitoring tag is a soft exchange warning for elevated-volatility
        # assets and often precedes delisting. It is a measured negative, so it
        # sets the block to zero whatever else is or is not known.
        tagged = df["monitoring_tag_active"].astype("boolean").fillna(False).astype(bool)
        metrics["monitoring_tag"] = pd.Series(np.nan, index=df.index).mask(tagged, 0.0)
        scores = scores.mask(tagged, 0.0)
        return scores, metrics

    def score_attention(self, df: pd.DataFrame) -> tuple[pd.Series, dict[str, pd.Series]]:
        """Social attention, z-scored and GATED. Weight 15.

        The gate matters as much as the metric: sentiment's predictive power
        concentrates at extreme states, so an asset sitting in the middle of its
        own distribution contributes nothing rather than contributing noise.
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

    @staticmethod
    def _combine(
        index: pd.Index, metrics: dict[str, pd.Series], weights: dict[str, float]
    ) -> pd.Series:
        """Weighted mean across a block's metrics, per asset, NaN-aware."""
        frame = pd.DataFrame(metrics, index=index)
        weight_vector = pd.Series({k: weights.get(k, 1.0) for k in frame.columns})
        mask = frame.notna()
        weighted_sum = (frame.fillna(0.0) * weight_vector).sum(axis=1)
        weight_total = (mask * weight_vector).sum(axis=1)
        return (weighted_sum / weight_total.replace(0, np.nan)).astype(float)

    # -- orchestration -------------------------------------------------------
    def score(self, df: pd.DataFrame) -> pd.DataFrame:
        """Score every survivor. Returns a ranked frame."""
        block_scores: dict[str, pd.Series] = {}
        percentiles: dict[str, dict[str, pd.Series]] = {}

        for block, scorer in (
            ("fundamental", self.score_fundamental),
            ("supply", self.score_supply),
            ("sector", self.score_sector),
            ("events", self.score_events),
            ("attention", self.score_attention),
            ("drawdown", self.score_drawdown),
        ):
            scores, metrics = scorer(df)
            block_scores[block] = scores
            percentiles[block] = metrics

        blocks = pd.DataFrame(block_scores, index=df.index)

        # Renormalise per asset over the blocks that ARE available, so a token
        # with no revenue model is not buried beneath one that merely has bad
        # revenue. This is the most consequential line in the module.
        weight_vector = pd.Series({b: self.weights[b] for b in BLOCKS})
        mask = blocks.notna()
        weighted_sum = (blocks.fillna(0.0) * weight_vector).sum(axis=1)
        weight_total = (mask * weight_vector).sum(axis=1)
        total = (weighted_sum / weight_total.replace(0, np.nan)).astype(float)

        out = blocks.copy()
        out["total_score"] = total
        out["blocks_available"] = mask.sum(axis=1)
        out = out.sort_values("total_score", ascending=False, na_position="last")
        out["rank"] = range(1, len(out) + 1)
        out.attrs["percentiles"] = percentiles
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
        "revenue_annualised, active_addresses_24h, has_fundamentals",
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

    for column in (
        "has_fundamentals", "tvl_usd", "fees_7d_usd", "fees_30d_usd", "revenue_30d_usd",
        "revenue_prev_30d_usd", "revenue_annualised", "active_addresses_24h",
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

    rows: list[dict[str, Any]] = []
    unscorable: list[str] = []
    for asset, row in scored.iterrows():
        # An asset with no data in ANY of the six blocks has a NaN total. The
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
                "score_sector": _clean(row["sector"]),
                "score_drawdown": _clean(row["drawdown"]),
                "score_events": _clean(row["events"]),
                "score_attention": _clean(row["attention"]),
                "rank": int(row["rank"]),
                "universe_size": len(scored),
                "percentiles": json_dump(per_asset),
                "fetched_at_utc": fetched_at,
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
    )
    return rows


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
    """Correlate the six block scores.

    Any pair above the threshold is measuring one thing twice. Applying
    iterative factor selection to 36 crypto return-predictive factors found
    two to three factors eliminated all significant portfolio alphas -- a
    six-block score with high internal correlation is a three-block score
    wearing a costume.
    """
    cfg = get_config().thresholds.layer2
    required = min_days or cfg.redundancy_min_days
    rows = db.query(
        "SELECT run_date, score_fundamental, score_supply, score_sector, "
        "score_events, score_attention, score_drawdown FROM layer2_result"
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
    "MIN_GATED_EXTREMES",
    "Layer2Scorer",
    "block_correlation_report",
    "cross_sectional_percentile",
    "load_scoring_frame",
    "run_layer2",
]
