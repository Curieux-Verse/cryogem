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
# improvement now meets a market with no scheduled seller. That is plausibly
# the strongest single positive feature in the system. It is computed in
# events/features.py as `unlock_overhang_cleared` and measured in the journal.
#
# SOURCE CHAIN (see D-008): every free unlock API is now paywalled, so sources
# are tried in order and the collector degrades gracefully rather than failing.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from src.collectors.base import BaseCollector
from src.db.connection import get_db
from src.db.writes import deterministic_id, upsert
from src.timeutil import format_day, parse_day, utc_now_iso

UNLOCK_CALENDAR_PATH = Path("config/unlock_calendar.yaml")

#: Recipient categories recognised by the scoring layer. Anything else is
#: stored as NULL rather than coerced into the nearest-looking bucket.
KNOWN_RECIPIENT_TYPES = frozenset({"team", "investor", "ecosystem", "community"})

#: Event types this collector may produce.
UNLOCK_EVENT_TYPES = frozenset({"unlock_cliff", "unlock_linear", "emissions_change", "burn"})


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


class UnlockCollector(BaseCollector):
    """Builds `scheduled_event` from whichever unlock source is available."""

    name = "unlocks"
    rate_limit_key = "defillama"
    tier = "C"

    async def fetch(self, as_of: datetime) -> dict[str, Any]:
        """Try each source in priority order. Never fail the run on a dead source."""
        sources: dict[str, Any] = {"manual": self._load_manual_calendar()}

        # DefiLlama emissions: free until it was not. Kept in the chain because
        # a Pro key makes it work again, and because the 402 is worth logging.
        try:
            base = self.config.settings.endpoints["defillama"]
            async with self.client(base) as client:
                sources["defillama"] = await self.request_json(client, "GET", "/emissions")
        except Exception as exc:  # noqa: BLE001
            self.warn(
                "unlock_source_unavailable",
                source="defillama_emissions",
                error=str(exc)[:150],
                effect="falling back to the manual calendar; see DECISIONS.md D-008",
            )
            sources["defillama"] = None

        return sources

    def _load_manual_calendar(self) -> list[dict[str, Any]]:
        path = self.config.repo_root / UNLOCK_CALENDAR_PATH
        if not path.exists():
            return []
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return list(data.get("events") or [])

    def transform(self, raw: dict[str, Any], as_of: datetime) -> list[dict[str, Any]]:
        fetched_at = utc_now_iso()
        rows: list[dict[str, Any]] = []
        rows.extend(self._from_manual(raw.get("manual") or [], fetched_at))
        if raw.get("defillama"):
            rows.extend(self._from_defillama(raw["defillama"], fetched_at))

        if not rows:
            self.warn(
                "no_unlock_events",
                effect="L1_UNLOCK will report source_unavailable for this run",
                note="see DECISIONS.md D-007: a source outage is not evidence about the market",
            )
        return rows

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

    def _from_defillama(self, payload: Any, fetched_at: str) -> list[dict[str, Any]]:
        """Parse DefiLlama's emissions payload when a Pro key makes it reachable."""
        rows: list[dict[str, Any]] = []
        entries = payload if isinstance(payload, list) else payload.get("protocols", [])
        for entry in entries or []:
            asset = str(entry.get("symbol") or "").upper()
            if not asset:
                continue
            for event in entry.get("events", []) or []:
                date = str(event.get("timestamp") or event.get("date") or "")[:10]
                if not date:
                    continue
                rows.append(
                    {
                        "event_id": deterministic_id("defillama", asset, "unlock_cliff", date),
                        "base_asset": asset,
                        "event_type": "unlock_cliff",
                        "event_date_utc": date,
                        "recipient_type": _normalise_recipient(event.get("category")),
                        "magnitude_tokens": event.get("noOfTokens"),
                        "magnitude_usd": None,
                        "pct_of_circulating": None,
                        "description": event.get("description"),
                        "source": "defillama_emissions",
                        "confidence": "confirmed",
                        # We learned of it now. Point-in-time honesty requires
                        # that a backtest cannot use it before this moment.
                        "first_seen_utc": fetched_at,
                        "fetched_at_utc": fetched_at,
                    }
                )
        return rows

    def write(self, rows: list[dict[str, Any]]) -> int:
        """INSERT OR IGNORE semantics: first_seen_utc must never be overwritten.

        The event_id is a deterministic hash of source+asset+type+date, so
        re-running keeps the ORIGINAL first_seen_utc rather than moving it
        forward to today. Moving it would erase the record of when we actually
        knew, which is the one field a point-in-time backtest depends on.
        """
        if not rows:
            return 0
        with get_db() as db:
            existing = {
                r["event_id"]
                for r in db.query(
                    "SELECT event_id FROM scheduled_event WHERE event_id IN "
                    f"({','.join('?' for _ in rows)})",
                    [r["event_id"] for r in rows],
                )
            }
            fresh = [r for r in rows if r["event_id"] not in existing]
            return upsert(db, "scheduled_event", fresh) if fresh else 0


__all__ = ["KNOWN_RECIPIENT_TYPES", "UNLOCK_EVENT_TYPES", "UnlockCollector"]
