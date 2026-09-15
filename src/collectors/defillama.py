"""
# WHY: ------------------------------------------------------------------------
# Revenue, fees and TVL -- the fundamental block of the L2 score, weight 35.
#
# The reference case is VVV: protocol revenue moving from a ~$70M to a ~$100M
# annualised run-rate inside a single month, while emissions were being cut and
# a large share of supply burned. That combination is visible in this data
# BEFORE it is visible in price, which is the entire premise of scoring it.
#
# Free, no API key, ~8000 protocols.
#
# The important discipline here is the asset -> protocol mapping. It is NOT
# guessable: token tickers and protocol slugs disagree constantly, and a wrong
# mapping attaches another project's revenue to an asset, which is worse than
# having no revenue data at all. So mappings live in config/protocol_map.yaml,
# unmapped assets get has_fundamentals = 0, and the L2 fundamental block scores
# None for them with its weight redistributed.
#
# "No revenue model" is information, not a gap. An asset with no fundamentals
# must carry itself on supply and narrative alone, and the journal will later
# show whether that class of asset performed differently.
#
# TVL caveat, embedded deliberately: TVL is a CAPITAL SNAPSHOT, not activity.
# It rises when prices rise with no new deposits. Weighted low for that reason.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from src.collectors.base import BaseCollector
from src.db.connection import get_db
from src.db.writes import upsert
from src.timeutil import format_day, utc_now_iso

PROTOCOL_MAP_PATH = Path("config/protocol_map.yaml")


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


def load_protocol_map(repo_root: Path) -> dict[str, str]:
    """base_asset -> DefiLlama protocol slug. Explicit mappings only."""
    path = repo_root / PROTOCOL_MAP_PATH
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    mapping = data.get("protocols", {}) or {}
    return {str(k).upper(): str(v) for k, v in mapping.items()}


class DefiLlamaCollector(BaseCollector):
    """Daily TVL, fees and revenue for mapped assets."""

    name = "defillama"
    rate_limit_key = "defillama"
    tier = "C"

    async def fetch(self, as_of: datetime) -> dict[str, Any]:
        base = self.config.settings.endpoints["defillama"]
        async with self.client(base) as client:
            protocols = await self.request_json(client, "GET", "/protocols")
            fees = await self.request_json(
                client, "GET", "/overview/fees", params={"dataType": "dailyFees"}
            )
            revenue = await self.request_json(
                client, "GET", "/overview/fees", params={"dataType": "dailyRevenue"}
            )
        return {"protocols": protocols, "fees": fees, "revenue": revenue}

    def transform(self, raw: dict[str, Any], as_of: datetime) -> list[dict[str, Any]]:
        fetched_at = utc_now_iso()
        snapshot_date = format_day(as_of)
        mapping = load_protocol_map(self.config.repo_root)

        by_slug = {p.get("slug"): p for p in raw["protocols"] if p.get("slug")}
        fees_by_slug = self._index_overview(raw["fees"])
        rev_by_slug = self._index_overview(raw["revenue"])

        assets = self._universe_assets()
        rows: list[dict[str, Any]] = []
        mapped = 0

        for asset in assets:
            slug = mapping.get(asset)
            protocol = by_slug.get(slug) if slug else None
            fee = fees_by_slug.get(slug) if slug else None
            rev = rev_by_slug.get(slug) if slug else None
            has_fundamentals = 1 if (protocol or fee or rev) else 0
            mapped += has_fundamentals

            revenue_30d = _f((rev or {}).get("total30d"))
            rows.append(
                {
                    "snapshot_date": snapshot_date,
                    "base_asset": asset,
                    "protocol_slug": slug,
                    "tvl_usd": _f((protocol or {}).get("tvl")),
                    "fees_24h_usd": _f((fee or {}).get("total24h")),
                    "fees_7d_usd": _f((fee or {}).get("total7d")),
                    "fees_30d_usd": _f((fee or {}).get("total30d")),
                    "revenue_24h_usd": _f((rev or {}).get("total24h")),
                    "revenue_7d_usd": _f((rev or {}).get("total7d")),
                    "revenue_30d_usd": revenue_30d,
                    "revenue_prev_30d_usd": _f((rev or {}).get("total60dto30d")),
                    "revenue_annualised": (
                        revenue_30d * (365.0 / 30.0) if revenue_30d is not None else None
                    ),
                    "active_addresses_24h": None,
                    "has_fundamentals": has_fundamentals,
                    "fetched_at_utc": fetched_at,
                }
            )

        self.log.info(
            "defillama_mapping",
            assets=len(assets),
            with_fundamentals=mapped,
            without=len(assets) - mapped,
            note="unmapped assets score the fundamental block None, not 0",
        )
        return rows

    @staticmethod
    def _index_overview(payload: dict[str, Any]) -> dict[str, dict]:
        """Index a DefiLlama /overview response by protocol slug."""
        out: dict[str, dict] = {}
        for item in (payload or {}).get("protocols", []) or []:
            slug = item.get("slug") or item.get("module")
            if slug:
                out[slug] = item
        return out

    def _universe_assets(self) -> list[str]:
        with get_db() as db:
            # Binance only (D-050): Hyperliquid's hourly rows would otherwise make
            # the newest date theirs, and bring their tickers with it.
            latest = db.scalar(
                "SELECT MAX(snapshot_date) FROM universe_snapshot WHERE exchange = 'binance'"
            )
            if not latest:
                return []
            rows = db.query(
                "SELECT DISTINCT base_asset FROM universe_snapshot "
                "WHERE snapshot_date = ? AND exchange = 'binance'",
                (latest,),
            )
        return sorted(r["base_asset"] for r in rows)

    def write(self, rows: list[dict[str, Any]]) -> int:
        with get_db() as db:
            return upsert(db, "fundamentals_snapshot", rows)


__all__ = ["DefiLlamaCollector", "load_protocol_map"]
