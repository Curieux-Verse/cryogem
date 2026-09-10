"""
# WHY: ------------------------------------------------------------------------
# One-way copy: local SQLite -> Turso.
#
# Needed exactly once, when the project stops being a laptop script and becomes
# an unattended job. Without it, the first Actions run starts against an empty
# Turso and everything collected so far is stranded on one machine.
#
# Most of what it moves is replaceable -- klines backfill from Binance, a
# universe snapshot, today's derivatives. Two things are NOT:
#
#   * journal_entry, which is append-only and IS the record of what the system
#     believed on a given day. It cannot be recomputed, because recomputing it
#     later would use revised data.
#   * layer1_result / layer2_result, for the same reason: the point-in-time
#     ranking is the thing the backtest replays, and re-screening an old date
#     with today's tables is a backtest of hindsight.
#
# Direction is deliberately one-way. A two-way sync would need conflict rules,
# and the honest rule here is that after the cutover Turso is the only writer.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import os
from typing import Any

from src.db import connection as connection_module
from src.db.connection import Database
from src.logging_setup import get_logger

log = get_logger("ops.migrate")

#: Copy order matters: forward_return has a foreign key onto journal_entry, so
#: the parent must land first or the insert is rejected.
TABLE_ORDER = (
    "universe_snapshot",
    "market_snapshot",
    "spot_snapshot",
    "price_daily",
    "derivatives_snapshot",
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
    "collector_run",
    "trigger_lag",
    "backtest_run",
    "table_stats",
)

#: Rows per round trip. Turso is HTTP, so one statement per row would be one
#: request per row -- 357k requests for the price history alone.
BATCH = 500


def _open_local() -> Database:
    """The local SQLite file, regardless of what TURSO_* says."""
    return connection_module._open_sqlite()


def _open_turso() -> Database:
    url = os.getenv("TURSO_DATABASE_URL")
    token = os.getenv("TURSO_AUTH_TOKEN")
    if not url:
        raise RuntimeError(
            "TURSO_DATABASE_URL is not set. Put it in .env or export it before "
            "running this, or there is nothing to migrate to."
        )
    return connection_module._open_libsql(url, token)


def copy_table(source: Database, target: Database, table: str, dry_run: bool) -> dict[str, Any]:
    rows = source.query(f"SELECT * FROM {table}")
    if not rows:
        return {"table": table, "rows": 0, "copied": 0, "skipped": "empty"}

    columns = list(rows[0])
    column_sql = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    # INSERT OR IGNORE, not upsert: re-running the migration must never
    # overwrite something Turso has already written for itself. After the
    # cutover, Turso is authoritative.
    sql = f"INSERT OR IGNORE INTO {table} ({column_sql}) VALUES ({placeholders})"

    if dry_run:
        return {"table": table, "rows": len(rows), "copied": 0, "skipped": "dry-run"}

    copied = 0
    for start in range(0, len(rows), BATCH):
        chunk = rows[start : start + BATCH]
        target.executemany(sql, [[row[c] for c in columns] for row in chunk])
        copied += len(chunk)
        if len(rows) > BATCH:
            log.info("migrate_progress", table=table, copied=copied, of=len(rows))
    return {"table": table, "rows": len(rows), "copied": copied}


def migrate(dry_run: bool = False) -> list[dict[str, Any]]:
    source = _open_local()
    # A dry run must work BEFORE the Turso account exists -- its whole purpose
    # is to show what is at stake so the decision can be made informed.
    target = None if dry_run and not os.getenv("TURSO_DATABASE_URL") else _open_turso()
    if target is None:
        try:
            results = [copy_table(source, source, t, dry_run=True) for t in TABLE_ORDER]
            for result in results:
                log.info("migrate_table", **result)
            return results
        finally:
            source.close()
    try:
        present = set(target.table_names())
        missing = [t for t in TABLE_ORDER if t not in present]
        if missing:
            raise RuntimeError(
                f"Turso is missing {len(missing)} table(s): {', '.join(missing)}.\n"
                "Run `python -m src.cli init-db` with TURSO_DATABASE_URL set first."
            )

        results = []
        for table in TABLE_ORDER:
            result = copy_table(source, target, table, dry_run)
            results.append(result)
            log.info("migrate_table", **result)
        if not dry_run:
            target.commit()
        return results
    finally:
        source.close()
        target.close()


def verify(target: Database | None = None) -> dict[str, Any]:
    """Confirm Turso enforces the append-only triggers.

    This is research question R9: the schema relies on SQLite triggers to make
    journal_entry append-only, and the guarantee is worth nothing if libSQL
    silently ignored them. Checked by ATTEMPTING a delete and requiring it to
    fail.
    """
    owned = target is None
    db = target or _open_turso()
    try:
        counts = {t: db.scalar(f"SELECT COUNT(*) FROM {t}") for t in TABLE_ORDER}
        enforced = False
        detail = "journal_entry is empty, so the trigger could not be exercised"
        if counts.get("journal_entry"):
            try:
                db.execute("DELETE FROM journal_entry WHERE 1=0 OR rowid IN (SELECT rowid FROM journal_entry LIMIT 1)")
                detail = "DELETE SUCCEEDED -- the append-only trigger is NOT enforced on Turso"
            except Exception as exc:  # noqa: BLE001 - the failure IS the pass
                enforced = "append-only" in str(exc).lower()
                detail = str(exc)[:200]
        return {"counts": counts, "append_only_enforced": enforced, "detail": detail}
    finally:
        if owned:
            db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Copy the local SQLite database into Turso.")
    parser.add_argument("--dry-run", action="store_true", help="Count rows, write nothing.")
    parser.add_argument("--verify", action="store_true", help="Only check Turso's state.")
    args = parser.parse_args()

    if args.verify:
        state = verify()
        for table, count in state["counts"].items():
            print(f"  {table:24s} {count:>8,}")
        print()
        print("append-only enforced:", state["append_only_enforced"])
        print("detail:", state["detail"])
        return

    results = migrate(dry_run=args.dry_run)
    total = sum(r["copied"] for r in results)
    print(f"{'DRY RUN — nothing written' if args.dry_run else 'Migration complete'}")
    for r in results:
        note = f"  ({r['skipped']})" if r.get("skipped") else ""
        print(f"  {r['table']:24s} {r['rows']:>8,} rows{note}")
    print(f"\n{total:,} rows copied")


if __name__ == "__main__":
    main()
