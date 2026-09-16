"""
D-064 and D-065: publish refuses an empty day; ops tooling that is safe and complete.
"""

from __future__ import annotations

import gzip
import re
import sqlite3

import pytest

from src.db.writes import upsert
from src.ops import backup, doctor, migrate
from src.report import publish

SCHEMA_TABLES = set(
    re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", backup.SCHEMA_PATH.read_text(encoding="utf-8"))
)


class TestPublishRefusesAnEmptyDay:
    def test_a_date_with_no_screen_is_refused(self, db):
        with pytest.raises(publish.NothingToPublish, match="no screen results"):
            publish.publish_all("2026-01-01")

    def test_no_screen_on_file_at_all_is_refused(self, db):
        with pytest.raises(publish.NothingToPublish):
            publish.publish_all()

    def test_an_empty_screen_never_empties_the_asset_directory(self, tmp_path):
        (tmp_path / "AAA.json").write_text("{}", encoding="utf-8")
        publish._prune_asset_dir(tmp_path, set())
        assert (tmp_path / "AAA.json").exists()


class TestBackup:
    def test_every_schema_table_is_backed_up(self):
        """backtest_run, the holdout audit log, was missing."""
        assert SCHEMA_TABLES == set(backup.TABLE_ORDER)

    def test_the_dump_restores_into_an_empty_database(self, db, tmp_path):
        """The documented `gunzip | sqlite3 restored.db` failed: no schema."""
        upsert(
            db,
            "price_daily",
            [
                {
                    "snapshot_date": "2026-09-01",
                    "base_asset": "BTC",
                    "close_usd": 60_000.0,
                    "source": "binance_klines",
                    "fetched_at_utc": "2026-09-02T03:10:00Z",
                }
            ],
        )
        db.commit()
        out = tmp_path / "backup.sql.gz"
        assert backup.dump(out) >= 1

        restored = sqlite3.connect(tmp_path / "restored.db")
        with gzip.open(out, "rt", encoding="utf-8") as fh:
            restored.executescript(fh.read())
        assert restored.execute("SELECT close_usd FROM price_daily").fetchone() == (60_000.0,)
        restored.close()


class TestMigrateVerify:
    def test_the_probe_never_touches_the_journal(self, db):
        upsert(
            db,
            "journal_entry",
            [
                {
                    "entry_id": "real",
                    "run_date": "2026-09-01",
                    "base_asset": "BTC",
                    "rank": 1,
                    "total_score": 50.0,
                    "price_at_signal": 1.0,
                    "btc_price_at_signal": 1.0,
                    "is_control": 0,
                    "created_at_utc": "2026-09-01T06:10:00Z",
                }
            ],
        )
        db.commit()
        state = migrate.verify(target=db)
        assert state["append_only_enforced"] is True, state["detail"]
        assert db.scalar("SELECT COUNT(*) FROM journal_entry") == 1
        tables = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert migrate._PROBE not in tables


class TestFailureStreaks:
    def test_streaks_are_per_collector_and_consecutive(self):
        runs = [
            {"collector_name": "coingecko", "status": "failed"},
            {"collector_name": "coingecko", "status": "failed"},
            {"collector_name": "coingecko", "status": "success"},
            {"collector_name": "coingecko", "status": "failed"},
            {"collector_name": "klines", "status": "partial"},
            {"collector_name": "klines", "status": "failed"},
        ]
        assert doctor.failure_streaks(runs) == {"coingecko": 2, "klines": 0}
