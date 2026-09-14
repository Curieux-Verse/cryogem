"""
# WHY: ------------------------------------------------------------------------
# Turns a calendar of dated events into the handful of numbers the screener
# actually asks for.
#
# Every function here obeys ONE rule that matters more than the arithmetic:
#
#     Only events whose first_seen_utc <= as_of may be used.
#
# That is what separates an honest backtest from a fictional one. An unlock
# schedule published in June cannot inform a May decision. Because the filter
# lives here rather than at each call site, no future caller can forget it --
# and a backtest that silently used tomorrow's knowledge would produce
# confident, wrong numbers that look exactly like a working strategy.
#
# The feature worth the most attention is `unlock_overhang_cleared`: the
# inverse signal almost nobody screens for. When every major cliff has passed,
# supply pressure structurally ends, and any demand improvement now meets a
# market with no scheduled seller.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.config import get_config
from src.db.connection import Database
from src.timeutil import days_between, today_utc

#: Event types that represent supply arriving on the market.
SUPPLY_EVENTS = ("unlock_cliff", "unlock_linear")

#: Event types that are structurally positive for the holder.
POSITIVE_EVENTS = ("burn", "mainnet", "upgrade")


@dataclass
class EventFeatures:
    """Every event-derived input to L1 and L2 for one asset, on one date."""

    base_asset: str
    as_of: str
    days_to_next_major_unlock: int | None
    days_since_last_major_unlock: int | None
    next_unlock_pct_circulating: float | None
    next_unlock_recipient_type: str | None
    unlock_overhang_cleared: bool
    emissions_trajectory: str | None
    event_density_30d: int
    positive_catalyst_30d: bool
    monitoring_tag_active: bool
    #: False when we hold no events at all for this asset -- which is a
    #: statement about our data, not about the asset.
    has_event_data: bool
    #: False when we hold no SUPPLY event for this asset. Narrower than
    #: has_event_data and the one that matters for unlock scoring: an asset
    #: with a mainnet date on file but no vesting schedule has event data yet
    #: nothing to say about its next cliff. Consumers must not read
    #: days_to_next_major_unlock is None as "no unlock ahead" unless this
    #: is True -- see the events block in screening/layer2_score.py.
    has_unlock_record: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "days_to_next_major_unlock": self.days_to_next_major_unlock,
            "days_since_last_major_unlock": self.days_since_last_major_unlock,
            "next_unlock_pct_circulating": self.next_unlock_pct_circulating,
            "next_unlock_recipient_type": self.next_unlock_recipient_type,
            "unlock_overhang_cleared": self.unlock_overhang_cleared,
            "emissions_trajectory": self.emissions_trajectory,
            "event_density_30d": self.event_density_30d,
            "positive_catalyst_30d": self.positive_catalyst_30d,
            "monitoring_tag_active": self.monitoring_tag_active,
            "has_event_data": self.has_event_data,
            "has_unlock_record": self.has_unlock_record,
        }


def load_known_events(
    db: Database, as_of: str | None = None, assets: list[str] | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Every event KNOWABLE as of `as_of`, grouped by asset.

    The `first_seen_utc <= as_of` filter is applied here, once, so that no
    caller can accidentally read the future.
    """
    run_date = as_of or today_utc()
    cutoff = f"{run_date}T23:59:59Z"

    sql = (
        "SELECT base_asset, event_type, event_date_utc, recipient_type, "
        "       magnitude_tokens, pct_of_circulating, confidence, first_seen_utc "
        "FROM scheduled_event WHERE first_seen_utc <= ? "
        # A retracted event was knowable until it was retracted, and not after.
        "AND (retracted_utc IS NULL OR retracted_utc > ?)"
    )
    params: list[Any] = [cutoff, cutoff]
    if assets:
        sql += f" AND base_asset IN ({','.join('?' for _ in assets)})"
        params.extend(assets)

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in db.query(sql, params):
        grouped.setdefault(row["base_asset"], []).append(row)
    return grouped


def _is_major_unlock(event: dict[str, Any], min_pct: float) -> bool:
    """A cliff big enough to matter. Unknown size counts as major.

    Treating an unsized unlock as major is deliberate: the alternative is to
    assume a lack of data means a small event, which is the assumption that
    lets a 20% team cliff through because nobody filled in a field.
    """
    if event["event_type"] not in SUPPLY_EVENTS:
        return False
    pct = event.get("pct_of_circulating")
    return True if pct is None else float(pct) >= min_pct


def compute_features(
    base_asset: str,
    events: list[dict[str, Any]],
    as_of: str | None = None,
    emissions_trajectory: str | None = None,
    monitoring_tag_active: bool = False,
) -> EventFeatures:
    """Derive every event feature for one asset.

    `events` must already be filtered to what was knowable -- use
    load_known_events(), which does that filtering.
    """
    cfg = get_config()
    run_date = as_of or today_utc()
    min_pct = cfg.thresholds.layer1.unlock_pct_circulating_fail
    lookahead = cfg.thresholds.layer1.unlock_lookahead_days

    future: list[tuple[int, dict]] = []
    past: list[tuple[int, dict]] = []
    for event in events:
        delta = days_between(run_date, event["event_date_utc"])
        (future if delta >= 0 else past).append((delta, event))

    future.sort(key=lambda pair: pair[0])
    past.sort(key=lambda pair: pair[0], reverse=True)

    next_major = next(((d, e) for d, e in future if _is_major_unlock(e, min_pct)), None)
    last_major = next(((d, e) for d, e in past if _is_major_unlock(e, min_pct)), None)

    # The inverse signal: every known major cliff is behind us. Requires that
    # we actually HAVE unlock history -- an asset we know nothing about is not
    # an asset whose overhang has cleared.
    has_any_unlock_record = any(e["event_type"] in SUPPLY_EVENTS for e in events)
    overhang_cleared = has_any_unlock_record and next_major is None

    density = sum(1 for delta, _ in future if 0 <= delta <= 30)
    positive_catalyst = any(
        e["event_type"] in POSITIVE_EVENTS and 0 <= d <= lookahead for d, e in future
    )

    return EventFeatures(
        base_asset=base_asset,
        as_of=run_date,
        days_to_next_major_unlock=next_major[0] if next_major else None,
        days_since_last_major_unlock=abs(last_major[0]) if last_major else None,
        next_unlock_pct_circulating=(
            next_major[1].get("pct_of_circulating") if next_major else None
        ),
        next_unlock_recipient_type=(
            next_major[1].get("recipient_type") if next_major else None
        ),
        unlock_overhang_cleared=overhang_cleared,
        emissions_trajectory=emissions_trajectory,
        event_density_30d=density,
        positive_catalyst_30d=positive_catalyst,
        monitoring_tag_active=monitoring_tag_active,
        has_event_data=bool(events),
        has_unlock_record=has_any_unlock_record,
    )


def compute_all(
    db: Database, assets: list[str], as_of: str | None = None
) -> dict[str, EventFeatures]:
    """Features for many assets in ONE query.

    Batched deliberately: Turso meters row reads, and a per-asset query across
    500 survivors is 500 round trips for data that fits in one indexed scan.
    """
    run_date = as_of or today_utc()
    grouped = load_known_events(db, run_date, assets)
    trajectories = _load_emissions_trajectories(db, run_date, assets)
    monitoring = _load_monitoring_tags(db, run_date, assets)

    return {
        asset: compute_features(
            base_asset=asset,
            events=grouped.get(asset, []),
            as_of=run_date,
            emissions_trajectory=trajectories.get(asset),
            monitoring_tag_active=asset in monitoring,
        )
        for asset in assets
    }


def _load_emissions_trajectories(
    db: Database, as_of: str, assets: list[str]
) -> dict[str, str]:
    if not assets:
        return {}
    rows = db.query(
        "SELECT base_asset, emissions_trajectory FROM supply_metrics "
        "WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM supply_metrics "
        "                       WHERE snapshot_date <= ?) "
        f"AND base_asset IN ({','.join('?' for _ in assets)})",
        [as_of, *assets],
    )
    return {r["base_asset"]: r["emissions_trajectory"] for r in rows if r["emissions_trajectory"]}


def _load_monitoring_tags(db: Database, as_of: str, assets: list[str]) -> set[str]:
    """Assets currently carrying an exchange monitoring tag.

    The tag is a soft exchange warning for elevated-volatility assets and is
    often a structural precursor to delisting. An 'add' with no later 'remove'
    means the tag is still on.
    """
    if not assets:
        return set()
    rows = db.query(
        "SELECT base_asset, event_type, event_date_utc FROM scheduled_event "
        "WHERE event_type IN ('monitoring_tag_add','monitoring_tag_remove') "
        "AND first_seen_utc <= ? "
        f"AND base_asset IN ({','.join('?' for _ in assets)}) "
        "ORDER BY event_date_utc",
        [f"{as_of}T23:59:59Z", *assets],
    )
    state: dict[str, bool] = {}
    for row in rows:
        state[row["base_asset"]] = row["event_type"] == "monitoring_tag_add"
    return {asset for asset, tagged in state.items() if tagged}


__all__ = [
    "POSITIVE_EVENTS",
    "SUPPLY_EVENTS",
    "EventFeatures",
    "compute_all",
    "compute_features",
    "load_known_events",
]
