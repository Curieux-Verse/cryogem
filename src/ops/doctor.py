"""
# WHY: ------------------------------------------------------------------------
# The freshness assertion, and the answer to "is anything actually broken?".
#
# This runs BEFORE the screen job in CI. If it fails, the screen does not run.
#
# The failure it exists to prevent is specific and is the worst output this
# system can produce: a confident dashboard built on three-day-old prices.
# Nothing about such a page looks wrong. It renders, it ranks, it has a date on
# it, and every number is stale. Silence is not evidence that collection is
# working, so freshness is asserted rather than assumed.
#
# Also checks the things that fail quietly months in: an unwritable database,
# a collector that has been failing repeatedly, and gaps in the daily series.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

from typing import Any

from src.config import get_config
from src.db.connection import get_db
from src.logging_setup import get_logger
from src.timeutil import age_hours, today_utc, utc_now_iso

log = get_logger("ops.doctor")

#: Tables whose staleness would corrupt a screen, how they are timestamped, and
#: which rows count. Hyperliquid writes derivatives and universe rows every
#: hour, so an unfiltered MAX() read as fresh while the Binance feed the screen
#: actually uses had been dead for days (D-050). The screen's own freshness
#: assertion reads this same tuple, so the two can never disagree.
FRESHNESS_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("derivatives_snapshot", "fetched_at_utc", "exchange = 'binance'"),
    ("market_snapshot", "fetched_at_utc", "1 = 1"),
    ("universe_snapshot", "fetched_at_utc", "exchange = 'binance'"),
)


def run_diagnostics(max_age_hours: float | None = None) -> dict[str, Any]:
    """Check config, database and data freshness.

    Returns a dict with `lines` (human-readable) and `problems` (list). The CLI
    exits non-zero when `problems` is non-empty, which fails the CI job, which
    means healthchecks.io never gets its ping. That chain is the point.
    """
    cfg = get_config()
    limit = max_age_hours or cfg.thresholds.collectors.max_data_age_hours
    lines: list[str] = []
    problems: list[str] = []

    lines.append(f"gem-screener doctor  ({utc_now_iso()})")
    lines.append("")

    # -- configuration -------------------------------------------------------
    missing = cfg.secrets.missing()
    lines.append(f"config      : OK  ({len(cfg.thresholds.layer2_weights.as_dict())} L2 blocks)")
    if missing:
        lines.append(f"secrets     : {len(missing)} unset (optional): {', '.join(sorted(missing))}")

    # -- database ------------------------------------------------------------
    try:
        with get_db() as db:
            tables = db.table_names()
            lines.append(f"database    : OK  backend={db.backend}, {len(tables)} tables")

            # -- freshness ---------------------------------------------------
            lines.append("")
            lines.append(f"freshness   : limit {limit:g}h")
            for table, column, where in FRESHNESS_TARGETS:
                newest = db.scalar(f"SELECT MAX({column}) FROM {table} WHERE {where}")
                if not newest:
                    problems.append(f"{table} is empty")
                    lines.append(f"  {table:<22} EMPTY            <- no data collected yet")
                    continue
                hours = age_hours(newest)
                flag = "OK " if hours <= limit else "STALE"
                if hours > limit:
                    problems.append(
                        f"{table} is {hours:.1f}h old, over the {limit:g}h limit"
                    )
                lines.append(f"  {table:<22} {hours:>6.1f}h  {flag}  (newest {newest})")

            # -- collector health --------------------------------------------
            lines.append("")
            lines.append("collectors  : last run per collector")
            recent = db.query(
                "SELECT collector_name, status, rows_written, started_at_utc "
                "FROM collector_run r WHERE started_at_utc = ("
                "  SELECT MAX(started_at_utc) FROM collector_run "
                "  WHERE collector_name = r.collector_name) "
                "ORDER BY collector_name"
            )
            if not recent:
                lines.append("  (none have run yet)")
            for row in recent:
                mark = "OK " if row["status"] == "success" else row["status"]
                lines.append(
                    f"  {row['collector_name']:<24} {mark:<8} "
                    f"{row['rows_written'] or 0:>6} rows  {row['started_at_utc']}"
                )

            # Repeated failure is different from a single flake.
            failing = db.query(
                "SELECT collector_name, COUNT(*) n FROM ("
                "  SELECT collector_name, status FROM collector_run "
                "  ORDER BY started_at_utc DESC LIMIT 30"
                ") WHERE status='failed' GROUP BY collector_name HAVING n >= ?",
                (cfg.thresholds.collectors.consecutive_failures_before_alert,),
            )
            for row in failing:
                problems.append(
                    f"{row['collector_name']} failed {row['n']} times in the last 30 runs"
                )

            # -- screening output --------------------------------------------
            last_screen = db.scalar("SELECT MAX(run_date) FROM layer1_result")
            lines.append("")
            lines.append(f"last screen : {last_screen or 'never'}  (today is {today_utc()})")

    except Exception as exc:  # noqa: BLE001
        problems.append(f"database unreachable: {exc}")
        lines.append(f"database    : FAILED  {exc}")

    lines.append("")
    if problems:
        lines.append(f"PROBLEMS ({len(problems)}):")
        lines.extend(f"  - {p}" for p in problems)
    else:
        lines.append("no problems found")

    return {"lines": lines, "problems": problems, "ok": not problems}


__all__ = ["FRESHNESS_TARGETS", "run_diagnostics"]
