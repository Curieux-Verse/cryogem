"""Layer 2: cross-sectional scoring, and the weight renormalisation rule."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.screening.layer2_score import (
    BLOCKS,
    Layer2Scorer,
    cross_sectional_percentile,
)

RUN_DATE = "2026-09-09"


def frame(assets: list[str], **columns) -> pd.DataFrame:
    """A scoring frame with every expected column, defaulting to NaN."""
    defaults = {
        "market_cap_usd": 100e6,
        "circulating_supply": 80e6,
        "total_supply": 100e6,
        "pct_below_ath": -0.5,
        "price_usd": 1.0,
        "tvl_usd": np.nan,
        "fees_7d_usd": np.nan,
        "fees_30d_usd": np.nan,
        "revenue_30d_usd": np.nan,
        "revenue_prev_30d_usd": np.nan,
        "revenue_annualised": np.nan,
        "active_addresses_24h": np.nan,
        "has_fundamentals": 0,
        "emissions_annual": np.nan,
        "emissions_trajectory": None,
        "social_volume_z": np.nan,
        "social_dominance": np.nan,
        "days_to_next_major_unlock": np.nan,
        "days_since_last_major_unlock": np.nan,
        "unlock_overhang_cleared": False,
        # The fixture default is an asset we HOLD a vesting schedule for, so
        # a null days_to_next_major_unlock means "no cliff ahead". Assets we
        # know nothing about are the explicit case, tested separately.
        "has_unlock_record": True,
        "positive_catalyst_30d": False,
        "monitoring_tag_active": False,
        "return_7d": 0.0,
        "return_30d": 0.0,
    }
    df = pd.DataFrame(index=pd.Index(assets, name="base_asset"))
    for key, value in defaults.items():
        df[key] = value
    for key, value in columns.items():
        df[key] = value
    return df


class TestPercentileRanking:
    def test_ranks_run_zero_to_one_hundred(self):
        s = cross_sectional_percentile(pd.Series([1, 2, 3, 4]))
        assert s.min() == 25.0
        assert s.max() == 100.0

    def test_nan_stays_nan_and_never_becomes_zero(self):
        """An asset we could not measure is not one that measured badly."""
        s = cross_sectional_percentile(pd.Series([1.0, np.nan, 3.0]))
        assert pd.isna(s.iloc[1])
        assert s.iloc[0] < s.iloc[2]

    def test_lower_is_better_inverts_the_ranking(self):
        s = cross_sectional_percentile(pd.Series([1.0, 10.0]), higher_is_better=False)
        assert s.iloc[0] > s.iloc[1], "a cheap price-to-sales must rank above an expensive one"

    def test_ranking_is_relative_not_absolute(self):
        """The same value ranks differently in a different universe.

        This is the whole point of Layer 2: an absolute cutoff breaks when
        market regime shifts, a percentile rank does not.
        """
        weak = cross_sectional_percentile(pd.Series([5.0, 1.0, 2.0])).iloc[0]
        strong = cross_sectional_percentile(pd.Series([5.0, 100.0, 200.0])).iloc[0]
        assert weak > strong


class TestWeightRenormalisation:
    """Renamed in spirit by D-075: weights normalise over LIVE blocks, not over
    whatever an individual asset happens to have."""

    def test_asset_missing_the_fundamental_block_still_totals_within_range(self):
        """Unchanged by D-075: a block dark for everyone drops out for everyone,
        so the assets are still scored, within 0-100."""
        df = frame(["A", "B", "C"], has_fundamentals=0)
        scored = Layer2Scorer(RUN_DATE).score(df)
        assert scored["fundamental"].isna().all(), "no revenue model => block is None"
        assert scored["total_score"].notna().all(), "the asset must still be scored"
        assert (scored["total_score"] <= 100.0).all()
        assert (scored["total_score"] >= 0.0).all()

    def test_missing_block_is_none_not_zero(self):
        """The block stays None in the row -- never a measured zero.

        Changed by D-075. This test used to assert that renormalisation kept
        HASNT comparable to HAS. Its assertion (HASNT > 0) still holds, but for
        a different reason: in a two-asset universe the live floor is both
        assets, so a fundamental block measured for one of them is dark and
        drops out for both. The None-not-zero half of the contract survives
        unchanged; see TestMissingDataScoresNothing for the live case.
        """
        df = frame(["HAS", "HASNT"])
        df.loc["HAS", "has_fundamentals"] = 1
        df.loc["HAS", "revenue_30d_usd"] = 1e6
        df.loc["HAS", "revenue_prev_30d_usd"] = 1e6
        df.loc["HAS", "revenue_annualised"] = 12e6
        scored = Layer2Scorer(RUN_DATE).score(df)
        assert pd.isna(scored.loc["HASNT", "fundamental"])
        assert scored.loc["HASNT", "total_score"] > 0.0

    def test_blocks_available_counts_exactly_the_measured_blocks(self):
        """The old assertion (>= 1 and <= 6) could not fail. The fixture measures
        supply (float), events (a schedule with no cliff) and drawdown (ATH);
        fundamental, momentum (no bars), sector (unclassified) and attention
        are unmeasured. D-077 added momentum, so there are seven blocks."""
        df = frame(["A", "B", "C"])
        scored = Layer2Scorer(RUN_DATE).score(df)
        assert (scored["blocks_available"] == 3).all()
        assert len(BLOCKS) == 7
        assert scored.attrs["live_blocks"] == ["supply", "events", "drawdown"]
        assert (scored["coverage"] == 1.0).all(), "measured on every live block"

    def test_rank_is_dense_and_starts_at_one(self):
        df = frame(["A", "B", "C", "D"])
        scored = Layer2Scorer(RUN_DATE).score(df)
        assert list(scored["rank"]) == [1, 2, 3, 4]


class TestBlockBehaviour:
    def test_falling_emissions_beat_rising(self):
        df = frame(["FALL", "RISE"])
        df.loc["FALL", "emissions_trajectory"] = "falling"
        df.loc["RISE", "emissions_trajectory"] = "rising"
        scored = Layer2Scorer(RUN_DATE).score(df)
        assert scored.loc["FALL", "supply"] > scored.loc["RISE", "supply"]

    def test_cleared_overhang_scores_above_a_pending_cliff(self):
        df = frame(["CLEARED", "PENDING"])
        df.loc["CLEARED", "unlock_overhang_cleared"] = True
        df.loc["PENDING", "days_to_next_major_unlock"] = 45
        scored = Layer2Scorer(RUN_DATE).score(df)
        assert scored.loc["CLEARED", "supply"] > scored.loc["PENDING", "supply"]

    def test_monitoring_tag_is_a_strong_negative(self):
        df = frame(["TAGGED", "CLEAN", "OTHER"])
        df["days_to_next_major_unlock"] = [40, 40, 90]
        df.loc["TAGGED", "monitoring_tag_active"] = True
        scored = Layer2Scorer(RUN_DATE).score(df)
        assert scored.loc["TAGGED", "events"] == 0.0
        assert scored.loc["CLEAN", "events"] > 0.0

    def test_attention_is_gated_to_extremes(self):
        """|z| below the gate contributes nothing rather than contributing noise."""
        df = frame(["MID", "E1", "E2", "E3"])
        df["social_volume_z"] = [0.5, 5.0, 4.5, 4.0]
        scorer = Layer2Scorer(RUN_DATE)
        _, metrics = scorer.score_attention(df)
        assert pd.isna(metrics["social_volume_z"]["MID"]), "middle of the distribution is gated out"
        assert metrics["social_volume_z"][["E1", "E2", "E3"]].notna().all()

    def test_a_lone_extreme_is_not_ranked_against_nobody(self):
        """D-059. One asset past the gate ranked first of one and scored 100."""
        df = frame(["MID", "EXTREME"])
        df["social_volume_z"] = [0.5, 5.0]
        _, metrics = Layer2Scorer(RUN_DATE).score_attention(df)
        assert metrics["social_volume_z"].isna().all()

    def test_a_loss_maker_is_not_the_cheapest_on_price_to_sales(self):
        """D-059. A negative revenue gave a negative P/S, which ranked cheapest."""
        df = frame(["LOSS", "A", "B"], has_fundamentals=1)
        df["revenue_annualised"] = [-5e6, 10e6, 20e6]
        _, metrics = Layer2Scorer(RUN_DATE).score_fundamental(df)
        assert pd.isna(metrics["price_to_sales"]["LOSS"])
        assert metrics["price_to_sales"][["A", "B"]].notna().all()

    def test_a_missing_ath_is_no_drawdown_score_rather_than_zero(self):
        """D-059. The overhang interaction always counted as measured, so no ATH scored 0."""
        df = frame(["NOATH", "A", "B"])
        df.loc["NOATH", "pct_below_ath"] = np.nan
        scored = Layer2Scorer(RUN_DATE).score(df)
        assert pd.isna(scored.loc["NOATH", "drawdown"])

    def test_lower_net_issuance_scores_higher(self):
        """D-072. A burn is negative issuance and outranks an inflating supply."""
        df = frame(["BURN", "FLAT", "INFLATE"])
        df["emissions_annual"] = [-0.05, 0.0, 0.40]
        _, metrics = Layer2Scorer(RUN_DATE).score_supply(df)
        issuance = metrics["net_issuance"]
        assert issuance["BURN"] > issuance["FLAT"] > issuance["INFLATE"]

    def test_unmeasured_issuance_is_none_not_zero(self):
        """No supply history is not zero issuance: it must not rank as the cleanest."""
        df = frame(["UNKNOWN", "A", "B"])
        df["emissions_annual"] = [np.nan, 0.01, 0.30]
        _, metrics = Layer2Scorer(RUN_DATE).score_supply(df)
        assert pd.isna(metrics["net_issuance"]["UNKNOWN"])

    def test_no_supply_metric_without_a_writer_is_listed(self):
        """D-072. Burned and staked share had no source; they must not look live."""
        _, metrics = Layer2Scorer(RUN_DATE).score_supply(frame(["A", "B"]))
        assert "burned_pct" not in metrics
        assert "staked_ratio" not in metrics

    def test_unclassified_sector_scores_none_not_the_mean(self):
        """An unmapped asset must not inherit an average sector score."""
        df = frame(["NOTATICKER123"])
        scorer = Layer2Scorer(RUN_DATE)
        _, metrics = scorer.score_sector(df)
        assert pd.isna(metrics["sector_rs_7d"]["NOTATICKER123"])

    def test_deeper_drawdown_scores_higher(self):
        df = frame(["DEEP", "SHALLOW"])
        df.loc["DEEP", "pct_below_ath"] = -0.90
        df.loc["SHALLOW", "pct_below_ath"] = -0.10
        scored = Layer2Scorer(RUN_DATE).score(df)
        assert scored.loc["DEEP", "drawdown"] > scored.loc["SHALLOW", "drawdown"]


class TestMissingUnlockDataIsNotGoodNews:
    """A review found the events block scoring ignorance as the best case.

    `days_to_next_major_unlock` is null for two opposite reasons: we hold a
    vesting schedule and there is no cliff ahead, or we hold nothing at all.
    Filling both with 9999 put every asset we knew nothing about in the top
    percentile for unlock distance.
    """

    def test_no_record_does_not_outrank_a_distant_known_cliff(self):
        df = frame(["UNKNOWN", "FARCLIFF"])
        df.loc["UNKNOWN", "has_unlock_record"] = False
        df.loc["UNKNOWN", "days_to_next_major_unlock"] = np.nan
        df.loc["FARCLIFF", "days_to_next_major_unlock"] = 400
        scorer = Layer2Scorer(RUN_DATE)
        _, metrics = scorer.score_events(df)
        unlock = metrics["days_to_next_unlock"]
        assert pd.isna(unlock["UNKNOWN"]), "an unmeasured asset must score None"
        assert pd.notna(unlock["FARCLIFF"])

    def test_no_record_beats_nothing_and_loses_to_nothing(self):
        """It is absent from the metric, so it neither gains nor suffers."""
        df = frame(["UNKNOWN", "SOONCLIFF"])
        df.loc["UNKNOWN", "has_unlock_record"] = False
        df.loc["SOONCLIFF", "days_to_next_major_unlock"] = 3
        scorer = Layer2Scorer(RUN_DATE)
        _, metrics = scorer.score_events(df)
        assert pd.isna(metrics["days_to_next_unlock"]["UNKNOWN"])

    def test_known_schedule_with_no_cliff_ahead_still_scores_top(self):
        """The legitimate half of the old behaviour must survive the fix."""
        df = frame(["CLEAR", "CLIFF"])
        df.loc["CLEAR", "days_to_next_major_unlock"] = np.nan
        df.loc["CLIFF", "days_to_next_major_unlock"] = 10
        scorer = Layer2Scorer(RUN_DATE)
        _, metrics = scorer.score_events(df)
        unlock = metrics["days_to_next_unlock"]
        assert unlock["CLEAR"] > unlock["CLIFF"]

    def test_an_asset_with_no_event_data_has_no_events_score(self):
        """D-059. The catalyst and monitoring flags were filled with False and
        counted as measured, so an asset we knew nothing about scored 50 on
        events. With nothing known the block is None. D-075: in this one-asset
        universe the block is then dark and drops out; in a live universe the
        None would earn 0 (TestMissingDataScoresNothing)."""
        df = frame(["UNKNOWN"])
        df.loc["UNKNOWN", "has_unlock_record"] = False
        scored = Layer2Scorer(RUN_DATE).score(df)
        assert pd.isna(scored.loc["UNKNOWN", "events"])
        assert pd.notna(scored.loc["UNKNOWN", "total_score"])


class TestSectorRelativeStrengthNeedsItsBenchmark:
    """Three real defi members, so the sector index is genuinely built.

    Otherwise the assertion would pass for the wrong reason -- an unmapped
    ticker scores None on this block anyway.
    """

    MEMBERS = ["UNI", "AAVE", "MKR"]

    def test_a_present_benchmark_produces_a_real_reading(self):
        df = frame([*self.MEMBERS, "BTC"])
        df["return_7d"] = [0.10, 0.20, 0.30, 0.05]
        df["return_30d"] = [0.10, 0.20, 0.30, 0.05]
        _, metrics = Layer2Scorer(RUN_DATE).score_sector(df)
        for window in ("7d", "30d"):
            got = metrics[f"sector_rs_{window}"]
            assert got[self.MEMBERS].notna().all(), "with BTC present this is measurable"

    def test_missing_benchmark_scores_none_not_a_peer_mean(self):
        """Without BTC there is no BTC comparison, so there is nothing to report.

        The earlier code substituted the cross-sectional mean, which silently
        redefined sector_rs as strength against the screened universe.
        """
        df = frame(self.MEMBERS)
        df["return_7d"] = [0.10, 0.20, 0.30]
        df["return_30d"] = [0.10, 0.20, 0.30]
        _, metrics = Layer2Scorer(RUN_DATE).score_sector(df)
        for window in ("7d", "30d"):
            assert metrics[f"sector_rs_{window}"].isna().all()

    def test_the_benchmark_need_not_be_a_survivor(self):
        """D-059. BTC failing Layer 1 must not switch the block off for every asset."""
        df = frame(self.MEMBERS)
        df["return_7d"] = [0.10, 0.20, 0.30]
        df["return_30d"] = [0.10, 0.20, 0.30]
        df.attrs.update(benchmark_return_7d=0.05, benchmark_return_30d=0.05)
        _, metrics = Layer2Scorer(RUN_DATE).score_sector(df)
        for window in ("7d", "30d"):
            assert metrics[f"sector_rs_{window}"][self.MEMBERS].notna().all()
