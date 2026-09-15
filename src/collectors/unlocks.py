"""
# WHY: ------------------------------------------------------------------------
# The token unlock calendar -- the highest-value data in this entire system.
#
# Why a calendar beats a news feed: there is no latency race. An unlock date is
# published months ahead. Nobody can beat you to a fact that was already on a
# public schedule, which makes this the one genuinely tractable edge available
# to a retail participant with no information advantage.
#
# What the research says, and why the thresholds look the way they do:
#   * Across 16,000+ unlock events on 40 tokens, roughly 90% were followed by
#     negative price pressure.
#   * Team unlocks are worst (drawdowns toward -25%). Investor unlocks are
#     milder -- funds often use OTC or hedge in advance. ECOSYSTEM unlocks
#     average slightly POSITIVE, because those tokens fund grants and liquidity
#     rather than being sold. Hence recipient_type is scored, not just size.
#   * The decline typically BEGINS about 30 days before the date and stabilises
#     within about two weeks after. By release day the market has often already
#     repriced. That is why L1 looks 30 days AHEAD, not 7.
#   * An independent sample of 236 events: 1-month median -16.26%, mean -8.10%.
#     Note the gap between those two numbers -- the mean was pulled by a few
#     outliers while the median described the typical experience.
#
# THE INVERSE SIGNAL, which almost nobody screens for: a token whose major
# cliffs have ALL passed. Supply pressure structurally ends, and any demand
# improvement now meets a market with no scheduled seller. It is computed in
# events/features.py as `unlock_overhang_cleared` and measured in the journal.
#
# SOURCE -- R4, resolved 2026-09-13 (DECISIONS.md D-037). DefiLlama's public
# datasets host, defillama-datasets.llama.fi, serves the computed output of its
# emissions adapters: the data behind defillama.com/unlocks, with no key.
# Verified live rather than taken from a summary:
#
#   * api.llama.fi/emissions and /emission/{slug} return 402. The DATASETS host
#     returns 200 for /emissionsProtocolsList (372 protocols) and for
#     /emissions/{slug}.
#   * The emissions-adapters GitHub repository is no longer public (404), and
#     the one recent copy carries no license. Self-computing from it is not an
#     option; the datasets host is the source.
#   * metadata.unlockEvents lists, per date, cliffAllocations and
#     linearAllocations with an explicit `recipient` and `category`. The
#     category becomes recipient_type -- the field the research ranks as most
#     predictive -- through settings.unlocks.category_map.
#
# COVERAGE IS SKEWED, and not fixable from here: ~370 protocols means
# established tokens, and most microcaps have no adapter. The events layer
# keeps "no unlock schedule on file" (has_unlock_record = False) distinct from
# "no unlock ahead", so an unlisted token never reads as a clean schedule.
#
# POINT-IN-TIME. first_seen_utc is when WE first saw an event, never the unlock
# date, and a refresh never moves it forward. An event DefiLlama later resizes
# keeps its first_seen and takes the new size. A FUTURE event that a refresh no
# longer lists is marked retracted_utc rather than deleted, so a backtest dated
# before the retraction still sees what was known then.
#
# LINEAR VESTING is stored as unlock_linear at each rate change, sized as the
# lookahead window's worth of tokens at the new weekly rate, so the same
# 5%-of-circulating test applies to a stream as to a cliff. A stream that began
# before the window and is still running produces no FUTURE event: the check
# sees rate changes, not a stream's continuation (D-037).
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from src.collectors.base import BaseCollector
from src.db.connection import Database, get_db
from src.db.writes import deterministic_id, json_load, upsert
from src.timeutil import add_days, days_between, format_day, from_millis, utc_now_iso

UNLOCK_CALENDAR_PATH = Path("config/unlock_calendar.yaml")

#: `source` for events from DefiLlama's datasets host.
DATASETS_SOURCE = "defillama_datasets"

#: Recipient categories recognised by the scoring layer. Anything else is
#: stored as NULL rather than coerced into the nearest-looking bucket.
KNOWN_RECIPIENT_TYPES = frozenset({"team", "investor", "ecosystem", "community"})

#: Event types this collector may produce.
UNLOCK_EVENT_TYPES = frozenset({"unlock_cliff", "unlock_linear", "emissions_change", "burn"})

#: A unit conversion, not a threshold: DefiLlama states linear rates per week.
_DAYS_PER_WEEK = 7.0

#: SQLite's bound-parameter ceiling is 999 on older builds; stay well under it.
_IN_CHUNK = 400


def _normalise_recipient(value: Any) -> str | None:
    """Map a source's recipient label onto our vocabulary, or NULL.

    NULL is a correct answer. The recipient distinction is the most predictive
    field in the unlock literature and most free sources omit it -- guessing
    'team' because a label said 'core' would fabricate the very signal being
    measured.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in KNOWN_RECIPIENT_TYPES:
        return text
    aliases = {
        "insiders": "team",
        "core contributors": "team",
        "founders": "team",
        "private sale": "investor",
        "investors": "investor",
        "vc": "investor",
        "treasury": "ecosystem",
        "foundation": "ecosystem",
        "airdrop": "community",
        "public": "community",
    }
    return aliases.get(text)


def _num(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _day_of(seconds: Any) -> str | None:
    try:
        return format_day(from_millis(float(seconds) * 1000.0))
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _chunks(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _strip(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k != "_kind"}


#: DefiLlama chain names -> CoinGecko platform keys, where the two differ. Token
#: references arrive as '<defillama chain>:<address>' while asset_contract is
#: keyed by CoinGecko platform, so without these, tokens on arbitrum, avax and
#: bsc never resolved and their unlock schedules never attached (D-055).
DEFILLAMA_CHAIN_ALIASES: dict[str, str] = {
    "arbitrum": "arbitrum-one",
    "avax": "avalanche",
    "bsc": "binance-smart-chain",
    "polygon": "polygon-pos",
    "optimism": "optimistic-ethereum",
    "era": "zksync",
    "ton": "the-open-network",
    "near": "near-protocol",
}


def resolve_gecko_id(
    gecko_id: Any, token: Any, contract_index: dict[str, str]
) -> str | None:
    """A protocol's CoinGecko id, from its own field or from its token reference.

    `token` is 'coingecko:<id>' or '<platform>:<address>'. The address form is
    matched against every contract asset_contract knows, on every chain.
    """
    if gecko_id:
        return str(gecko_id)
    prefix, _, ref = str(token or "").partition(":")
    if not ref:
        return None
    if prefix == "coingecko":
        return ref
    address = ref.lower()
    platform = DEFILLAMA_CHAIN_ALIASES.get(prefix, prefix)
    return contract_index.get(f"{platform}:{address}") or contract_index.get(
        f"{prefix}:{address}"
    )


def plan_fetches(
    slugs: list[str],
    known: dict[str, dict[str, Any]],
    screened_gecko_ids: set[str],
    today: str,
    refresh_days: int,
    remap_days: int,
) -> list[str]:
    """Which protocols to download this run, most useful first.

    Due protocols that map to a screened asset come first, because they move
    L1 now. Then protocols never seen, which is the mapping bootstrap. Then the
    rest, when due for a re-check. Stalest first inside each group, so a run
    cut short by its budget still makes progress on the next.
    """
    due_matched: list[tuple[int, str]] = []
    unmapped: list[str] = []
    due_other: list[tuple[int, str]] = []
    for slug in slugs:
        record = known.get(slug)
        if record is None:
            unmapped.append(slug)
            continue
        age = days_between(str(record["last_fetched_utc"])[:10], today)
        if record.get("gecko_id") in screened_gecko_ids:
            if age >= refresh_days:
                due_matched.append((-age, slug))
        elif age >= remap_days:
            due_other.append((-age, slug))
    return (
        [slug for _, slug in sorted(due_matched)]
        + unmapped
        + [slug for _, slug in sorted(due_other)]
    )


def _event(
    slug: str,
    base_asset: str,
    event_type: str,
    day: str,
    recipient: str,
    category: str,
    magnitude: float,
    circulating_supply: float | None,
    category_map: dict[str, str | None],
    description: str,
    today: str,
    fetched_at: str,
) -> dict[str, Any]:
    return {
        "_kind": "event",
        # The amount is deliberately NOT part of the identity. DefiLlama
        # revises sizes; a revision must update the event, not add a second one
        # that double-counts the unlock.
        "event_id": deterministic_id(
            DATASETS_SOURCE, slug, base_asset, event_type, day, recipient, category
        ),
        "base_asset": base_asset,
        "event_type": event_type,
        "event_date_utc": day,
        "recipient_type": category_map.get(category) if category in category_map else None,
        "recipient_category": category or None,
        "recipient_label": recipient or None,
        "magnitude_tokens": magnitude,
        "magnitude_usd": None,
        "pct_of_circulating": magnitude / circulating_supply if circulating_supply else None,
        "description": description,
        "source": DATASETS_SOURCE,
        "source_ref": slug,
        "confidence": "expected" if day >= today else "confirmed",
        # We learned of it now. write() keeps the ORIGINAL value for an event
        # already on file, so this is only ever the first sighting.
        "first_seen_utc": fetched_at,
        "fetched_at_utc": fetched_at,
    }


def unlock_rows(
    slug: str,
    base_asset: str,
    unlock_events: list[dict[str, Any]],
    circulating_supply: float | None,
    category_map: dict[str, str | None],
    history_cutoff: str,
    today: str,
    fetched_at: str,
    window_days: float,
) -> list[dict[str, Any]]:
    """scheduled_event rows for one protocol's metadata.unlockEvents.

    Allocations sharing a date, recipient and category are summed: DefiLlama
    can list one recipient's cliff as several entries on the same day, and the
    event that matters is the total arriving on the market.
    """
    cliffs: dict[tuple[str, str, str], float] = {}
    linear: dict[tuple[str, str, str], dict[str, Any]] = {}
    for unlock in unlock_events or []:
        day = _day_of(unlock.get("timestamp"))
        if day is None or day < history_cutoff:
            continue
        for alloc in unlock.get("cliffAllocations") or []:
            key = (day, str(alloc.get("recipient") or ""), str(alloc.get("category") or ""))
            cliffs[key] = cliffs.get(key, 0.0) + _num(alloc.get("amount"))
        for alloc in unlock.get("linearAllocations") or []:
            key = (day, str(alloc.get("recipient") or ""), str(alloc.get("category") or ""))
            slot = linear.setdefault(key, {"rate": 0.0, "previous": 0.0, "end": None})
            slot["rate"] += _num(alloc.get("newRatePerWeek"))
            slot["previous"] += _num(alloc.get("previousRatePerWeek"))
            slot["end"] = alloc.get("endTimestamp") or slot["end"]

    rows: list[dict[str, Any]] = []
    for (day, recipient, category), amount in sorted(cliffs.items()):
        if amount <= 0:
            continue
        who = recipient or "an unnamed allocation"
        rows.append(
            _event(
                slug, base_asset, "unlock_cliff", day, recipient, category, amount,
                circulating_supply, category_map, f"Cliff of {amount:,.0f} tokens to {who}",
                today, fetched_at,
            )
        )
    for (day, recipient, category), slot in sorted(linear.items()):
        if slot["rate"] <= 0:
            continue  # a stream ending adds no supply
        who = recipient or "an unnamed allocation"
        magnitude = slot["rate"] * window_days / _DAYS_PER_WEEK
        end_day = _day_of(slot["end"]) if slot["end"] else None
        until = f" until {end_day}" if end_day else ""
        description = (
            f"Linear vesting to {who}: {slot['previous']:,.0f} -> {slot['rate']:,.0f} "
            f"tokens/week{until}; sized as {window_days:.0f} days at the new rate"
        )
        rows.append(
            _event(
                slug, base_asset, "unlock_linear", day, recipient, category, magnitude,
                circulating_supply, category_map, description, today, fetched_at,
            )
        )
    return rows


# -- writes ---------------------------------------------------------------------
def insert_new(db: Database, rows: list[dict[str, Any]]) -> int:
    """INSERT-only semantics: an event already on file is left exactly as it is.

    Used for the manual calendar, whose first_seen comes from the file.
    """
    if not rows:
        return 0
    ids = [r["event_id"] for r in rows]
    existing: set[str] = set()
    for chunk in _chunks(ids, _IN_CHUNK):
        existing.update(
            r["event_id"]
            for r in db.query(
                f"SELECT event_id FROM scheduled_event WHERE event_id IN ({','.join('?' for _ in chunk)})",
                list(chunk),
            )
        )
    fresh = [r for r in rows if r["event_id"] not in existing]
    return upsert(db, "scheduled_event", fresh) if fresh else 0


def _moved(old: Any, new: Any, tolerance: float) -> bool:
    if old is None or new is None:
        return (old is None) != (new is None)
    return abs(float(old) - float(new)) > tolerance * max(1.0, abs(float(old)))


def _differs(old: dict[str, Any], new: dict[str, Any]) -> bool:
    return (
        _moved(old["magnitude_tokens"], new["magnitude_tokens"], 1e-6)
        or _moved(old["pct_of_circulating"], new["pct_of_circulating"], 1e-4)
        or old["recipient_type"] != new["recipient_type"]
        or old["recipient_label"] != new["recipient_label"]
        or old["confidence"] != new["confidence"]
        or old["retracted_utc"] is not None
    )


#: What a revision can change, as load_known_events reads it back (D-055).
REVISED_FIELDS = (
    "magnitude_tokens",
    "pct_of_circulating",
    "recipient_type",
    "recipient_label",
    "confidence",
    "retracted_utc",
)


def _with_revision(old: dict[str, Any], recorded_utc: str) -> str | None:
    """revisions_json with the values about to be replaced appended, oldest first."""
    from src.db.writes import json_dump

    history = json_load(old.get("revisions_json"), []) or []
    history.append({"recorded_utc": recorded_utc, **{f: old.get(f) for f in REVISED_FIELDS}})
    return json_dump(history)


def merge_events(db: Database, events: list[dict[str, Any]]) -> int:
    """Insert new events; update changed ones WITHOUT touching first_seen_utc."""
    if not events:
        return 0
    existing: dict[str, dict[str, Any]] = {}
    ids = [e["event_id"] for e in events]
    for chunk in _chunks(ids, _IN_CHUNK):
        for row in db.query(
            "SELECT event_id, magnitude_tokens, pct_of_circulating, recipient_type, "
            "recipient_label, confidence, retracted_utc, revisions_json FROM scheduled_event "
            f"WHERE event_id IN ({','.join('?' for _ in chunk)})",
            list(chunk),
        ):
            existing[row["event_id"]] = row

    fresh = [e for e in events if e["event_id"] not in existing]
    changed = [e for e in events if e["event_id"] in existing and _differs(existing[e["event_id"]], e)]
    written = upsert(db, "scheduled_event", fresh) if fresh else 0
    if changed:
        # A reappearing event is un-retracted: it is again part of the schedule.
        # The values being replaced go into revisions_json first, so the event
        # can still be read as it stood before this revision (D-055).
        db.executemany(
            "UPDATE scheduled_event SET magnitude_tokens = ?, pct_of_circulating = ?, "
            "recipient_type = ?, recipient_category = ?, recipient_label = ?, description = ?, "
            "confidence = ?, fetched_at_utc = ?, retracted_utc = NULL, revisions_json = ? "
            "WHERE event_id = ?",
            [
                [
                    e["magnitude_tokens"], e["pct_of_circulating"], e["recipient_type"],
                    e["recipient_category"], e["recipient_label"], e["description"],
                    e["confidence"], e["fetched_at_utc"],
                    _with_revision(existing[e["event_id"]], e["fetched_at_utc"]),
                    e["event_id"],
                ]
                for e in changed
            ],
        )
        written += len(changed)
    return written


def retract_missing(db: Database, refreshed: list[dict[str, Any]]) -> int:
    """Mark FUTURE events a refreshed protocol no longer lists. Never deletes."""
    total = 0
    for marker in refreshed:
        live = set(marker["event_ids"])
        rows = db.query(
            "SELECT event_id FROM scheduled_event WHERE source = ? AND source_ref = ? "
            "AND base_asset = ? AND event_date_utc >= ? AND retracted_utc IS NULL",
            (DATASETS_SOURCE, marker["slug"], marker["base_asset"], marker["today"]),
        )
        gone = [[marker["fetched_at"], r["event_id"]] for r in rows if r["event_id"] not in live]
        if gone:
            db.executemany("UPDATE scheduled_event SET retracted_utc = ? WHERE event_id = ?", gone)
            total += len(gone)
    return total


class UnlockCollector(BaseCollector):
    """Builds `scheduled_event` from DefiLlama's datasets host and the manual calendar."""

    name = "unlocks"
    rate_limit_key = "defillama_datasets"
    tier = "C"

    async def fetch(self, as_of: datetime) -> dict[str, Any]:
        settings = self.config.settings
        cfg = settings.unlocks
        fetched_at = utc_now_iso()
        today = fetched_at[:10]
        universe = self._universe()
        known = self._known_protocols()

        protocols: list[dict[str, Any]] = []
        try:
            async with self.client(settings.endpoints["defillama_datasets"]) as client:
                slugs = await self.request_json(client, "GET", "/emissionsProtocolsList")
                if not isinstance(slugs, list) or not slugs:
                    raise RuntimeError("emissionsProtocolsList returned no protocols")
                plan = plan_fetches(
                    [str(s) for s in slugs], known, set(universe), today,
                    cfg.refresh_days, cfg.remap_days,
                )
                failures = 0
                for slug in plan[: cfg.max_protocol_fetches_per_run]:
                    try:
                        body = await self.request_json(client, "GET", f"/emissions/{slug}")
                    except Exception as exc:  # noqa: BLE001 - one protocol, not the run
                        failures += 1
                        self.log.warning("emission_protocol_unavailable", slug=slug, error=str(exc)[:150])
                        continue
                    body = body if isinstance(body, dict) else {}
                    meta = body.get("metadata") or {}
                    # Keep only what is used. A protocol is up to 2.5 MB, almost
                    # all of it a daily supply series this collector never reads.
                    protocols.append(
                        {
                            "slug": slug,
                            "gecko_id": body.get("gecko_id"),
                            "name": body.get("name"),
                            "token": meta.get("token"),
                            "unlock_events": meta.get("unlockEvents") or [],
                        }
                    )
                if failures:
                    self.warn("emission_protocols_unavailable", count=failures)
                if len(plan) > cfg.max_protocol_fetches_per_run:
                    self.log.info(
                        "emission_fetches_deferred",
                        remaining=len(plan) - cfg.max_protocol_fetches_per_run,
                    )
        except Exception as exc:  # noqa: BLE001 - the source, not the run
            self.warn(
                "unlock_source_unavailable",
                source=DATASETS_SOURCE,
                error=str(exc)[:150],
                effect="manual calendar only this run; events on file are kept, none retracted",
            )

        return {
            "manual": self._load_manual_calendar(),
            "protocols": protocols,
            "universe": universe,
            "contract_index": self._contract_index(),
            "fetched_at": fetched_at,
        }

    def transform(self, raw: dict[str, Any], as_of: datetime) -> list[dict[str, Any]]:
        cfg = self.config.settings.unlocks
        fetched_at = raw["fetched_at"]
        today = fetched_at[:10]
        cutoff = add_days(today, -cfg.history_days)
        window = float(self.config.thresholds.layer1.unlock_lookahead_days)
        universe = raw.get("universe") or {}
        index = raw.get("contract_index") or {}

        rows: list[dict[str, Any]] = self._from_manual(raw.get("manual") or [], fetched_at)
        claimed: dict[str, str] = {}
        unmapped_categories: set[str] = set()

        # Largest schedule first: when two DefiLlama protocols resolve to one
        # asset, the fuller one wins and the other is logged, not merged --
        # merging would count the same unlock twice.
        ordered = sorted(
            raw.get("protocols") or [],
            key=lambda p: (-len(p.get("unlock_events") or []), p["slug"]),
        )
        for protocol in ordered:
            gecko = resolve_gecko_id(protocol.get("gecko_id"), protocol.get("token"), index)
            rows.append(
                {
                    "_kind": "protocol",
                    "slug": protocol["slug"],
                    "gecko_id": gecko,
                    "name": protocol.get("name"),
                    "token": protocol.get("token"),
                    "unlock_event_count": len(protocol.get("unlock_events") or []),
                    "last_fetched_utc": fetched_at,
                    "fetched_at_utc": fetched_at,
                }
            )
            target = universe.get(gecko) if gecko else None
            if not target:
                continue
            if gecko in claimed:
                self.log.warning(
                    "emission_protocol_duplicate_asset",
                    gecko_id=gecko,
                    kept=claimed[gecko],
                    skipped=protocol["slug"],
                )
                continue
            claimed[gecko] = protocol["slug"]

            events = unlock_rows(
                protocol["slug"],
                target["base_asset"],
                protocol.get("unlock_events") or [],
                target.get("circulating_supply"),
                cfg.category_map,
                cutoff,
                today,
                fetched_at,
                window,
            )
            unmapped_categories.update(
                e["recipient_category"]
                for e in events
                if e["recipient_category"] and e["recipient_category"] not in cfg.category_map
            )
            rows.extend(events)
            rows.append(
                {
                    "_kind": "refreshed",
                    "slug": protocol["slug"],
                    "base_asset": target["base_asset"],
                    "event_ids": [e["event_id"] for e in events],
                    "today": today,
                    "fetched_at": fetched_at,
                }
            )

        if unmapped_categories:
            self.log.warning(
                "unlock_categories_unmapped",
                categories=sorted(unmapped_categories),
                effect="stored with recipient_type NULL, which L1 treats as possibly team",
            )
        self.log.info(
            "unlock_protocols_processed",
            fetched=len(raw.get("protocols") or []),
            matched_to_screened_assets=len(claimed),
            events=sum(1 for r in rows if r["_kind"] == "event"),
        )
        return rows

    def write(self, rows: list[dict[str, Any]]) -> int:
        manual = [_strip(r) for r in rows if r.get("_kind") == "manual"]
        events = [_strip(r) for r in rows if r.get("_kind") == "event"]
        protocols = [_strip(r) for r in rows if r.get("_kind") == "protocol"]
        refreshed = [r for r in rows if r.get("_kind") == "refreshed"]
        with get_db() as db:
            written = insert_new(db, manual) + merge_events(db, events)
            retracted = retract_missing(db, refreshed)
            if protocols:
                upsert(db, "emission_protocol", protocols)
        if retracted:
            self.log.info("unlock_events_retracted", count=retracted)
        return written

    # -- the manual calendar --------------------------------------------------
    def _load_manual_calendar(self) -> list[dict[str, Any]]:
        path = self.config.repo_root / UNLOCK_CALENDAR_PATH
        if not path.exists():
            return []
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return list(data.get("events") or [])

    def _from_manual(self, events: list[dict], fetched_at: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for event in events:
            asset = str(event.get("base_asset", "")).upper()
            event_type = str(event.get("event_type", "unlock_cliff"))
            date = event.get("event_date_utc")
            if not asset or not date:
                self.warn("manual_unlock_row_incomplete", row=str(event)[:120])
                continue
            if event_type not in UNLOCK_EVENT_TYPES:
                self.warn("unknown_event_type", event_type=event_type, asset=asset)
                continue

            # first_seen is what makes point-in-time backtesting honest. It is
            # taken from the file, never from today, because a row added last
            # month must not appear to have been knowable today only.
            first_seen = str(event.get("first_seen") or "")[:10]
            if not first_seen:
                self.warn(
                    "manual_unlock_missing_first_seen",
                    asset=asset,
                    effect="using today, which understates how long this was known",
                )
                first_seen = fetched_at[:10]

            rows.append(
                {
                    "_kind": "manual",
                    "event_id": deterministic_id("manual", asset, event_type, str(date)[:10]),
                    "base_asset": asset,
                    "event_type": event_type,
                    "event_date_utc": str(date)[:10],
                    "recipient_type": _normalise_recipient(event.get("recipient_type")),
                    "magnitude_tokens": event.get("magnitude_tokens"),
                    "magnitude_usd": event.get("magnitude_usd"),
                    "pct_of_circulating": event.get("pct_of_circulating"),
                    "description": event.get("description"),
                    "source": str(event.get("source") or "manual_calendar"),
                    "confidence": str(event.get("confidence") or "expected"),
                    "first_seen_utc": f"{first_seen}T00:00:00Z",
                    "fetched_at_utc": fetched_at,
                }
            )
        return rows

    # -- database reads -----------------------------------------------------------
    def _universe(self) -> dict[str, dict[str, Any]]:
        """Screened assets keyed by coingecko_id, with circulating supply."""
        with get_db() as db:
            universe_date = db.scalar(
                "SELECT MAX(snapshot_date) FROM universe_snapshot WHERE exchange = 'binance'"
            )
            market_date = db.scalar("SELECT MAX(snapshot_date) FROM market_snapshot")
            if not universe_date or not market_date:
                return {}
            rows = db.query(
                "SELECT DISTINCT m.coingecko_id, m.base_asset, m.circulating_supply "
                "FROM market_snapshot m JOIN universe_snapshot u ON u.base_asset = m.base_asset "
                "AND u.snapshot_date = ? AND u.exchange = 'binance' AND u.status = 'TRADING' "
                "WHERE m.snapshot_date = ? AND m.coingecko_id IS NOT NULL ORDER BY m.base_asset",
                (universe_date, market_date),
            )
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            out.setdefault(
                row["coingecko_id"],
                {"base_asset": row["base_asset"], "circulating_supply": row["circulating_supply"]},
            )
        return out

    def _known_protocols(self) -> dict[str, dict[str, Any]]:
        try:
            with get_db() as db:
                rows = db.query("SELECT slug, gecko_id, last_fetched_utc FROM emission_protocol")
        except Exception as exc:  # noqa: BLE001 - table absent before init-db
            self.log.warning("emission_protocol_unreadable", error=str(exc)[:150])
            return {}
        return {r["slug"]: r for r in rows}

    def _contract_index(self) -> dict[str, str]:
        """'<platform>:<address>' -> coingecko_id, across every chain on file."""
        try:
            with get_db() as db:
                rows = db.query("SELECT coingecko_id, platforms_json FROM asset_contract")
        except Exception as exc:  # noqa: BLE001 - table absent before init-db
            self.log.warning("asset_contract_unreadable", error=str(exc)[:150])
            return {}
        index: dict[str, str] = {}
        for row in rows:
            for platform, address in (json_load(row["platforms_json"], {}) or {}).items():
                index[f"{platform}:{str(address).lower()}"] = row["coingecko_id"]
        return index


__all__ = [
    "DATASETS_SOURCE",
    "DEFILLAMA_CHAIN_ALIASES",
    "REVISED_FIELDS",
    "KNOWN_RECIPIENT_TYPES",
    "UNLOCK_EVENT_TYPES",
    "UnlockCollector",
    "insert_new",
    "merge_events",
    "plan_fetches",
    "resolve_gecko_id",
    "retract_missing",
    "unlock_rows",
]
