"""
Phase 6: the journal.

The journal is the only component that produces truth, so these tests are less
about arithmetic and more about the properties that make the numbers honest:
append-only, a random control, both central tendencies, and excursions.
"""

from __future__ import annotations

import pytest

from src.db.writes import upsert
from src.journal import forward_returns as fr

RUN_DATE = "2026-06-01"


def _price(db, date: str, asset: str, close: float, high=None, low=None) -> None:
    upsert(
        db,
        "price_daily",
        [
            {
                "snapshot_date": date,
                "base_asset": asset,
                "open_usd": close,
                "high_usd": high if high is not None else close,
                "low_usd": low if low is not None else close,
                "close_usd": close,
                "volume_usd": 1e6,
                "source": "binance_klines",
                "fetched_at_utc": f"{date}T00:00:00Z",
            }
        ],
    )


def _seed_day(
    db,
    run_date: str = RUN_DATE,
    ranked: list[str] | None = None,
    extra_survivors: list[str] | None = None,
    btc_price: float | None = 60_000.0,
) -> None:
    """One complete screening day: survivors, a ranking, and prices.

    Written through the same `upsert` the collectors use, so a schema change
    that breaks production breaks these tests too.
    """
    ranked = ranked if ranked is not None else ["AAA", "BBB"]
    extra = extra_survivors if extra_survivors is not None else ["CCC", "DDD", "EEE"]

    for asset in ranked + extra:
        _price(db, run_date, asset, 100.0)
        upsert(
            db,
            "layer1_result",
            [
                {
                    "run_date": run_date,
                    "base_asset": asset,
                    "passed": 1,
                    "failed_checks": None,
                    "check_values": '{"oi_to_mcap": 0.1}',
                    "fetched_at_utc": f"{run_date}T00:00:00Z",
                }
            ],
        )
    if btc_price is not None:
        _price(db, run_date, "BTC", btc_price)

    for position, asset in enumerate(ranked, start=1):
        upsert(
            db,
            "layer2_result",
            [
                {
                    "run_date": run_date,
                    "base_asset": asset,
                    "total_score": 90.0 - position,
                    "score_fundamental": 80.0,
                    "score_supply": 70.0,
                    "score_sector": None,
                    "score_drawdown": 60.0,
                    "score_events": 50.0,
                    "score_attention": None,
                    "rank": position,
                    "universe_size": len(ranked) + len(extra),
                    "percentiles": "{}",
                    "fetched_at_utc": f"{run_date}T00:00:00Z",
                }
            ],
        )
    db.commit()


class TestEntryWriting:
    def test_signals_and_controls_are_both_journalled(self, db):
        _seed_day(db)
        written = fr.write_entries(RUN_DATE)
        assert written == 4  # 2 signals + 2 controls

        rows = db.query("SELECT base_asset, is_control, rank FROM journal_entry")
        signals = {r["base_asset"] for r in rows if not r["is_control"]}
        controls = {r["base_asset"] for r in rows if r["is_control"]}
        assert signals == {"AAA", "BBB"}
        assert len(controls) == 2

    def test_control_is_drawn_from_survivors_outside_the_ranking(self, db):
        """A control drawn from the ranked list would compare the picks to
        themselves and always show zero edge."""
        _seed_day(db)
        fr.write_entries(RUN_DATE)

        controls = {
            r["base_asset"]
            for r in db.query("SELECT base_asset FROM journal_entry WHERE is_control = 1")
        }
        assert controls.issubset({"CCC", "DDD", "EEE"})
        assert not controls & {"AAA", "BBB"}

    def test_no_control_when_every_survivor_is_ranked(self, db):
        """With nothing left to draw from, write the signals and no control --
        never fall back to sampling the ranked list itself."""
        _seed_day(db, ranked=["AAA", "BBB"], extra_survivors=[])
        assert fr.write_entries(RUN_DATE) == 2
        assert db.scalar("SELECT COUNT(*) FROM journal_entry WHERE is_control = 1") == 0

    def test_rerun_is_idempotent(self, db):
        _seed_day(db)
        first = fr.write_entries(RUN_DATE)
        before = db.query_one(
            "SELECT created_at_utc FROM journal_entry WHERE base_asset = 'AAA'"
        )["created_at_utc"]

        fr.write_entries(RUN_DATE)
        assert db.scalar("SELECT COUNT(*) FROM journal_entry") == first
        after = db.query_one(
            "SELECT created_at_utc FROM journal_entry WHERE base_asset = 'AAA'"
        )["created_at_utc"]
        # INSERT OR IGNORE: the ORIGINAL row survives. A re-run must not restamp
        # history, because that would let a later run rewrite what was recorded.
        assert after == before

    def test_nothing_is_written_without_a_btc_price(self, db):
        """Every return is measured against BTC. A row missing that reference
        could never be repaired -- the table is append-only."""
        _seed_day(db, btc_price=None)
        assert fr.write_entries(RUN_DATE) == 0
        assert db.scalar("SELECT COUNT(*) FROM journal_entry") == 0

    def test_asset_without_a_price_is_skipped_not_zeroed(self, db):
        _seed_day(db, ranked=["AAA", "BBB"])
        db.execute("DELETE FROM price_daily WHERE base_asset = 'BBB'")
        db.commit()
        fr.write_entries(RUN_DATE)
        assets = {r["base_asset"] for r in db.query("SELECT base_asset FROM journal_entry")}
        assert "AAA" in assets
        assert "BBB" not in assets

    def test_no_ranking_writes_nothing(self, db):
        assert fr.write_entries("2026-01-01") == 0

    def test_a_rerun_with_more_survivors_draws_no_new_controls(self, db):
        """D-062. Control ids are per asset, so a re-run whose survivor pool had
        changed inserted a second, different control group beside the first."""
        _seed_day(db)
        fr.write_entries(RUN_DATE)
        query = "SELECT base_asset FROM journal_entry WHERE is_control = 1"
        before = {r["base_asset"] for r in db.query(query)}
        for asset in ("FFF", "GGG", "HHH"):
            _price(db, RUN_DATE, asset, 100.0)
            upsert(
                db,
                "layer1_result",
                [
                    {
                        "run_date": RUN_DATE,
                        "base_asset": asset,
                        "passed": 1,
                        "failed_checks": None,
                        "check_values": "{}",
                        "fetched_at_utc": f"{RUN_DATE}T00:00:00Z",
                    }
                ],
            )
        db.commit()
        fr.write_entries(RUN_DATE)
        assert {r["base_asset"] for r in db.query(query)} == before

    def test_the_control_draw_does_not_depend_on_row_order(self, db):
        """D-062. The pool is sorted before the seeded sample."""
        import random

        _seed_day(db)
        expected = random.Random(RUN_DATE).sample(["CCC", "DDD", "EEE"], k=2)
        assert fr.draw_controls(db, RUN_DATE) == expected

    def test_layer_values_are_captured_at_signal_time(self, db):
        """The journal records what the system BELIEVED on the day. Re-deriving
        it later would use today's data and quietly rewrite the past."""
        _seed_day(db)
        fr.write_entries(RUN_DATE)
        row = db.query_one(
            "SELECT layer1_values, layer2_values FROM journal_entry WHERE base_asset = 'AAA'"
        )
        assert "oi_to_mcap" in row["layer1_values"]
        assert "score_fundamental" in row["layer2_values"]


class TestAppendOnly:
    def test_delete_is_refused_by_the_database(self, db):
        _seed_day(db)
        fr.write_entries(RUN_DATE)
        with pytest.raises(Exception, match="append-only"):
            db.execute("DELETE FROM journal_entry WHERE base_asset = 'AAA'")

    def test_update_is_refused_by_the_database(self, db):
        _seed_day(db)
        fr.write_entries(RUN_DATE)
        with pytest.raises(Exception, match="append-only"):
            db.execute("UPDATE journal_entry SET total_score = 100 WHERE base_asset = 'AAA'")

    def test_forward_return_delete_is_refused(self, db):
        _seed_day(db)
        fr.write_entries(RUN_DATE)
        _price(db, "2026-06-02", "AAA", 110.0)
        _price(db, "2026-06-02", "BTC", 60_000.0)
        db.commit()
        fr.backfill_returns("2026-06-03")
        with pytest.raises(Exception, match="append-only"):
            db.execute("DELETE FROM forward_return")


class TestForwardReturns:
    def test_return_is_measured_against_btc(self, db):
        """+100% in a market where BTC did +10% is not the same as +100% flat,
        and only the BTC-relative number can tell the two apart."""
        _seed_day(db)
        fr.write_entries(RUN_DATE)

        # One day later: AAA +20%, BTC +10%. Relative edge = +10%.
        _price(db, "2026-06-02", "AAA", 120.0)
        _price(db, "2026-06-02", "BTC", 66_000.0)
        db.commit()

        assert fr.backfill_returns("2026-06-03") >= 1
        row = db.query_one(
            "SELECT f.return_raw, f.return_vs_btc FROM forward_return f "
            "JOIN journal_entry e ON e.entry_id = f.entry_id "
            "WHERE e.base_asset = 'AAA' AND f.horizon = '1d'"
        )
        assert row["return_raw"] == pytest.approx(0.20, abs=1e-6)
        assert row["return_vs_btc"] == pytest.approx(0.10, abs=1e-6)

    def test_entry_is_the_signal_day_kline_close_not_the_run_time_price(self, db):
        """D-047. price_at_signal is the price at collection time; the next
        day's klines run replaces that row with the day's real close. Returns
        are measured from the close, which the backtest can also rebuild."""
        _seed_day(db)
        fr.write_entries(RUN_DATE)  # price_at_signal = 100
        _price(db, RUN_DATE, "AAA", 110.0)  # the signal day's kline close
        _price(db, "2026-06-02", "AAA", 121.0)
        _price(db, "2026-06-02", "BTC", 60_000.0)
        db.commit()

        fr.backfill_returns("2026-06-03")
        row = db.query_one(
            "SELECT e.price_at_signal, f.entry_price, f.price_source, f.return_raw "
            "FROM forward_return f JOIN journal_entry e ON e.entry_id = f.entry_id "
            "WHERE e.base_asset = 'AAA' AND f.horizon = '1d'"
        )
        assert row["price_at_signal"] == pytest.approx(100.0)
        assert row["entry_price"] == pytest.approx(110.0)
        assert row["price_source"] == "binance_klines"
        assert row["return_raw"] == pytest.approx(0.10, abs=1e-6)

    def test_a_coingecko_row_is_never_a_horizon_price(self, db):
        """CoinGecko keys price_daily on its own ticker, which is not always the
        Binance token of that name. A horizon priced from it could compare two
        different coins."""
        _seed_day(db)
        fr.write_entries(RUN_DATE)
        upsert(
            db,
            "price_daily",
            [
                {
                    "snapshot_date": "2026-06-02",
                    "base_asset": asset,
                    "close_usd": close,
                    "source": "coingecko",
                    "fetched_at_utc": "2026-06-02T03:10:00Z",
                }
                for asset, close in (("AAA", 120.0), ("BTC", 66_000.0))
            ],
        )
        db.commit()
        assert fr.backfill_returns("2026-06-03") == 0

    def test_excursions_record_the_path_not_just_the_endpoint(self, db):
        """A +20% return that first drew down 40% is not a winning trade."""
        _seed_day(db)
        fr.write_entries(RUN_DATE)
        # Entry at 100: dips to 60 intraday, closes the window at 120.
        _price(db, "2026-06-02", "AAA", 120.0, high=140.0, low=60.0)
        _price(db, "2026-06-02", "BTC", 60_000.0)
        db.commit()
        fr.backfill_returns("2026-06-03")

        row = db.query_one(
            "SELECT f.max_favourable, f.max_adverse FROM forward_return f "
            "JOIN journal_entry e ON e.entry_id = f.entry_id "
            "WHERE e.base_asset = 'AAA' AND f.horizon = '1d'"
        )
        assert row["max_favourable"] == pytest.approx(0.40, abs=1e-6)
        assert row["max_adverse"] == pytest.approx(-0.40, abs=1e-6)

    def test_missing_future_price_leaves_the_row_pending(self, db):
        """No price for the horizon date means no return -- not a zero. A
        fabricated zero could never be corrected in an append-only table."""
        _seed_day(db)
        fr.write_entries(RUN_DATE)
        assert fr.backfill_returns("2026-06-03") == 0
        assert db.scalar("SELECT COUNT(*) FROM forward_return") == 0

    def test_stale_price_outside_the_lookback_is_not_used(self, db):
        """A price from three weeks ago is not 'the price at the 30d horizon'."""
        _seed_day(db)
        fr.write_entries(RUN_DATE)
        # 30d horizon lands on 2026-07-01; the only later price is 40 days on.
        _price(db, "2026-07-11", "AAA", 200.0)
        _price(db, "2026-07-11", "BTC", 60_000.0)
        db.commit()
        fr.backfill_returns("2026-07-20")
        assert (
            db.scalar("SELECT COUNT(*) FROM forward_return WHERE horizon = '30d'") == 0
        )

    def test_a_delisted_asset_exits_at_its_last_close_not_pending_forever(self, db):
        """D-061. With no horizon price the row stayed pending forever, so the
        worst losers never reached the statistics."""
        _seed_day(db)
        fr.write_entries(RUN_DATE)
        _price(db, "2026-06-03", "AAA", 40.0)  # its last trade
        for day in ("2026-06-03", "2026-06-10", "2026-07-01"):
            _price(db, day, "BTC", 60_000.0)
        # On the horizon, Binance lists BTC and no longer lists AAA.
        upsert(
            db,
            "universe_snapshot",
            [
                {
                    "snapshot_date": "2026-07-01",
                    "exchange": "binance",
                    "symbol": "BTCUSDT",
                    "base_asset": "BTC",
                    "quote_asset": "USDT",
                    "status": "TRADING",
                    "fetched_at_utc": "2026-07-01T03:10:00Z",
                }
            ],
        )
        db.commit()

        fr.backfill_returns("2026-07-02")
        row = db.query_one(
            "SELECT f.return_raw, f.exit_reason FROM forward_return f "
            "JOIN journal_entry e ON e.entry_id = f.entry_id "
            "WHERE e.base_asset = 'AAA' AND f.horizon = '30d'"
        )
        assert row["exit_reason"] == "delisted"
        assert row["return_raw"] == pytest.approx(-0.60)

    def test_without_a_recent_universe_a_missing_price_stays_pending(self, db):
        """An outage of ours is not a delisting."""
        _seed_day(db)
        fr.write_entries(RUN_DATE)
        _price(db, "2026-06-03", "AAA", 40.0)
        _price(db, "2026-07-01", "BTC", 60_000.0)
        db.commit()
        fr.backfill_returns("2026-07-02")
        assert (
            db.scalar("SELECT COUNT(*) FROM forward_return WHERE horizon = '30d'") == 0
        )

    def test_backfill_is_idempotent(self, db):
        _seed_day(db)
        fr.write_entries(RUN_DATE)
        _price(db, "2026-06-02", "AAA", 120.0)
        _price(db, "2026-06-02", "BTC", 60_000.0)
        db.commit()

        first = fr.backfill_returns("2026-06-03")
        count = db.scalar("SELECT COUNT(*) FROM forward_return")
        assert fr.backfill_returns("2026-06-03") == 0
        assert db.scalar("SELECT COUNT(*) FROM forward_return") == count == first

    def test_horizon_that_has_not_elapsed_is_not_filled(self, db):
        _seed_day(db)
        fr.write_entries(RUN_DATE)
        for day in ("2026-06-02", "2026-06-08"):
            _price(db, day, "AAA", 120.0)
            _price(db, day, "BTC", 60_000.0)
        db.commit()

        fr.backfill_returns("2026-06-03")  # only 1d has elapsed
        horizons = {r["horizon"] for r in db.query("SELECT horizon FROM forward_return")}
        assert horizons == {"1d"}


def _seed_returns(
    db,
    values: list[tuple[str, bool, float, float]],
    score_version: str | None = "current",
    prefix: str = "e",
) -> None:
    """Write journal entries with pre-computed returns.

    Each tuple is (asset, is_control, return_raw, return_vs_btc). Statistics
    tests care about the arithmetic on top of the returns, not about how the
    returns were derived, so this bypasses the price path deliberately.

    D-076: entries are stamped with the CURRENT score_version by default,
    because compute_statistics now reads one cohort (the current method's)
    unless told otherwise. Before D-076 these rows had no version and every
    cohort was blended; pass score_version=None to write such a legacy row.
    """
    if score_version == "current":
        score_version = fr.current_score_version()
    entries = []
    returns = []
    for index, (asset, is_control, raw, vs_btc) in enumerate(values):
        entry_id = f"{prefix}{index}"
        entries.append(
            {
                "entry_id": entry_id,
                "run_date": RUN_DATE,
                "base_asset": asset,
                "rank": 0 if is_control else index + 1,
                "total_score": 50.0,
                "price_at_signal": 100.0,
                "btc_price_at_signal": 60_000.0,
                "is_control": 1 if is_control else 0,
                "layer1_values": None,
                "layer2_values": None,
                "layer3_values": None,
                "news_context": "[]",
                "events_context": "[]",
                "created_at_utc": f"{RUN_DATE}T00:00:00Z",
                "score_version": score_version,
            }
        )
        returns.append(
            {
                "entry_id": entry_id,
                "horizon": "30d",
                "price_at_horizon": 100.0 * (1 + raw),
                "return_raw": raw,
                "return_vs_btc": vs_btc,
                "max_favourable": abs(raw) + 0.05,
                "max_adverse": -abs(raw) - 0.05,
                "computed_at_utc": f"{RUN_DATE}T00:00:00Z",
            }
        )
    upsert(db, "journal_entry", entries)
    upsert(db, "forward_return", returns)
    db.commit()


class TestStatistics:
    def test_median_and_mean_are_both_reported(self, db):
        """The unlock study had mean -8.10% against median -16.26%. The gap IS
        the finding, and it is invisible unless both numbers are on the page."""
        _seed_returns(
            db,
            [
                ("A", False, 0.0, -0.10),
                ("B", False, 0.0, -0.10),
                ("C", False, 0.0, -0.10),
                ("D", False, 0.0, 1.00),  # one large winner drags the mean up
            ],
        )
        stats = fr.compute_statistics("30d")
        signal = stats["signal"]
        assert signal["vs_btc"]["median"] == pytest.approx(-0.10, abs=1e-4)
        assert signal["vs_btc"]["mean"] > 0
        # A lottery-ticket distribution: positive mean, negative median.
        assert signal["vs_btc"]["mean"] > 0 > signal["vs_btc"]["median"]

    def test_hit_rate_is_measured_against_btc_not_zero(self, db):
        """Three picks up in absolute terms but behind BTC is a 0% hit rate.
        Beating zero in a bull market is not evidence of anything."""
        _seed_returns(
            db,
            [
                ("A", False, 0.20, -0.05),
                ("B", False, 0.30, -0.02),
                ("C", False, 0.10, -0.15),
            ],
        )
        stats = fr.compute_statistics("30d")
        assert stats["signal"]["hit_rate_vs_btc"] == 0.0

    def test_excursions_are_summarised(self, db):
        _seed_returns(db, [("A", False, 0.20, 0.10), ("B", False, -0.30, -0.40)])
        stats = fr.compute_statistics("30d")
        assert stats["signal"]["max_adverse"]["min"] is not None
        assert stats["signal"]["max_favourable"]["max"] is not None

    def test_empty_group_reports_n_zero(self, db):
        _seed_returns(db, [("A", False, 0.1, 0.1)])
        stats = fr.compute_statistics("30d")
        assert stats["control"] == {"n": 0}

    def test_edge_requires_both_groups(self, db):
        _seed_returns(db, [("A", False, 0.1, 0.1)])
        edge = fr.compute_statistics("30d")["edge_vs_control"]
        assert edge["available"] is False
        assert "not enough" in edge["reason"]

    def test_edge_is_the_difference_of_medians(self, db):
        _seed_returns(
            db,
            [
                ("A", False, 0.0, 0.10),
                ("B", False, 0.0, 0.20),
                ("C", True, 0.0, 0.00),
                ("D", True, 0.0, 0.10),
            ],
        )
        edge = fr.compute_statistics("30d")["edge_vs_control"]
        assert edge["available"] is True
        assert edge["median_difference"] == pytest.approx(0.10, abs=1e-4)
        assert edge["signal_n"] == 2
        assert edge["control_n"] == 2


class TestJournalCommand:
    """D-062. A day the journal skips can never be written later."""

    def test_a_day_with_nothing_journalled_fails_the_command(self, db):
        from typer.testing import CliRunner

        from src.cli import app

        result = CliRunner().invoke(app, ["journal", "--date", "2026-01-01"])
        assert result.exit_code == 1

    def test_a_journalled_day_exits_clean(self, db):
        from typer.testing import CliRunner

        from src.cli import app

        _seed_day(db)
        result = CliRunner().invoke(app, ["journal", "--date", RUN_DATE])
        assert result.exit_code == 0, result.output


class TestReport:
    def test_report_refuses_to_show_partial_numbers(self, db):
        text = fr.render_report()
        assert "Not enough data yet" in text

    def test_report_warns_on_a_lottery_ticket_distribution(self, db):
        _seed_returns(
            db,
            [
                ("A", False, 0.0, -0.10),
                ("B", False, 0.0, -0.10),
                ("C", False, 0.0, -0.10),
                ("D", False, 0.0, 1.00),
                ("E", True, 0.0, 0.00),
                ("F", True, 0.0, 0.01),
            ],
        )
        text = fr.render_report()
        assert "lottery-ticket" in text
        assert "Edge over a random L1 survivor" in text
