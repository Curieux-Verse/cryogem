"""
Phase 8: the backtest harness.

A backtest is the easiest thing in this project to make lie, so almost every
test here is about a way of lying: a universe that only contains survivors, a
cost model that rounds to zero, a split that puts the future in the training
set, a holdout looked at twice, and a harness that disagrees with the journal
because one of the two can see forward.
"""

from __future__ import annotations

import math
import statistics

import pytest

from src.backtest import harness as bt
from src.backtest import universe as btu
from src.db.writes import upsert
from src.timeutil import add_days

START = "2026-01-01"


def _universe_row(day: str, symbol: str, base: str, status: str = "TRADING") -> dict:
    return {
        "snapshot_date": day,
        "exchange": "binance",
        "symbol": symbol,
        "base_asset": base,
        "quote_asset": "USDT",
        "contract_type": "PERPETUAL",
        "status": status,
        "onboard_date": "2024-01-01",
        "price_multiplier": 1,
        "fetched_at_utc": f"{day}T03:10:00Z",
    }


def seed_history(db, days: int = 130, assets=("AAA", "BBB", "CCC", "DDD")) -> list[str]:
    """A synthetic run of recorded screening days.

    Deliberately seeds the SAME tables production writes, through the same
    upsert, so a schema or key change breaks these tests too.
    """
    dates = [add_days(START, i) for i in range(days)]
    prices, l1, l2, snapshots = [], [], [], []

    for index, day in enumerate(dates + [add_days(START, days + 40)]):
        # BTC drifts up 0.2%/day; assets drift a little faster, so a naive
        # backtest would look profitable and only the vs-BTC number is honest.
        btc = 60_000.0 * (1.002**index)
        prices.append(_price(day, "BTC", btc))
        for offset, asset in enumerate(assets):
            price = 100.0 * (1.003**index) * (1 + 0.01 * offset)
            prices.append(_price(day, asset, price))

    for day in dates:
        for asset in assets:
            snapshots.append(_universe_row(day, f"{asset}USDT", asset))
            l1.append(
                {
                    "run_date": day,
                    "base_asset": asset,
                    "passed": 1,
                    "failed_checks": "[]",
                    "check_values": "{}",
                    "fetched_at_utc": f"{day}T03:10:00Z",
                }
            )
        for rank, asset in enumerate(assets[:2], start=1):
            l2.append(
                {
                    "run_date": day,
                    "base_asset": asset,
                    "total_score": 80.0 - rank,
                    "score_fundamental": 70.0 + rank,
                    "score_supply": 60.0,
                    "score_sector": None,
                    "score_drawdown": 50.0,
                    "score_events": 40.0,
                    "score_attention": None,
                    "rank": rank,
                    "universe_size": len(assets),
                    "percentiles": "{}",
                    "fetched_at_utc": f"{day}T03:10:00Z",
                }
            )

    upsert(db, "price_daily", prices)
    upsert(db, "universe_snapshot", snapshots)
    upsert(db, "layer1_result", l1)
    upsert(db, "layer2_result", l2)
    db.commit()
    return dates


def _price(day: str, asset: str, close: float) -> dict:
    return {
        "snapshot_date": day,
        "base_asset": asset,
        "open_usd": close,
        "high_usd": close * 1.05,
        "low_usd": close * 0.93,
        "close_usd": close,
        "volume_usd": 1e6,
        "source": "binance_klines",
        "fetched_at_utc": f"{day}T00:00:00Z",
    }


class TestGating:
    def test_refuses_to_run_on_insufficient_history(self, db):
        """A warning above a plausible Sharpe ratio is a warning nobody reads,
        and the number then gets quoted without it."""
        seed_history(db, days=10)
        with pytest.raises(bt.InsufficientHistory, match="before any number"):
            bt.assert_runnable(db)

    def test_the_refusal_says_how_far_short_the_data_is(self, db):
        seed_history(db, days=10)
        text = bt.render_report(START, add_days(START, 60))
        assert "**Not run.**" in text
        assert "10 screening days" in text
        assert f"{bt.MIN_SCREENING_DAYS} days" in text
        # No metrics table at all: a zero Sharpe on nine trades reads as "the
        # strategy does not work" rather than "nine trades".
        assert "Sharpe" not in text

    def test_runs_once_there_is_enough(self, db):
        seed_history(db, days=130)
        state = bt.assert_runnable(db)
        assert state["screening_days"] == 130


class TestPointInTimeUniverse:
    def test_a_delisted_symbol_is_visible_in_the_window(self, db):
        """A delisted symbol VANISHES from exchangeInfo. Using today's list over
        last year deletes exactly the assets L1 exists to catch."""
        seed_history(db, days=40)
        # ZZZ trades for the first 20 days and then disappears.
        upsert(
            db,
            "universe_snapshot",
            [_universe_row(add_days(START, i), "ZZZUSDT", "ZZZ") for i in range(20)],
        )
        db.commit()

        early = btu.point_in_time_universe(db, add_days(START, 5))
        late = btu.point_in_time_universe(db, add_days(START, 30))
        assert "ZZZUSDT" in early
        assert "ZZZUSDT" not in late

        gone = btu.delistings_between(db, add_days(START, 5), add_days(START, 30))
        assert [g["symbol"] for g in gone] == ["ZZZUSDT"]
        assert gone[0]["last_seen"] == add_days(START, 19)

    def test_a_missing_day_falls_back_never_forward(self, db):
        """A later snapshot contains symbols that had not yet listed, and using
        one would let the backtest trade an asset before it existed."""
        seed_history(db, days=10)
        upsert(db, "universe_snapshot", [_universe_row(add_days(START, 30), "NEWUSDT", "NEW")])
        db.commit()

        # Day 20 has no snapshot. It must resolve to day 9, not day 30.
        symbols = btu.point_in_time_universe(db, add_days(START, 20))
        assert "NEWUSDT" not in symbols
        assert "AAAUSDT" in symbols

    def test_no_snapshot_at_all_returns_empty_not_todays_list(self, db):
        seed_history(db, days=10)
        assert btu.point_in_time_universe(db, "2020-01-01") == []

    def test_coverage_reports_holes(self, db):
        seed_history(db, days=10)
        out = btu.coverage(db, START, add_days(START, 19))
        assert out["expected_days"] == 20
        assert out["days_with_snapshot"] == 10
        assert out["completeness"] == 0.5


class TestCosts:
    def test_fees_are_charged_both_ways(self):
        """Entry and exit are two fills, not one."""
        assert bt.fee_cost(5.0) == pytest.approx(0.001)

    def test_missing_depth_costs_the_full_band_not_zero(self, db):
        """Zero would mean 'free to trade', which is the opposite of what
        'we do not know the exit cost' implies."""
        assert bt.slippage_cost(db, "AAA", START, 10_000.0) == pytest.approx(0.02)

    def test_slippage_scales_with_position_against_recorded_depth(self, db):
        seed_history(db, days=10)
        upsert(
            db,
            "depth_snapshot",
            [
                {
                    "ts_utc": f"{START}T03:10:00Z",
                    "exchange": "binance",
                    "symbol": "AAAUSDT",
                    "market_type": "spot",
                    "bid_depth_0p5": 10_000.0,
                    "bid_depth_1p0": 50_000.0,
                    "bid_depth_2p0": 100_000.0,
                    "ask_depth_0p5": 10_000.0,
                    "ask_depth_1p0": 50_000.0,
                    "ask_depth_2p0": 100_000.0,
                    "fetched_at_utc": f"{START}T03:10:00Z",
                }
            ],
        )
        db.commit()

        small = bt.slippage_cost(db, "AAA", add_days(START, 5), 10_000.0)
        large = bt.slippage_cost(db, "AAA", add_days(START, 5), 200_000.0)
        assert small == pytest.approx(0.002)
        assert large > 0.02  # beyond the recorded band there is no liquidity
        assert large > small

    def test_funding_is_zero_only_when_none_was_recorded(self, db):
        assert bt.funding_cost(db, "AAA", START, add_days(START, 30)) == 0.0

    def test_cost_drag_is_non_trivial(self, db):
        """Acceptance criterion: if costs are ~0, the model is not applied."""
        seed_history(db, days=130)
        trades = bt.replay(db, START, add_days(START, 129), horizon="30d")
        signals = [t for t in trades if not t.is_control]
        assert signals
        stats = bt.describe(signals, "signal")
        assert stats["cost_drag_share_of_gross"] > 0.01
        assert all(t.net_return < t.gross_return for t in signals)


class TestSplit:
    def test_the_split_is_chronological_not_random(self):
        """A random split puts future days in the development set, and with
        overlapping 30-day horizons that leaks the answer directly."""
        dates = [add_days(START, i) for i in range(90)]
        development, holdout = bt.split_dates(dates, holdout_fraction=1 / 3)
        assert development[-1] < holdout[0]
        assert len(holdout) == 30
        assert development + holdout == dates

    def test_the_development_split_ends_one_horizon_before_the_holdout(self):
        """D-063. A development trade exiting inside the holdout is holdout data."""
        dates = [add_days(START, i) for i in range(90)]
        development, holdout = bt.split_dates(dates, holdout_fraction=1 / 3, embargo_days=30)
        assert add_days(development[-1], 30) < holdout[0]
        assert len(holdout) == 30

    def test_an_empty_date_list_splits_to_nothing(self):
        assert bt.split_dates([]) == ([], [])

    def test_a_second_holdout_run_is_labelled_as_no_longer_out_of_sample(self, db):
        """'Test once' is a promise no code can keep by asking politely."""
        seed_history(db, days=130)
        first = bt.run_backtest(START, add_days(START, 129), holdout=True)
        assert first["holdout_runs_recorded"] == 1
        assert "holdout_warning" not in first

        second = bt.run_backtest(START, add_days(START, 129), holdout=True)
        assert second["holdout_runs_recorded"] == 2
        assert "no longer out-of-sample" in second["holdout_warning"]


class TestReplay:
    def test_it_replays_recorded_rankings_and_does_not_rescore(self, db):
        """Fundamentals, holder data and unlock calendars are all revised after
        the fact. Re-screening a historic date with today's tables would score
        assets on information that did not exist."""
        dates = seed_history(db, days=130)
        trades = bt.replay(db, dates[0], dates[-1], horizon="30d")
        signals = [t for t in trades if not t.is_control]
        # Only the two assets that were RANKED on the day appear as signals.
        assert {t.base_asset for t in signals} == {"AAA", "BBB"}
        assert all(t.rank in (1, 2) for t in signals)

    def test_controls_come_from_survivors_outside_the_ranking(self, db):
        dates = seed_history(db, days=130)
        trades = bt.replay(db, dates[0], dates[-1], horizon="30d")
        controls = {t.base_asset for t in trades if t.is_control}
        assert controls
        assert not controls & {"AAA", "BBB"}

    def test_returns_are_relative_to_btc(self, db):
        """The assets drift up 0.3%/day and BTC 0.2%/day. Raw returns are
        positive for everything; only the vs-BTC number is informative."""
        dates = seed_history(db, days=130)
        trades = bt.replay(db, dates[0], dates[-1], horizon="30d")
        signal = next(t for t in trades if not t.is_control)
        assert signal.gross_return > 0
        assert signal.net_return_vs_btc < signal.gross_return

    def test_a_day_without_a_btc_price_is_skipped_entirely(self, db):
        """Substituting zero would quietly convert a missing benchmark into an
        outperforming one."""
        dates = seed_history(db, days=130)
        db.execute("DELETE FROM price_daily WHERE base_asset = 'BTC'")
        db.commit()
        assert bt.replay(db, dates[0], dates[-1], horizon="30d") == []

    def test_excursions_are_recorded_on_each_trade(self, db):
        dates = seed_history(db, days=130)
        trades = bt.replay(db, dates[0], dates[-1], horizon="30d")
        signal = next(t for t in trades if not t.is_control)
        assert signal.max_adverse is not None
        assert signal.max_favourable is not None
        assert signal.max_adverse < 0 < signal.max_favourable


class TestMetrics:
    def test_median_and_mean_are_both_reported(self, db):
        dates = seed_history(db, days=130)
        trades = bt.replay(db, dates[0], dates[-1])
        stats = bt.describe([t for t in trades if not t.is_control], "signal")
        assert "median_vs_btc" in stats
        assert "mean_vs_btc" in stats
        assert "median_gross" in stats and "mean_gross" in stats

    def test_max_drawdown_is_never_positive(self, db):
        dates = seed_history(db, days=130)
        curve = bt.equity_curve(bt.replay(db, dates[0], dates[-1]))
        assert curve
        assert bt.max_drawdown(curve) <= 0

    def test_max_drawdown_measures_peak_to_trough(self):
        """The assertion above cannot fail: `worst` starts at zero."""
        curve = [{"equity": value} for value in (1.0, 1.2, 0.9, 1.1)]
        assert bt.max_drawdown(curve) == pytest.approx(-0.25)

    def test_the_equity_curve_does_not_compound_overlapping_trades(self, db):
        """D-063. 120 daily entries of a 30-day trade compounded as 120 periods."""
        dates = seed_history(db, days=130)
        curve = bt.equity_curve(bt.replay(db, dates[0], dates[-1], horizon="30d"), 30)
        entered = [point["run_date"] for point in curve]
        assert len(entered) >= 2
        assert all(add_days(a, 30) <= b for a, b in zip(entered, entered[1:]))

    def test_sortino_uses_downside_deviation_over_every_return(self):
        """D-063. Two identical losses have zero spread between them, which the
        old version divided by; the downside deviation is not zero."""
        returns = [0.10, -0.05, -0.05, 0.20]
        downside = math.sqrt((0.0025 + 0.0025) / 4)
        expected = round(statistics.fmean(returns) / downside, 4)
        assert bt._ratio(returns, downside_only=True) == pytest.approx(expected)

    def test_a_delisted_signal_is_a_trade_not_a_silent_skip(self, db):
        """D-061."""
        dates = seed_history(db, days=130)
        gone = add_days(START, 10)
        db.execute("DELETE FROM price_daily WHERE base_asset = 'AAA' AND snapshot_date > ?", (gone,))
        db.execute(
            "DELETE FROM universe_snapshot WHERE base_asset = 'AAA' AND snapshot_date > ?", (gone,)
        )
        db.commit()
        trades = bt.replay(db, dates[0], dates[5], horizon="30d")
        delisted = [t for t in trades if t.base_asset == "AAA" and not t.is_control]
        assert delisted
        assert all(t.exit_reason == "delisted" for t in delisted)

    def test_ratios_return_none_rather_than_a_number_from_one_point(self):
        assert bt._ratio([0.05]) is None
        assert bt._ratio([0.05, 0.05]) is None  # zero deviation

    def test_block_attribution_refuses_small_samples(self, db):
        seed_history(db, days=130)
        trades = bt.replay(db, START, add_days(START, 40))
        out = bt.block_attribution(trades)
        assert set(out) == {
            "fundamental",
            "supply",
            "sector",
            "events",
            "attention",
            "drawdown",
        }
        # sector and attention were never scored: None, never zero.
        assert out["sector"]["correlation"] is None

    def test_regime_is_computed_from_recorded_prices(self, db):
        seed_history(db, days=130)
        assert bt.regime_on(db, add_days(START, 60)) in {
            "btc_up",
            "btc_flat",
            "btc_down",
        }
        assert bt.regime_on(db, "2019-01-01") == "unknown"

    def test_the_report_splits_by_regime(self, db):
        seed_history(db, days=130)
        text = bt.render_report(START, add_days(START, 129))
        assert "## By regime" in text
        assert "beta bet in disguise" in text
        assert "## Block attribution" in text


class TestJournalReconciliation:
    def test_harness_and_journal_agree_on_the_same_signals(self, db):
        """Spec 13 acceptance criterion. If they disagree, one of them has
        look-ahead bias, and finding out which is worth more than either
        number."""
        from src.journal import forward_returns as fr

        seed_history(db, days=130)
        # Journal a handful of days, then let both sides compute 30d returns.
        for day in [add_days(START, i) for i in range(0, 60, 10)]:
            fr.write_entries(day)
        fr.backfill_returns(add_days(START, 129))

        out = bt.reconcile_with_journal("30d")
        assert out["comparable"] is True
        assert out["compared"] > 0
        assert out["verdict"] == "agree", out["disagreements"]

    def test_harness_and_journal_agree_across_a_missing_price_day(self, db):
        """The first reconciliation test had no gaps, so it could not catch two
        different lookback rules."""
        from src.journal import forward_returns as fr

        seed_history(db, days=130)
        db.execute(
            "DELETE FROM price_daily WHERE base_asset = 'AAA' AND snapshot_date = ?",
            (add_days(START, 40),),
        )
        db.commit()
        for day in [add_days(START, i) for i in range(0, 60, 10)]:
            fr.write_entries(day)
        fr.backfill_returns(add_days(START, 129))

        out = bt.reconcile_with_journal("30d")
        assert out["compared"] > 0
        assert out["verdict"] == "agree", out["disagreements"]

    def test_reconciliation_says_so_when_there_is_nothing_to_compare(self, db):
        seed_history(db, days=130)
        out = bt.reconcile_with_journal("30d")
        assert out["comparable"] is False
        assert "no completed returns" in out["reason"]


class TestAudit:
    def test_the_holdout_log_cannot_overwrite_its_own_rows(self, db):
        """The first version keyed run_id on (window, timestamp-to-the-second):
        two runs in the same second collided and the counter stayed at 1, so
        the mechanism guarding against a re-looked holdout was itself
        defeatable."""
        seed_history(db, days=130)
        window = {"start": START, "end": add_days(START, 129)}
        counts = [
            bt.record_holdout_run(db, "holdout", window, "30d", {"trades": 1})
            for _ in range(3)
        ]
        assert counts == [1, 2, 3]
        assert db.scalar("SELECT COUNT(*) FROM backtest_run") == 3
