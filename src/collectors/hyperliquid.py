"""
# WHY: ------------------------------------------------------------------------
# A second, independent perp venue -- and the cleanest public tape available.
#
# Hyperliquid publishes trades at trade level and holds positions on-chain,
# rather than batching or throttling the public feed the way centralised venues
# do. Where Binance's liquidation stream is capped at roughly one print per
# second per symbol (so every liquidation total from it is a FLOOR), Hyperliquid
# is not throttled in the same way. For order-flow research it is the better
# source, and it costs nothing: no API key, one POST for every perp at once.
#
# Its value here is also corroboration. When Binance and Hyperliquid disagree
# about funding or OI on the same asset, that disagreement is information --
# and a single-venue system cannot see it at all.
#
# Funding note: Hyperliquid settles HOURLY, not 8-hourly. apr = rate * 8760.
# The same interval-normalisation rule as Binance, different constant.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from src.collectors.base import BaseCollector
from src.db.connection import get_db
from src.db.writes import upsert
from src.symbols import funding_apr
from src.timeutil import format_day, format_instant, utc_now_iso

#: Hyperliquid settles funding every hour. Fixed by the venue, not assumed.
HYPERLIQUID_FUNDING_INTERVAL_HOURS = 1.0


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


class HyperliquidCollector(BaseCollector):
    """Every Hyperliquid perp in one POST: funding, OI, premium, 24h volume."""

    name = "hyperliquid"
    rate_limit_key = "hyperliquid"
    tier = "B"

    async def fetch(self, as_of: datetime) -> list[Any]:
        url = self.config.settings.endpoints["hyperliquid"]
        async with self.client() as client:
            payload = await self.request_json(
                client, "POST", url, json={"type": "metaAndAssetCtxs"}
            )
        if not isinstance(payload, list) or len(payload) != 2:
            raise RuntimeError(
                f"metaAndAssetCtxs returned an unexpected shape ({type(payload).__name__}). "
                "Refusing to guess the layout."
            )
        return payload

    def transform(self, raw: list[Any], as_of: datetime) -> list[dict[str, Any]]:
        fetched_at = utc_now_iso()
        ts = format_instant(as_of)
        meta, contexts = raw[0], raw[1]
        universe = meta.get("universe", [])

        if len(universe) != len(contexts):
            # The two arrays are positionally paired. A length mismatch means a
            # zip would silently attach ETH's funding to SOL's name.
            self.warn(
                "hyperliquid_array_mismatch",
                universe=len(universe),
                contexts=len(contexts),
                action="truncating to the shorter of the two",
            )

        rows: list[dict[str, Any]] = []
        for asset, ctx in zip(universe, contexts):
            name = asset.get("name")
            if not name:
                continue
            mark = _f(ctx.get("markPx"))
            oracle = _f(ctx.get("oraclePx"))
            rate = _f(ctx.get("funding"))
            oi = _f(ctx.get("openInterest"))

            rows.append(
                {
                    "ts_utc": ts,
                    "exchange": "hyperliquid",
                    "symbol": name,
                    "base_asset": str(name).upper(),
                    "mark_price": mark,
                    "index_price": oracle,
                    "open_interest_base": oi,
                    "open_interest_usd": oi * mark if oi is not None and mark else None,
                    "funding_rate": rate,
                    "funding_interval_hours": HYPERLIQUID_FUNDING_INTERVAL_HOURS,
                    "funding_apr": (
                        funding_apr(rate, HYPERLIQUID_FUNDING_INTERVAL_HOURS)
                        if rate is not None
                        else None
                    ),
                    "next_funding_ts": None,  # not published in this payload
                    "premium": _f(ctx.get("premium")),
                    "volume_24h_usd": _f(ctx.get("dayNtlVlm")),
                    "fetched_at_utc": fetched_at,
                }
            )
        return rows

    def write(self, rows: list[dict[str, Any]]) -> int:
        with get_db() as db:
            written = upsert(db, "derivatives_snapshot", rows)
            # Hyperliquid is also a universe in its own right. Recording it
            # keeps the point-in-time symbol list complete across venues.
            snapshot_date = rows[0]["ts_utc"][:10] if rows else None
            if snapshot_date:
                upsert(
                    db,
                    "universe_snapshot",
                    [
                        {
                            "snapshot_date": snapshot_date,
                            "exchange": "hyperliquid",
                            "symbol": r["symbol"],
                            "base_asset": r["base_asset"],
                            "quote_asset": "USD",
                            "contract_type": "PERPETUAL",
                            "status": "TRADING",
                            "price_multiplier": 1,
                            "funding_interval_hours": HYPERLIQUID_FUNDING_INTERVAL_HOURS,
                            "fetched_at_utc": r["fetched_at_utc"],
                        }
                        for r in rows
                    ],
                )
            return written


__all__ = ["HYPERLIQUID_FUNDING_INTERVAL_HOURS", "HyperliquidCollector"]
