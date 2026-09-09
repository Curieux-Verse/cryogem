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
        "emissions_trajectory": None,
        "burned_pct_of_total": np.nan,
        "staked_ratio": np.nan,
        "staked_is_team_controlled": np.nan,
        "social_volume_z": np.nan,
        "social_dominance": np.nan,
        "days_to_next_major_unlock": np.nan,
        "days_since_last_major_unlock": np.nan,
        "unlock_overhang_cleared": False,
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
    def test_asset_missing_the_fundamental_block_still_totals_within_range(self):
        df = frame(["A", "B", "C"], has_fundamentals=0)
        scored = Layer2Scorer(RUN_DATE).score(df)
        assert scored["fundamental"].isna().all(), "no revenue model => block is None"
        assert scored["total_score"].notna().all(), "the asset must still be scored"
        assert (scored["total_score"] <= 100.0).all()
        assert (scored["total_score"] >= 0.0).all()

    def test_missing_block_is_none_not_zero(self):
        """A None redistributes weight; a zero would bury every non-revenue asset.

        Two otherwise-identical assets, one with fundamentals data and one
        without, must not be separated by a fabricated zero.
        """
        df = frame(["HAS", "HASNT"])
        df.loc["HAS", "has_fundamentals"] = 1
        df.loc["HAS", "revenue_30d_usd"] = 1e6
        df.loc["HAS", "revenue_prev_30d_usd"] = 1e6
        df.loc["HAS", "revenue_annualised"] = 12e6
        scored = Layer2Scorer(RUN_DATE).score(df)
        assert pd.isna(scored.loc["HASNT", "fundamental"])
        # If a missing block scored 0, HASNT would be dragged far below HAS.
        # Renormalisation keeps them comparable on the blocks they share.
        assert scored.loc["HASNT", "total_score"] > 0.0

    def test_blocks_available_is_recorded(self):
        df = frame(["A", "B", "C"])
        scored = Layer2Scorer(RUN_DATE).score(df)
        assert (scored["blocks_available"] >= 1).all()
        assert (scored["blocks_available"] <= len(BLOCKS)).all()

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
        df = frame(["TAGGED", "CLEAN"])
        df.loc["TAGGED", "monitoring_tag_active"] = True
        scored = Layer2Scorer(RUN_DATE).score(df)
        assert scored.loc["TAGGED", "events"] < scored.loc["CLEAN", "events"]

    def test_attention_is_gated_to_extremes(self):
        """|z| below the gate contributes nothing rather than contributing noise."""
        df = frame(["MID", "EXTREME"])
        df.loc["MID", "social_volume_z"] = 0.5
        df.loc["EXTREME", "social_volume_z"] = 4.0
        scorer = Layer2Scorer(RUN_DATE)
        _, metrics = scorer.score_attention(df)
        assert pd.isna(metrics["social_volume_z"]["MID"]), "middle of the distribution is gated out"
        assert pd.notna(metrics["social_volume_z"]["EXTREME"])

    def test_team_controlled_stake_is_discounted(self):
        """Staked supply reduces float only if it is not the team's own stake."""
        df = frame(["TEAM", "REAL"])
        df.loc[:, "staked_ratio"] = 0.5
        df.loc["TEAM", "staked_is_team_controlled"] = 1
        df.loc["REAL", "staked_is_team_controlled"] = 0
        scorer = Layer2Scorer(RUN_DATE)
        _, metrics = scorer.score_supply(df)
        assert pd.isna(metrics["staked_ratio"]["TEAM"])
        assert pd.notna(metrics["staked_ratio"]["REAL"])

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
