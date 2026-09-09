"""
# WHY: ------------------------------------------------------------------------
# One place that decides what a timestamp looks like.
#
# Spec 0.1.6: "Timestamp everything at ingest." Every row carries fetched_at_utc,
# and point-in-time backtesting depends on those strings being consistent,
# sortable, and unambiguously UTC. SQLite has no date type -- it stores text --
# so string format IS the schema here. Two modules formatting differently would
# corrupt every range query silently.
#
# Format decisions, fixed here and nowhere else:
#   instants : 'YYYY-MM-DDTHH:MM:SSZ'  (second precision, explicit Z, sortable)
#   days     : 'YYYY-MM-DD'
#
# Also note spec 10 (failure register): always write the ACTUAL run time to
# fetched_at_utc, never a nominal cron slot time, or every downstream time
# calculation is quietly wrong.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

ISO_INSTANT = "%Y-%m-%dT%H:%M:%SZ"
ISO_DAY = "%Y-%m-%d"


def utc_now() -> datetime:
    """Timezone-aware 'now'. Never use datetime.utcnow(): it returns naive."""
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return format_instant(utc_now())


def today_utc() -> str:
    return utc_now().strftime(ISO_DAY)


def format_instant(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime(ISO_INSTANT)


def format_day(value: datetime | date) -> str:
    if isinstance(value, datetime):
        return format_instant(value)[:10]
    return value.strftime(ISO_DAY)


def parse_instant(text: str) -> datetime:
    """Parse our own instant format, and tolerate common ISO variants."""
    cleaned = text.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_day(text: str) -> date:
    return datetime.strptime(text.strip()[:10], ISO_DAY).date()


def from_millis(ms: int | float | str) -> datetime:
    """Exchange APIs return epoch milliseconds almost everywhere."""
    return datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc)


def millis_from(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def days_between(earlier: str | date | datetime, later: str | date | datetime) -> int:
    """Whole days from `earlier` to `later`. Negative when `later` precedes it."""
    a = _as_date(earlier)
    b = _as_date(later)
    return (b - a).days


def add_days(day: str, n: int) -> str:
    return (parse_day(day) + timedelta(days=n)).strftime(ISO_DAY)


def age_hours(timestamp: str, now: datetime | None = None) -> float:
    """Hours since `timestamp`. Used by the data-freshness assertion."""
    ref = now or utc_now()
    return (ref - parse_instant(timestamp)).total_seconds() / 3600.0


def horizon_to_days(horizon: str) -> int:
    """'1d' -> 1, '7d' -> 7, '30d' -> 30, '90d' -> 90."""
    text = horizon.strip().lower()
    if not text.endswith("d") or not text[:-1].isdigit():
        raise ValueError(f"unsupported horizon {horizon!r}: expected forms like '30d'")
    return int(text[:-1])


def _as_date(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return parse_day(value)


__all__ = [
    "ISO_DAY",
    "ISO_INSTANT",
    "add_days",
    "age_hours",
    "days_between",
    "format_day",
    "format_instant",
    "from_millis",
    "horizon_to_days",
    "millis_from",
    "parse_day",
    "parse_instant",
    "today_utc",
    "utc_now",
    "utc_now_iso",
]
