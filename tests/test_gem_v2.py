"""
gem-v2: missing data scores nothing (D-075), and the momentum / flow block
(D-077).

The acceptance test from docs/PLAN_ACTIVE_SCREENER.md section 4 is the first
one below: a coin measured on two strong blocks must not outrank an otherwise
identical coin measured on five, on missing weight alone.
"""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from src.db.writes import upsert
from src.screening import layer2_score as l2
from src.screening.layer2_score import BLOCKS, Layer2Scorer, composite, live_floor
from src.timeutil import add_days
from tests.test_layer2 import RUN_DATE, frame

WEIGHTS = {
    "fundamental": 30, "supply": 20, "momentum": 20, "sector": 10,
    "events": 10, "attention": 10, "drawdown": 10,
}


def blocks_frame(rows: dict[str, dict[str, float]]) -> pd.DataFrame:
    out = pd.DataFrame.from_dict(rows, orient="index", dtype=float)
    return out.reindex(columns=list(BLOCKS))


# ==============================================================================
# D-075 -- missing data scores nothing
# ==============================================================================
class TestMissingDataScoresNothing:
    """The owner's rule: "Don't let missing data score anything"."""

    @staticmethod
    def _universe() -> pd.DataFrame:
        # Four fillers measured on five blocks keep those blocks live (>= 5
        # measured, with FIVE). TWO is FIVE with three blocks missing.
        rows = {
            f"F{i}": {
                "fundamental": 50, "supply": 50, "sector": 50, "events": 50, "drawdown": 50
            }
            for i in range(4)
        }
        rows["FIVE"] = {
            "fundamental": 20, "supply": 95, "sector": 20, "events": 20, "drawdown": 95
        }
        rows["TWO"] = {"supply": 95, "drawdown": 95}
        return blocks_frame(rows)

    def test_a_two_block_asset_cannot_outrank_an_identical_five_block_one(self):
        """The plan's acceptance test. FIVE's extra blocks are weak (20), which
        under per-asset renormalisation handed TWO the win on missing weight
        alone: TWO renormalised to 95 while FIVE averaged its weak blocks in."""
        blocks = self._universe()
        total, _, _, live = composite(blocks, WEIGHTS, live_min_assets=5)
        assert set(live) == {"fundamental", "supply", "sector", "events", "drawdown"}
        assert total["FIVE"] > total["TWO"]

        # The defect this replaces, reproduced: the old formula ranked TWO first.
        weights = pd.Series({b: WEIGHTS[b] for b in BLOCKS})
        old = (blocks.fillna(0) * weights).sum(axis=1) / (blocks.notna() * weights).sum(axis=1)
        assert old["TWO"] > old["FIVE"]

    def test_the_same_holds_through_the_real_scorer(self):
        """End to end: fundamentals measured for five assets but not TWO, and
        TWO has no unlock record. Supply and drawdown inputs are identical, and
        the best in the universe, for both."""
        assets = ["A", "B", "C", "D", "FIVE", "TWO"]
        df = frame(assets, has_fundamentals=1)
        df["revenue_30d_usd"] = [2e6, 3e6, 4e6, 5e6, 1e6, np.nan]
        df["revenue_prev_30d_usd"] = [1e6] * 5 + [np.nan]
        df["revenue_annualised"] = [24e6, 36e6, 48e6, 60e6, 12e6, np.nan]
        df.loc["TWO", "has_fundamentals"] = 0
        df.loc["TWO", "has_unlock_record"] = False
        df["pct_below_ath"] = [-0.2, -0.3, -0.4, -0.5, -0.95, -0.95]
        df["circulating_supply"] = [50e6, 55e6, 60e6, 65e6, 100e6, 100e6]
        scored = Layer2Scorer(RUN_DATE).score(df)
        assert pd.isna(scored.loc["TWO", "fundamental"])
        assert pd.notna(scored.loc["FIVE", "fundamental"])
        assert scored.loc["FIVE", "total_score"] > scored.loc["TWO", "total_score"]
        assert scored.loc["TWO", "coverage"] < scored.loc["FIVE", "coverage"]

    def test_a_block_dark_for_everyone_drops_out_for_everyone(self):
        """Attention measured for nobody: removing the column changes nothing,
        neither the order nor the absolute scores."""
        blocks = self._universe()
        with_total, with_cov, _, live = composite(blocks, WEIGHTS, 5)
        without_total, without_cov, _, _ = composite(
            blocks.drop(columns=["attention"]), WEIGHTS, 5
        )
        assert "attention" not in live
        pd.testing.assert_series_equal(with_total, without_total)
        pd.testing.assert_series_equal(with_cov, without_cov)

    def test_a_block_below_the_floor_is_dark_even_if_someone_has_it(self):
        """Three assets measured on attention is not a cross-section: those
        three must not gain on everyone else from it."""
        blocks = self._universe()
        blocks.loc[["F0", "F1", "TWO"], "attention"] = 100.0
        total, _, _, live = composite(blocks, WEIGHTS, 5)
        baseline, _, _, _ = composite(self._universe(), WEIGHTS, 5)
        assert "attention" not in live
        pd.testing.assert_series_equal(total, baseline)

    def test_coverage_is_measured_live_weight_over_live_weight(self):
        _, coverage, available, _ = composite(self._universe(), WEIGHTS, 5)
        # Live: fundamental 30, supply 20, sector 10, events 10, drawdown 10 = 80.
        assert coverage["FIVE"] == pytest.approx(1.0)
        assert coverage["TWO"] == pytest.approx(30 / 80)
        assert available["FIVE"] == 5
        assert available["TWO"] == 2

    def test_a_missing_live_block_contributes_zero(self):
        total, _, _, _ = composite(self._universe(), WEIGHTS, 5)
        assert total["TWO"] == pytest.approx((95 * 20 + 95 * 10) / 80)

    def test_nothing_measured_on_a_live_block_is_unscorable_not_zero(self):
        """Still omitted, never written as a measured 0 (the run_layer2 rule)."""
        blocks = self._universe()
        blocks.loc["NONE"] = np.nan
        blocks.loc["NONE", "attention"] = 80.0  # a dark block only
        total, coverage, _, _ = composite(blocks, WEIGHTS, 5)
        assert pd.isna(total["NONE"])
        assert coverage["NONE"] == 0.0

    def test_the_live_floor_is_the_universe_when_it_is_smaller(self):
        assert live_floor(3, 5) == 3
        assert live_floor(200, 5) == 5
        assert live_floor(0, 5) == 1


class TestMetricLevelLiveRule:
    """The same rule one level down: inside a block, metric by metric."""

    ASSETS = ["A", "B", "C", "D", "E", "F"]

    def _combine(self, metrics, weights):
        return Layer2Scorer(RUN_DATE)._combine(pd.Index(self.ASSETS), metrics, weights)

    def test_a_missing_live_metric_contributes_zero(self):
        full = pd.Series([80.0] * 6, index=self.ASSETS)
        partial = pd.Series([60.0] * 5 + [np.nan], index=self.ASSETS)
        scores = self._combine({"x": full, "y": partial}, {"x": 1.0, "y": 1.0})
        assert scores["A"] == pytest.approx(70.0)
        # Renormalised, F would have scored 80 -- above everyone who had both.
        assert scores["F"] == pytest.approx(40.0)

    def test_a_metric_below_the_floor_drops_out_for_everyone(self):
        x = pd.Series([80.0, 70.0, 60.0, 50.0, 40.0, 30.0], index=self.ASSETS)
        sparse = pd.Series([100.0, 100.0] + [np.nan] * 4, index=self.ASSETS)
        with_sparse = self._combine({"x": x, "sparse": sparse}, {"x": 1.0, "sparse": 1.0})
        alone = self._combine({"x": x}, {"x": 1.0})
        pd.testing.assert_series_equal(with_sparse, alone)

    def test_no_live_metric_measured_is_none_not_zero(self):
        x = pd.Series([80.0, 70.0, 60.0, 50.0, 40.0, np.nan], index=self.ASSETS)
        scores = self._combine({"x": x}, {"x": 1.0})
        assert pd.isna(scores["F"]), "coverage must be able to see F was unmeasured"

    def test_a_lone_catalyst_no_longer_scores_the_whole_block(self):
        """100-or-NaN was an inflation path: renormalised, an asset whose only
        events reading was a known catalyst scored the events block 100."""
        df = frame(self.ASSETS)
        df["days_to_next_major_unlock"] = [30, 60, 90, 120, 150, np.nan]
        df.loc["F", "has_unlock_record"] = False
        df["positive_catalyst_30d"] = True  # six known catalysts: live
        scores, _ = Layer2Scorer(RUN_DATE).score_events(df)
        assert scores["F"] == pytest.approx(100.0 / 3.0)
        assert scores["E"] > scores["F"]

    def test_the_monitoring_tag_still_forces_events_to_zero(self):
        df = frame(self.ASSETS)
        df["days_to_next_major_unlock"] = [30, 60, 90, 120, 150, 400]
        df.loc["F", "monitoring_tag_active"] = True
        scores, _ = Layer2Scorer(RUN_DATE).score_events(df)
        assert scores["F"] == 0.0

    def test_the_overhang_bonus_never_lifts_an_unmeasured_supply(self):
        df = frame(self.ASSETS)
        df["circulating_supply"] = [np.nan] + [80e6] * 5
        df.loc["A", "unlock_overhang_cleared"] = True
        scores, _ = Layer2Scorer(RUN_DATE).score_supply(df)
        assert pd.isna(scores["A"])

    def test_the_overhang_bonus_still_applies_to_a_measured_supply(self):
        df = frame(self.ASSETS)
        df.loc["A", "unlock_overhang_cleared"] = True
        scores, _ = Layer2Scorer(RUN_DATE).score_supply(df)
        assert scores["A"] == pytest.approx(min(100.0, scores["B"] + 25.0))

    def test_active_addresses_is_not_a_metric(self):
        """D-075. active_addresses_24h has no writer; it must not look live."""
        _, metrics = Layer2Scorer(RUN_DATE).score_fundamental(frame(self.ASSETS))
        assert "active_addresses" not in metrics


# ==============================================================================
# D-077 -- the momentum / flow block
# ==============================================================================
LAST_BAR = add_days(RUN_DATE, -1)


def bar_rows(
    asset: str,
    closes: list[float],
    taker_share: float | None = 0.6,
    last: str = LAST_BAR,
    volume: float = 1e6,
) -> list[dict]:
    """Daily bars ending on `last`, oldest first."""
    n = len(closes)
    return [
        {
            "base_asset": asset,
            "snapshot_date": add_days(last, -(n - 1 - i)),
            "close_usd": float(close),
            "volume_usd": volume,
            "taker_buy_usd": None if taker_share is None else volume * taker_share,
        }
        for i, close in enumerate(closes)
    ]


def zigzag(n: int = 90, up: float = 0.02, down: float = -0.01, start: float = 100.0):
    steps = [up if i % 2 else down for i in range(n - 1)]
    return list(start * np.exp(np.concatenate([[0.0], np.cumsum(steps)])))


class TestMomentumFeatures:
    def test_flow_is_signed_taker_share_over_seven_bars(self):
        got = l2.momentum_features(pd.DataFrame(bar_rows("A", zigzag())), RUN_DATE)
        assert got.loc["A", "flow_7d"] == pytest.approx(0.2)  # 2 * 0.6 - 1

    def test_flow_only_reads_the_last_seven_bars(self):
        rows = bar_rows("A", zigzag(), taker_share=0.1)
        for row in rows[-7:]:
            row["taker_buy_usd"] = row["volume_usd"] * 0.75
        got = l2.momentum_features(pd.DataFrame(rows), RUN_DATE)
        assert got.loc["A", "flow_7d"] == pytest.approx(0.5)

    def test_one_bar_without_taker_volume_leaves_flow_unmeasured(self):
        rows = bar_rows("A", zigzag())
        rows[-3]["taker_buy_usd"] = None
        got = l2.momentum_features(pd.DataFrame(rows), RUN_DATE)
        assert pd.isna(got.loc["A", "flow_7d"])
        assert pd.notna(got.loc["A", "vamom_7d"]), "the price metrics need no taker data"

    def test_vol_adjusted_momentum_uses_the_stated_windows(self):
        closes = zigzag()
        got = l2.momentum_features(pd.DataFrame(bar_rows("A", closes)), RUN_DATE)
        log_returns = np.diff(np.log(closes))
        expected_7 = (closes[-1] / closes[-8] - 1) / (
            np.std(log_returns[-30:], ddof=1) * np.sqrt(7)
        )
        expected_30 = (closes[-1] / closes[-31] - 1) / (
            np.std(log_returns[-60:], ddof=1) * np.sqrt(30)
        )
        assert got.loc["A", "vamom_7d"] == pytest.approx(expected_7)
        assert got.loc["A", "vamom_30d"] == pytest.approx(expected_30)

    def test_the_forming_bar_is_never_used(self):
        """A row dated run_date is CoinGecko's 03:10 price or, on a re-screen,
        a bar that closed after the screen. Either way it changes nothing."""
        rows = bar_rows("A", zigzag())
        clean = l2.momentum_features(pd.DataFrame(rows), RUN_DATE)
        partial = dict(
            rows[-1], snapshot_date=RUN_DATE, close_usd=rows[-1]["close_usd"] * 10,
            taker_buy_usd=rows[-1]["volume_usd"],
        )
        dirty = l2.momentum_features(pd.DataFrame([*rows, partial]), RUN_DATE)
        pd.testing.assert_frame_equal(clean, dirty)

    def test_a_stale_series_is_not_scored(self):
        """Newest bar two days old: momentum on it would describe the past."""
        rows = bar_rows("A", zigzag(), last=add_days(LAST_BAR, -1))
        got = l2.momentum_features(pd.DataFrame(rows), RUN_DATE)
        assert got.loc["A"].isna().all()

    def test_trend_states(self):
        rising = list(np.linspace(50, 150, 90))
        falling = list(np.linspace(150, 50, 90))
        # An uptrend whose last close falls under EMA20 while EMA20 is still
        # above EMA50: neither stack holds, so the state is neutral.
        # (EMA20 lags a linear trend by ~9.5 steps of 1.14, so it sits near 139.)
        pullback = list(np.linspace(50, 150, 89)) + [125.0]
        bars = pd.DataFrame(
            bar_rows("UP", rising) + bar_rows("DOWN", falling) + bar_rows("MIX", pullback)
        )
        got = l2.momentum_features(bars, RUN_DATE)
        assert got.loc["UP", "trend_1d"] == 100.0
        assert got.loc["DOWN", "trend_1d"] == 0.0
        assert got.loc["MIX", "trend_1d"] == 50.0

    def test_trend_needs_sixty_bars(self):
        got = l2.momentum_features(pd.DataFrame(bar_rows("A", zigzag(59))), RUN_DATE)
        assert pd.isna(got.loc["A", "trend_1d"])
        got = l2.momentum_features(pd.DataFrame(bar_rows("A", zigzag(60))), RUN_DATE)
        assert got.loc["A", "trend_1d"] in (0.0, 50.0, 100.0)

    def test_a_young_listing_has_no_long_momentum(self):
        got = l2.momentum_features(pd.DataFrame(bar_rows("A", zigzag(35))), RUN_DATE)
        assert pd.notna(got.loc["A", "vamom_7d"])
        assert pd.isna(got.loc["A", "vamom_30d"])

    def test_a_flat_series_has_no_volatility_to_scale_by(self):
        got = l2.momentum_features(pd.DataFrame(bar_rows("A", [1.0] * 90)), RUN_DATE)
        assert pd.isna(got.loc["A", "vamom_7d"])

    def test_no_bars_is_an_empty_frame(self):
        assert l2.momentum_features(pd.DataFrame(), RUN_DATE).empty


class TestMomentumLoader:
    @staticmethod
    def _write(db, rows, source="binance_klines"):
        upsert(
            db,
            "price_daily",
            [{**r, "source": source, "fetched_at_utc": "2026-09-09T03:05:00Z"} for r in rows],
        )
        db.commit()

    def test_reads_complete_kline_bars_only(self, db):
        self._write(db, bar_rows("A", zigzag()))
        self._write(db, bar_rows("A", [999.0], last=RUN_DATE), source="coingecko")
        bars = l2.load_momentum_bars(db, RUN_DATE, ["A"])
        assert bars["snapshot_date"].max() == LAST_BAR
        assert len(bars) == 90

    def test_a_close_only_coingecko_row_on_the_last_day_is_not_a_bar(self, db):
        """If klines missed yesterday, CoinGecko's 03:10 snapshot sits in its
        place. It is not a daily close: the asset goes unmeasured instead."""
        rows = bar_rows("A", zigzag())
        self._write(db, rows[:-1])
        self._write(db, rows[-1:], source="coingecko")
        got = l2.momentum_features(l2.load_momentum_bars(db, RUN_DATE, ["A"]), RUN_DATE)
        assert got.loc["A"].isna().all()

    def test_the_scoring_frame_carries_the_momentum_inputs(self, db):
        self._write(db, bar_rows("A", zigzag()))
        df = l2.load_scoring_frame(db, RUN_DATE, ["A", "B"])
        assert df.loc["A", "flow_7d"] == pytest.approx(0.2)
        assert pd.isna(df.loc["B", "flow_7d"])


# ==============================================================================
# D-076 -- score_version splits journal cohorts
# ==============================================================================
class TestScoreVersionCohorts:
    def test_entries_are_stamped_with_their_ranking_version(self, db):
        from src.journal import forward_returns as fr
        from tests.test_journal import RUN_DATE as J_DATE
        from tests.test_journal import _seed_day

        _seed_day(db)
        db.execute("UPDATE layer2_result SET score_version = 'gem-v2'")
        db.commit()
        fr.write_entries(J_DATE)
        versions = {r["score_version"] for r in db.query("SELECT score_version FROM journal_entry")}
        assert versions == {"gem-v2"}, "signals and their controls share the cohort"

    def test_an_unstamped_ranking_journals_as_gem_v1(self, db):
        """A late journal run over a pre-D-076 ranking must not relabel it."""
        from src.journal import forward_returns as fr
        from tests.test_journal import RUN_DATE as J_DATE
        from tests.test_journal import _seed_day

        _seed_day(db)
        fr.write_entries(J_DATE)
        versions = {r["score_version"] for r in db.query("SELECT score_version FROM journal_entry")}
        assert versions == {"gem-v1"}

    def test_statistics_never_blend_cohorts(self, db):
        from src.journal import forward_returns as fr
        from tests.test_journal import _seed_returns

        # gem-v1 (unstamped, legacy): a great record. gem-v2: a poor one.
        _seed_returns(
            db, [("A", False, 0.5, 0.5), ("B", True, 0.0, 0.0)], score_version=None, prefix="old"
        )
        _seed_returns(db, [("C", False, -0.2, -0.2), ("D", True, 0.0, 0.0)], prefix="new")
        current = fr.compute_statistics("30d")
        legacy = fr.compute_statistics("30d", score_version="gem-v1")
        assert current["score_version"] == "gem-v2"
        assert current["signal"]["n"] == 1
        assert current["signal"]["vs_btc"]["median"] == pytest.approx(-0.2)
        assert legacy["signal"]["vs_btc"]["median"] == pytest.approx(0.5)
        assert current["entries_total"] == 2

    def test_the_report_groups_by_version(self, db):
        from src.journal import forward_returns as fr
        from tests.test_journal import _seed_returns

        _seed_returns(
            db, [("A", False, 0.5, 0.5), ("B", True, 0.0, 0.0)], score_version=None, prefix="old"
        )
        text = fr.render_report()
        assert "## Cohort `gem-v2` (current method)" in text
        assert "## Cohort `gem-v1`" in text
        current, legacy = text.split("## Cohort `gem-v1`")
        assert "Not enough data yet" in current, "the old record must not appear as the new one's"
        assert "Edge over a random L1 survivor" in legacy

    def test_the_cli_report_prints_the_cohorts(self, db):
        from typer.testing import CliRunner

        from src.cli import app

        result = CliRunner().invoke(app, ["journal", "--report"])
        assert result.exit_code == 0, result.output
        assert "Cohort `gem-v2`" in result.output


# ==============================================================================
# Publishing: the names the dashboard is built against
# ==============================================================================
class TestPublishedContract:
    DATE = "2026-06-01"
    ASSETS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]

    def _seed(self, db, version: str | None = "gem-v2") -> None:
        import json

        upsert(
            db,
            "layer1_result",
            [
                {
                    "run_date": self.DATE,
                    "base_asset": a,
                    "passed": 1,
                    "failed_checks": "[]",
                    "check_values": "{}",
                    "fetched_at_utc": f"{self.DATE}T03:10:00Z",
                }
                for a in self.ASSETS
            ],
        )
        rows = []
        for rank, asset in enumerate(self.ASSETS, start=1):
            row = {
                "run_date": self.DATE,
                "base_asset": asset,
                "total_score": 90.0 - rank,
                "score_fundamental": None if asset == "FFF" else 50.0,
                "score_supply": 60.0,
                "score_sector": None,
                "score_drawdown": 40.0,
                "score_events": 50.0,
                "score_attention": None,
                "rank": rank,
                "universe_size": len(self.ASSETS),
                "percentiles": json.dumps({}),
                "fetched_at_utc": f"{self.DATE}T03:10:00Z",
            }
            if version is not None:
                row.update(
                    score_momentum=70.0,
                    coverage=0.75 if asset == "FFF" else 1.0,
                    score_version=version,
                )
            rows.append(row)
        upsert(db, "layer2_result", rows)
        db.commit()

    def test_latest_json_carries_coverage_version_momentum_and_live_blocks(self, db):
        from src.report import daily, publish

        self._seed(db)
        out = publish.build_latest(db, self.DATE, daily.gather(db, self.DATE))
        assert out["score_version"] == "gem-v2"
        # fundamental: 5 of 6 measured (>= the floor of 5); sector and
        # attention: nobody.
        assert out["live_blocks"] == ["fundamental", "supply", "momentum", "events", "drawdown"]
        row = {r["asset"]: r for r in out["ranked"]}["FFF"]
        assert row["coverage"] == 0.75
        assert row["score_version"] == "gem-v2"
        assert row["blocks"]["momentum"] == 70.0
        assert set(row["blocks"]) == set(BLOCKS)

    def test_a_pre_d076_row_publishes_as_gem_v1_with_no_coverage(self, db):
        from src.report import daily, publish

        self._seed(db, version=None)
        out = publish.build_latest(db, self.DATE, daily.gather(db, self.DATE))
        assert out["score_version"] == "gem-v1"
        assert out["ranked"][0]["coverage"] is None
        assert out["ranked"][0]["blocks"]["momentum"] is None
        assert "momentum" not in out["live_blocks"]

    def test_asset_json_layer2_carries_the_same_fields(self, db):
        from src.report import publish

        self._seed(db)
        l1 = db.query_one(
            "SELECT * FROM layer1_result WHERE run_date = ? AND base_asset = 'FFF'", (self.DATE,)
        )
        out = publish.build_asset(db, self.DATE, "FFF", l1)["layer2"]
        assert out["coverage"] == 0.75
        assert out["score_version"] == "gem-v2"
        assert out["blocks"]["momentum"] == 70.0

    def test_journal_json_splits_statistics_by_version(self, db):
        from src.report import publish
        from tests.test_journal import _seed_returns

        _seed_returns(
            db, [("A", False, 0.5, 0.5), ("B", True, 0.0, 0.0)], score_version=None, prefix="old"
        )
        out = publish.build_journal(db)
        assert out["score_version"] == "gem-v2"
        assert set(out["statistics_by_version"]) == {"gem-v2", "gem-v1"}
        assert out["statistics"]["30d"]["signal"] == {"n": 0}, "old cohort must not leak in"
        assert out["statistics_by_version"]["gem-v1"]["30d"]["signal"]["n"] == 1
        assert {e["score_version"] for e in out["entries"]} == {"gem-v1"}

    def test_the_markdown_table_shows_momentum_and_coverage(self, db):
        from src.report import daily

        self._seed(db)
        text = daily.render(daily.gather(db, self.DATE))
        assert "| Cov |" in text and "Mom" in text
        assert "| 75% |" in text
        assert "redistributed" not in text, "the old renormalisation note is gone"
        blocks = daily.data_quality(db, self.DATE)["blocks"]
        assert "momentum" in blocks


class TestTakerBuyCollection:
    """D-077: kline index 10 is taker buy QUOTE volume; it lands in
    price_daily.taker_buy_usd and nothing downstream can blank it."""

    AS_OF = datetime(2026, 6, 10, 3, 10, tzinfo=timezone.utc)

    @staticmethod
    def _bar(taker_quote: str = "600.0", taker_base: str = "6.0") -> list:
        from tests.test_klines import bar

        raw = bar("2026-06-01", quote_volume=1_000.0)
        raw[9], raw[10] = taker_base, taker_quote
        return raw

    def test_index_10_is_stored_and_not_divided(self):
        from src.collectors.klines import BinanceKlinesCollector

        rows = BinanceKlinesCollector().transform({"1000PEPEUSDT": [self._bar()]}, self.AS_OF)
        # Quote units, like volume_usd: the 1000x multiplier must not touch it,
        # and index 9 (base units) must not be mistaken for it.
        assert rows[0]["taker_buy_usd"] == pytest.approx(600.0)

    def test_a_bar_without_the_taker_field_stores_none(self):
        from src.collectors.klines import BinanceKlinesCollector

        rows = BinanceKlinesCollector().transform({"BTCUSDT": [self._bar()[:9]]}, self.AS_OF)
        assert rows[0]["taker_buy_usd"] is None
        assert rows[0]["close_usd"] is not None

    @staticmethod
    def _row(source, taker=None, close=1.0):
        row = {
            "snapshot_date": "2026-06-01",
            "base_asset": "BTC",
            "close_usd": close,
            "volume_usd": 1000.0,
            "source": source,
            "fetched_at_utc": "2026-06-02T03:10:00Z",
        }
        if source == "binance_klines":
            row["taker_buy_usd"] = taker
        return row

    def _taker(self, db):
        return db.scalar("SELECT taker_buy_usd FROM price_daily WHERE base_asset = 'BTC'")

    def test_a_coingecko_row_never_blanks_a_kline_taker_value(self, db):
        upsert(db, "price_daily", [self._row("binance_klines", taker=600.0)])
        upsert(db, "price_daily", [self._row("coingecko", close=2.0)])
        assert self._taker(db) == 600.0

    def test_a_kline_row_fills_the_taker_value_over_a_coingecko_row(self, db):
        upsert(db, "price_daily", [self._row("coingecko")])
        upsert(db, "price_daily", [self._row("binance_klines", taker=600.0)])
        assert self._taker(db) == 600.0

    def test_a_kline_refetch_without_taker_keeps_the_value_on_file(self, db):
        upsert(db, "price_daily", [self._row("binance_klines", taker=600.0)])
        upsert(db, "price_daily", [self._row("binance_klines", taker=None, close=3.0)])
        assert self._taker(db) == 600.0
        assert db.scalar("SELECT close_usd FROM price_daily") == 3.0

    def test_request_limits_sit_in_the_cheap_weight_bands(self, monkeypatch):
        """Binance charges klines by `limit`: [1,100) 1, [100,500) 2, [500,1000] 5.
        500 was weight 5. The daily run needs ~8 bars and pays weight 1."""
        import asyncio

        from src.collectors import klines

        assert klines.PAGE_LIMIT == 499
        assert klines.INCREMENTAL_PAGE_LIMIT < 100

        for backfill, expected in ((False, klines.INCREMENTAL_PAGE_LIMIT), (True, 499)):
            collector = klines.BinanceKlinesCollector(backfill=backfill)
            seen: list[int] = []

            async def fake_request(client, method, url, params=None, **kwargs):
                seen.append(params["limit"])
                return []

            monkeypatch.setattr(collector, "request_json", fake_request)
            asyncio.run(collector._fetch_symbol(None, "BTCUSDT", "2026-06-01"))
            assert seen == [expected]


class TestMomentumBlock:
    ASSETS = ["A", "B", "C", "D", "E", "X"]

    def test_missing_taker_data_contributes_zero_not_a_renormalised_score(self):
        """X has no flow reading; E has the WORST flow and is otherwise X's
        twin. E must still score above X: no reading earns nothing, and must
        never earn more than the worst real one."""
        df = frame(self.ASSETS)
        df["flow_7d"] = [0.5, 0.4, 0.3, 0.2, -0.9, np.nan]
        df["vamom_7d"] = [1.0, 0.8, 0.6, 0.4, 2.0, 2.0]
        df["vamom_30d"] = [1.0, 0.8, 0.6, 0.4, 2.0, 2.0]
        df["trend_1d"] = [50.0, 50.0, 50.0, 50.0, 100.0, 100.0]
        scores, metrics = Layer2Scorer(RUN_DATE).score_momentum(df)
        assert pd.isna(metrics["flow_7d"]["X"])
        assert scores["E"] > scores["X"]
        assert pd.notna(scores["X"]), "X is still measured on the price metrics"

    def test_a_frame_without_momentum_inputs_leaves_the_block_dark(self):
        scored = Layer2Scorer(RUN_DATE).score(frame(self.ASSETS))
        assert scored["momentum"].isna().all()
        assert "momentum" not in scored.attrs["live_blocks"]

    def test_run_layer2_persists_momentum_coverage_and_version(self, db, monkeypatch):
        df = frame(self.ASSETS)
        df["flow_7d"] = [0.5, 0.4, 0.3, 0.2, 0.1, 0.0]
        monkeypatch.setattr(l2, "load_scoring_frame", lambda *a, **k: df)
        l2.run_layer2(db, RUN_DATE, self.ASSETS)
        rows = db.query(
            "SELECT base_asset, score_momentum, coverage, score_version FROM layer2_result"
        )
        assert len(rows) == 6
        assert all(r["score_version"] == "gem-v2" for r in rows)
        assert all(r["score_momentum"] is not None for r in rows)
        assert all(0.0 < r["coverage"] <= 1.0 for r in rows)
        live = l2.live_blocks_for_run(db, RUN_DATE)
        assert "momentum" in live
        assert "attention" not in live

    def test_the_correlation_report_includes_momentum(self, db, monkeypatch):
        df = frame(self.ASSETS)
        df["flow_7d"] = [0.5, 0.4, 0.3, 0.2, 0.1, 0.0]
        monkeypatch.setattr(l2, "load_scoring_frame", lambda *a, **k: df)
        l2.run_layer2(db, RUN_DATE, self.ASSETS)
        report = l2.block_correlation_report(db, min_days=1)
        assert "score_momentum" in report["matrix"]
