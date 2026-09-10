"""
# WHY: ------------------------------------------------------------------------
# Measures the gap between when a job was SUPPOSED to fire and when it actually
# started, from an independent source (cron-job.org states its intended time).
#
# This is the early-warning signal for the failure that hides best: a stray
# `on: schedule:` trigger somewhere in the repo. A scheduled workflow still
# appears to run, so nothing looks broken -- but 5-45 minute drift returns, and
# so does the 60-day inactivity auto-disable.
#
# ON THE INPUT FORMAT: cron-job.org sends a FIXED request body. It has no
# templating, so it cannot put "now" into the JSON. A hardcoded instant would
# be correct once and then drift by a day per day, so the reading would be
# nonsense inside a week -- and a metric that is quietly nonsense is worse than
# one that is absent, because it is still plotted. The input is therefore the
# intended TIME OF DAY, which for a fixed cron slot genuinely is constant:
#
#     "03:10"  a daily job   -> the most recent 03:10 UTC
#     ":25"    an hourly job -> the most recent :25 past the hour
#
# A full UTC ISO instant is still accepted, for manual dispatch and for tests.
#
# Expected reading with external triggering is SECONDS (runner provisioning).
# A p95 in tens of minutes means GitHub's scheduler is involved again.
# The Health page plots the distribution.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import re
import uuid
from datetime import datetime, timedelta

from src.db.connection import get_db
from src.db.writes import upsert
from src.logging_setup import get_logger
from src.timeutil import format_instant, parse_instant, utc_now

log = get_logger("ops.record_lag")

_TIME_OF_DAY = re.compile(r"^(\d{1,2}):(\d{2})$")
_MINUTE_PAST = re.compile(r"^:(\d{2})$")

# A trigger can land a hair BEFORE its nominal slot, because cron-job.org's
# clock and GitHub's clock are not the same clock. Rolling the candidate back
# a whole period over two seconds of skew would record a 24-hour lag and set
# off exactly the alarm this metric exists to raise, so absorb small negatives
# as the near-zero lag they actually are.
_SKEW_TOLERANCE = timedelta(seconds=120)


def _most_recent(candidate: datetime, now: datetime, period: timedelta) -> datetime:
    """Walk a repeating slot back to the last occurrence at or before `now`."""
    while candidate - now > _SKEW_TOLERANCE:
        candidate -= period
    return candidate


def resolve_scheduled(text: str, now: datetime) -> datetime:
    """Intended fire time as an instant. See the module docstring on formats.

    Raises ValueError on anything unrecognisable, which the caller records as
    a run with no lag figure rather than a failed job.
    """
    value = text.strip()

    minute_past = _MINUTE_PAST.match(value)
    if minute_past:
        candidate = now.replace(
            minute=int(minute_past.group(1)), second=0, microsecond=0
        )
        return _most_recent(candidate, now, timedelta(hours=1))

    time_of_day = _TIME_OF_DAY.match(value)
    if time_of_day:
        candidate = now.replace(
            hour=int(time_of_day.group(1)),
            minute=int(time_of_day.group(2)),
            second=0,
            microsecond=0,
        )
        return _most_recent(candidate, now, timedelta(days=1))

    return parse_instant(value)


def record(workflow: str, scheduled_for: str | None) -> float | None:
    """Store one trigger-lag observation. Returns the lag in seconds."""
    actual = utc_now()
    actual_iso = format_instant(actual)
    lag_seconds: float | None = None
    scheduled_iso: str | None = None

    if scheduled_for:
        try:
            scheduled = resolve_scheduled(scheduled_for, actual)
            # Normalise to an instant before storing. "03:10" is not one, and
            # the column is read back by the Health page as an instant.
            scheduled_iso = format_instant(scheduled)
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
                    # The SAME instant the lag was measured against, not a
                    # second call to the clock a few milliseconds later.
                    "actual_start_utc": actual_iso,
                    "lag_seconds": lag_seconds,
                    "fetched_at_utc": actual_iso,
                }
            ],
        )
    log.info("trigger_lag_recorded", workflow=workflow, lag_seconds=lag_seconds)
    return lag_seconds


def main() -> None:
    parser = argparse.ArgumentParser(description="Record trigger lag for a workflow run.")
    parser.add_argument("--workflow", required=True)
    parser.add_argument(
        "--scheduled",
        default="",
        help='Intended fire time: "HH:MM", ":MM", or a full UTC ISO instant.',
    )
    args = parser.parse_args()
    lag = record(args.workflow, args.scheduled or None)
    print(f"{args.workflow}: lag {lag if lag is not None else 'unknown'}s")


if __name__ == "__main__":
    main()
