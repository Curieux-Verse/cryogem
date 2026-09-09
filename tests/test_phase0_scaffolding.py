"""Phase 0 acceptance criteria, as executable tests."""

from __future__ import annotations

import pytest

from src.config import get_config
from src.db.writes import deterministic_id, json_dump, json_load, upsert

EXPECTED_TABLES = {
    "universe_snapshot",
    "derivatives_snapshot",
    "market_snapshot",
    "depth_snapshot",
    "fundamentals_snapshot",
    "holder_snapshot",
    "supply_metrics",
    "liquidation_snapshot",
    "scheduled_event",
    "attention_snapshot",
    "market_regime",
    "news_item",
    "layer1_result",
    "layer2_result",
    "layer3_result",
    "journal_entry",
    "forward_return",
    "price_daily",
    "collector_run",
    "table_stats",
    "trigger_lag",
}


class TestConfig:
    def test_every_layer1_threshold_present(self):
        l1 = get_config().thresholds.layer1
        for field in (
            "oi_to_mcap_fail",
            "perp_to_spot_vol_fail",
            "min_mcap_usd",
            "min_contract_age_days",
            "top10_holder_share_fail",
            "min_circulating_ratio",
            "unlock_lookahead_days",
            "unlock_pct_circulating_fail",
            "mcap_to_liquidation_ratio_fail",
        ):
            assert getattr(l1, field) is not None

    def test_layer2_weights_all_positive(self):
        for name, value in get_config().thresholds.layer2_weights.as_dict().items():
            assert value > 0, f"weight {name} must be positive"

    def test_weights_sum_to_110_and_are_normalised_later(self):
        # Spec deliberately over-allocates so a missing block renormalises.
        assert sum(get_config().thresholds.layer2_weights.as_dict().values()) == 110

    def test_unmapped_ticker_is_unclassified_not_guessed(self):
        assert get_config().sectors.sector_of("NOTAREALTICKER") == "unclassified"

    def test_system_runs_with_no_secrets(self):
        # Every secret is optional. This must not raise.
        assert isinstance(get_config().secrets.missing(), list)


class TestSchema:
    def test_all_tables_created(self, db):
        assert EXPECTED_TABLES.issubset(set(db.table_names()))

    def test_apply_schema_is_idempotent(self, db):
        before = set(db.table_names())
        db.apply_schema()
        db.apply_schema()
        assert set(db.table_names()) == before

    def test_journal_entry_rejects_delete(self, db):
        _insert_journal_row(db)
        with pytest.raises(Exception, match="append-only"):
            db.execute("DELETE FROM journal_entry WHERE entry_id='e1'")

    def test_journal_entry_rejects_update(self, db):
        _insert_journal_row(db)
        with pytest.raises(Exception, match="append-only"):
            db.execute("UPDATE journal_entry SET rank=99 WHERE entry_id='e1'")


class TestWrites:
    def test_upsert_is_idempotent(self, db):
        row = {
            "snapshot_date": "2026-09-09",
            "exchange": "binance",
            "symbol": "BTCUSDT",
            "base_asset": "BTC",
            "quote_asset": "USDT",
            "fetched_at_utc": "2026-09-09T03:12:00Z",
        }
        upsert(db, "universe_snapshot", [row])
        upsert(db, "universe_snapshot", [row])
        assert db.scalar("SELECT COUNT(*) FROM universe_snapshot") == 1

    def test_upsert_updates_on_conflict(self, db):
        row = {
            "snapshot_date": "2026-09-09",
            "exchange": "binance",
            "symbol": "BTCUSDT",
            "base_asset": "BTC",
            "quote_asset": "USDT",
            "status": "TRADING",
            "fetched_at_utc": "2026-09-09T03:12:00Z",
        }
        upsert(db, "universe_snapshot", [row])
        upsert(db, "universe_snapshot", [{**row, "status": "SETTLING"}])
        assert db.scalar("SELECT status FROM universe_snapshot") == "SETTLING"

    def test_upsert_refuses_rows_missing_primary_key(self, db):
        with pytest.raises(ValueError, match="primary-key"):
            upsert(db, "universe_snapshot", [{"base_asset": "BTC"}])

    def test_unknown_table_raises(self, db):
        with pytest.raises(KeyError):
            upsert(db, "not_a_table", [{"a": 1}])

    def test_deterministic_id_is_stable_and_distinct(self):
        assert deterministic_id("UNLOCK", "ARB", "2026-10-01") == deterministic_id(
            "UNLOCK", "ARB", "2026-10-01"
        )
        assert deterministic_id("UNLOCK", "ARB", "2026-10-01") != deterministic_id(
            "UNLOCK", "OP", "2026-10-01"
        )

    def test_json_none_stays_none(self):
        # A None must not become the string "null" -- that would read back as
        # a truthy value and silently pass a data-availability check.
        assert json_dump(None) is None
        assert json_load(None) is None
        assert json_load("") is None


def _insert_journal_row(db) -> None:
    db.execute(
        "INSERT INTO journal_entry "
        "(entry_id, run_date, base_asset, rank, total_score, price_at_signal, "
        " btc_price_at_signal, created_at_utc) "
        "VALUES ('e1','2026-09-09','TEST',1,50.0,1.0,60000.0,'2026-09-09T03:12:00Z')"
    )
