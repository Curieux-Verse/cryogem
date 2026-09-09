"""
# WHY: ------------------------------------------------------------------------
# Measures the gap between when a job was SUPPOSED to fire and when it actually
# started, from an independent source (cron-job.org passes its intended time as
# a workflow input).
#
# This is the early-warning signal for the failure that hides best: a stray
# `on: schedule:` trigger somewhere in the repo. A scheduled workflow still
# appears to run, so nothing looks broken -- but 5-45 minute drift returns, and
# so does the 60-day inactivity auto-disable.
#
# Expected reading with external triggering is SECONDS (runner provisioning).
# A p95 in tens of minutes means GitHub's scheduler is involved again.
# The Health page plots the distribution.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import uuid

from src.db.connection import get_db
from src.db.writes import upsert
from src.logging_setup import get_logger
from src.timeutil import parse_instant, utc_now, utc_now_iso

log = get_logger("ops.record_lag")


def record(workflow: str, scheduled_for: str | None) -> float | None:
    """Store one trigger-lag observation. Returns the lag in seconds."""
    actual = utc_now()
    lag_seconds: float | None = None
    scheduled_iso: str | None = None

    if scheduled_for:
        try:
            scheduled = parse_instant(scheduled_for)
            scheduled_iso = scheduled_for
            lag_seconds = (actual - scheduled).total_seconds()
        except (ValueError, TypeError):
            # A malformed input must not fail the collection job that just
            # succeeded. Record the run without a lag figure.
            log.warning("unparseable_scheduled_time", value=scheduled_for)

    with get_db() as db:
        upsert(
            db,
            "trigger_lag",
            [
                {
                    "run_id": uuid.uuid4().hex,
                    "workflow": workflow,
                    "scheduled_at_utc": scheduled_iso,
                    "actual_start_utc": utc_now_iso(),
                    "lag_seconds": lag_seconds,
                    "fetched_at_utc": utc_now_iso(),
                }
            ],
        )
    log.info("trigger_lag_recorded", workflow=workflow, lag_seconds=lag_seconds)
    return lag_seconds


def main() -> None:
    parser = argparse.ArgumentParser(description="Record trigger lag for a workflow run.")
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--scheduled", default="", help="Intended fire time, UTC ISO.")
    args = parser.parse_args()
    lag = record(args.workflow, args.scheduled or None)
    print(f"{args.workflow}: lag {lag if lag is not None else 'unknown'}s")


if __name__ == "__main__":
    main()
