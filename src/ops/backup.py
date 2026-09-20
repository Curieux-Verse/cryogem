"""
# WHY: ------------------------------------------------------------------------
# Weekly database dump, published as a GitHub RELEASE ASSET.
#
# Not only as an Actions artifact: artifacts expire, releases do not. A backup
# that silently ages out is not a backup, and this dataset is the one thing in
# the project that cannot be rebuilt in a weekend.
#
# Dumps as gzipped SQL rather than a binary file so the output is diffable,
# portable between sqlite3 and libSQL, and readable without this codebase.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import gzip
from pathlib import Path
from typing import Any

from src.db.connection import get_db
from src.logging_setup import get_logger
from src.timeutil import utc_now_iso

log = get_logger("ops.backup")

#: High-frequency tables are dumped last so a truncated file still contains the
#: irreplaceable low-volume records (the journal above all).
TABLE_ORDER = (
    "journal_entry",
    "forward_return",
    "pulse_journal",
    "pulse_forward_return",
    "scheduled_event",
    "emission_protocol",
    "asset_contract",
    "address_label",
    "layer1_result",
    "layer2_result",
    "layer3_result",
    "universe_snapshot",
    "market_snapshot",
    "fundamentals_snapshot",
    "holder_snapshot",
    "supply_metrics",
    "supply_history",
    "supply_backfill",
    "attention_snapshot",
    "market_regime",
    "spot_snapshot",
    "price_daily",
    "liquidation_snapshot",
    "news_item",
    "collector_run",
    "trigger_lag",
    "table_stats",
    "depth_snapshot",
    "derivatives_snapshot",
    "pulse_result",
    "pulse_alert",
    "bar_1h",
    "oi_1h",
    "series_cursor",
    # The holdout audit log (D-018). Missing, a restore silently reset the count
    # of times the holdout had been looked at (D-065).
    "backtest_run",
)

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "db" / "schema.sql"


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return repr(value)
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def dump(out_path: Path) -> int:
    """Write a gzipped SQL dump. Returns the number of rows written."""
    total = 0
    with get_db() as db, gzip.open(out_path, "wt", encoding="utf-8") as fh:
        fh.write(f"-- gem-screener backup {utc_now_iso()}\n")
        fh.write("-- Restore: gunzip -c backup.sql.gz | sqlite3 restored.db\n")
        # The schema first. The dump held INSERTs only, so the documented restore
        # into an empty file failed with "no such table" (D-065).
        fh.write(SCHEMA_PATH.read_text(encoding="utf-8"))
        fh.write("\nPRAGMA foreign_keys=OFF;\nBEGIN;\n")

        present = set(db.table_names())
        for table in TABLE_ORDER:
            if table not in present:
                continue
            rows = db.query(f"SELECT * FROM {table}")
            if not rows:
                continue
            columns = list(rows[0])
            column_sql = ", ".join(columns)
            for row in rows:
                values = ", ".join(_sql_literal(row[c]) for c in columns)
                fh.write(f"INSERT OR IGNORE INTO {table} ({column_sql}) VALUES ({values});\n")
            total += len(rows)
            log.info("table_dumped", table=table, rows=len(rows))

        fh.write("COMMIT;\n")
    log.info("backup_complete", path=str(out_path), rows=total)
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="Dump the database to gzipped SQL.")
    parser.add_argument("--out", default="backup.sql.gz")
    args = parser.parse_args()
    path = Path(args.out)
    rows = dump(path)
    print(f"wrote {path} ({path.stat().st_size:,} bytes, {rows:,} rows)")


if __name__ == "__main__":
    main()
