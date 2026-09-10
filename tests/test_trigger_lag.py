"""
# WHY: ------------------------------------------------------------------------
# The trigger-lag metric is the only guard that notices a stray `on: schedule:`
# trigger AFTER it has been merged -- CI catches it in the repo, this catches it
# in production. So the resolver has to be right for the two formats
# cron-job.org can actually send, and it has to fail safely rather than report a
# plausible wrong number.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.ops.record_lag import resolve_scheduled


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def test_daily_slot_resolves_to_today_when_already_past():
    now = at("2026-09-10T03:10:14")
    assert resolve_scheduled("03:10", now) == at("2026-09-10T03:10:00")


def test_daily_slot_rolls_back_a_day_when_still_ahead():
    # A job dispatched at 02:00 for an 03:10 slot is yesterday's run, late.
    now = at("2026-09-10T02:00:00")
    assert resolve_scheduled("03:10", now) == at("2026-09-09T03:10:00")


def test_hourly_slot_resolves_within_the_current_hour():
    now = at("2026-09-10T14:25:09")
    assert resolve_scheduled(":25", now) == at("2026-09-10T14:25:00")


def test_hourly_slot_rolls_back_an_hour_not_a_day():
    now = at("2026-09-10T14:05:00")
    assert resolve_scheduled(":25", now) == at("2026-09-10T13:25:00")


def test_small_clock_skew_reads_as_near_zero_not_a_full_period():
    # cron-job.org's clock and GitHub's clock are not the same clock. Two
    # seconds early must NOT become a 24-hour lag and fire the alarm this
    # metric exists to raise.
    now = at("2026-09-10T03:09:58")
    resolved = resolve_scheduled("03:10", now)
    lag = (now - resolved).total_seconds()
    assert -120 <= lag <= 0
    assert resolved == at("2026-09-10T03:10:00")


def test_skew_beyond_tolerance_does_roll_back():
    now = at("2026-09-10T03:00:00")
    assert resolve_scheduled("03:10", now) == at("2026-09-09T03:10:00")


def test_full_iso_instant_still_accepted():
    now = at("2026-09-10T03:10:30")
    assert resolve_scheduled("2026-09-10T03:10:00Z", now) == at("2026-09-10T03:10:00")


@pytest.mark.parametrize("bad", ["", "soon", "99:99", "3:1", ":9", "03:10:00"])
def test_unparseable_input_raises_rather_than_guessing(bad):
    # record() turns this into a run with no lag figure. A guess would be
    # plotted on the Health page as though it were measured.
    with pytest.raises(ValueError):
        resolve_scheduled(bad, at("2026-09-10T03:10:30"))


def test_lag_is_seconds_for_a_punctual_external_trigger():
    now = at("2026-09-10T03:10:12")
    lag = (now - resolve_scheduled("03:10", now)).total_seconds()
    assert 0 <= lag < 60, "punctual external triggering should read in seconds"


def test_a_scheduled_workflow_would_read_in_minutes():
    # The symptom described in the runbook's failure register: GitHub's own
    # scheduler drifts 5-45 minutes, so the p95 climbs out of the seconds band.
    now = at("2026-09-10T03:10:00") + timedelta(minutes=37)
    lag = (now - resolve_scheduled("03:10", now)).total_seconds()
    assert lag == pytest.approx(37 * 60)
