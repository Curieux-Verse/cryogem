"""
# WHY: ------------------------------------------------------------------------
# Idempotent writes. A collector must be safe to run twice.
#
# This matters more than it sounds. Triggering is external (cron-job.org), and a
# delayed trigger can land on top of a still-running job. Actions can also retry.
# If a second run of the same collector duplicated rows, every cross-sectional
# ranking would silently double-count assets. So every write here is an UPSERT
# keyed on the table's natural primary key: re-running overwrites the row it
# wrote before and changes nothing else.
#
# Two tables are exceptions and use INSERT-OR-IGNORE instead of UPSERT:
#   journal_entry / forward_return -- append-only, guarded by DB triggers. An
#   UPDATE there raises, so a re-run must skip rather than overwrite.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from src.db.connection import Database
from src.timeutil import utc_now_iso

# Natural primary keys, mirroring schema.sql. Kept here so a write cannot
# silently target the wrong conflict target if the schema changes.
PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "universe_snapshot": ("snapshot_date", "exchange", "symbol"),
    "derivatives_snapshot": ("ts_utc", "exchange", "symbol"),
    "market_snapshot": ("snapshot_date", "base_asset"),
    "spot_snapshot": ("snapshot_date", "exchange", "symbol"),
    "depth_snapshot": ("ts_utc", "exchange", "symbol", "market_type"),
    "fundamentals_snapshot": ("snapshot_date", "base_asset"),
    "holder_snapshot": ("snapshot_date", "base_asset"),
    "supply_metrics": ("snapshot_date", "base_asset"),
    "liquidation_snapshot": ("snapshot_date", "exchange", "symbol"),
    "scheduled_event": ("event_id",),
    "attention_snapshot": ("snapshot_date", "base_asset", "source"),
    "market_regime": ("snapshot_date",),
    "news_item": ("news_id",),
    "layer1_result": ("run_date", "base_asset"),
    "layer2_result": ("run_date", "base_asset"),
    "layer3_result": ("run_date", "base_asset"),
    "price_daily": ("snapshot_date", "base_asset"),
    "collector_run": ("run_id",),
    "table_stats": ("table_name",),
    "backtest_run": ("run_id",),
    "trigger_lag": ("run_id",),
    "journal_entry": ("entry_id",),
    "forward_return": ("entry_id", "horizon"),
}

# Append-only tables: never UPDATE, only INSERT OR IGNORE.
APPEND_ONLY = frozenset({"journal_entry", "forward_return"})


def json_dump(value: Any) -> str | None:
    """Serialise a dict/list column. None stays None -- never the string 'null'."""
    if value is None:
        return None
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)


def json_load(text: str | None, default: Any = None) -> Any:
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default


def deterministic_id(*parts: Any) -> str:
    """Stable id from its inputs.

    Used for event_id and entry_id. Deterministic means re-running a collector
    produces the SAME id for the same logical event, so INSERT OR IGNORE
    de-duplicates instead of accumulating near-identical rows.
    """
    joined = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


def _columns_of(rows: Sequence[dict[str, Any]]) -> list[str]:
    """Union of keys across rows, in first-seen order.

    A union (not the first row's keys) so a partially-populated row does not
    silently drop a column that a later row provides.
    """
    seen: dict[str, None] = {}
    for row in rows:
        for key in row:
            seen.setdefault(key, None)
    return list(seen)


def upsert(db: Database, table: str, rows: Sequence[dict[str, Any]]) -> int:
    """Insert rows, updating on primary-key conflict. Returns rows submitted."""
    if not rows:
        return 0
    if table not in PRIMARY_KEYS:
        raise KeyError(f"unknown table {table!r}: add its primary key to PRIMARY_KEYS")

    cols = _columns_of(rows)
    keys = PRIMARY_KEYS[table]
    missing_keys = [k for k in keys if k not in cols]
    if missing_keys:
        raise ValueError(
            f"cannot upsert into {table}: rows are missing primary-key column(s) "
            f"{missing_keys}. Refusing to write a row that cannot be de-duplicated."
        )

    placeholders = ", ".join("?" for _ in cols)
    col_list = ", ".join(cols)

    if table in APPEND_ONLY:
        sql = f"INSERT OR IGNORE INTO {table} ({col_list}) VALUES ({placeholders})"
    else:
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in keys)
        conflict = ", ".join(keys)
        sql = (
            f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
            f"ON CONFLICT({conflict}) DO UPDATE SET {updates}"
            if updates
            else f"INSERT OR IGNORE INTO {table} ({col_list}) VALUES ({placeholders})"
        )

    payload = [[row.get(c) for c in cols] for row in rows]
    written = db.executemany(sql, payload)
    _bump_stats(db, table, written)
    return written


def _bump_stats(db: Database, table: str, written: int) -> None:
    """Maintain table_stats.

    Turso meters ROW READS. `SELECT COUNT(*)` over derivatives_snapshot to answer
    "how big is this table" would read every row and burn the free allowance
    (spec 15.1.5). So counts are maintained incrementally on write instead.

    The count is approximate by construction: an upsert that overwrites an
    existing row still increments. It is a growth indicator for the Health page,
    not an exact census, and it is labelled that way in the UI.
    """
    if written <= 0:
        return
    db.execute(
        "INSERT INTO table_stats (table_name, row_count, last_write_utc) VALUES (?, ?, ?) "
        "ON CONFLICT(table_name) DO UPDATE SET "
        "row_count = table_stats.row_count + excluded.row_count, "
        "last_write_utc = excluded.last_write_utc",
        (table, written, utc_now_iso()),
    )


def record_collector_run(
    db: Database,
    run_id: str,
    collector_name: str,
    started_at_utc: str,
    ended_at_utc: str | None,
    status: str,
    rows_written: int,
    error_message: str | None = None,
) -> None:
    """Write the ops row for one collector run.

    Written on BOTH the success and the failure path. A collector that fails
    silently and leaves no row is indistinguishable from one that never ran,
    and that ambiguity is what lets two weeks of missing data go unnoticed.
    """
    if status not in {"success", "partial", "failed"}:
        raise ValueError(f"invalid collector status {status!r}")
    upsert(
        db,
        "collector_run",
        [
            {
                "run_id": run_id,
                "collector_name": collector_name,
                "started_at_utc": started_at_utc,
                "ended_at_utc": ended_at_utc,
                "status": status,
                "rows_written": rows_written,
                "error_message": (error_message or "")[:2000] or None,
            }
        ],
    )


def latest_snapshot_date(db: Database, table: str, column: str = "snapshot_date") -> str | None:
    """Newest date present in a daily table. Indexed, so cheap."""
    return db.scalar(f"SELECT MAX({column}) AS d FROM {table}")


def latest_instant(db: Database, table: str, column: str = "fetched_at_utc") -> str | None:
    return db.scalar(f"SELECT MAX({column}) AS t FROM {table}")


__all__ = [
    "APPEND_ONLY",
    "PRIMARY_KEYS",
    "deterministic_id",
    "json_dump",
    "json_load",
    "latest_instant",
    "latest_snapshot_date",
    "record_collector_run",
    "upsert",
]
