"""
# WHY: ------------------------------------------------------------------------
# Market cap, float and all-time-high -- the inputs to four of the nine L1
# checks and to the drawdown block in L2.
#
# The trap here is TICKER COLLISION, and it is mandatory to handle:
#
#   Ticker symbols are not unique on CoinGecko. A $20M token can share a symbol
#   with a $2B one. Join naively and a micro-cap inherits a large-cap's market
#   cap, sailing straight through the "< $30M" kill switch -- the single check
#   most likely to have caught it.
#
# The fix: iterate pages in market-cap-DESCENDING order and keep the FIRST
# match for each symbol. That biases toward attributing the LARGER market cap,
# which means the screener under-flags rather than over-flags.
#
# Under-flagging is the safe direction here, and it is worth being explicit
# about why: this is a filter whose job is to justify a "no". A false "no" costs
# an opportunity; a false "yes" costs money. When forced to be wrong, be wrong
# in the direction that keeps a bad asset OUT is impossible here -- so instead
# we accept the risk of letting one through rather than silently mislabelling
# every mid-cap with a colliding ticker.
#
# The residual risk is real and unfixable without paid per-asset id mapping.
# It is recorded in docs/API_DEVIATIONS.md rather than hidden.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from src.collectors.base import BaseCollector
from src.db.connection import get_db
from src.db.writes import upsert
from src.timeutil import format_day, utc_now_iso


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


def coingecko_endpoint(settings: Any, key: str | None) -> tuple[str, dict[str, str]]:
    """Host and auth header for the configured CoinGecko plan (D-054).

    Demo keys (the free tier) and Pro keys look alike -- both begin 'CG-' -- but
    each is accepted only on its own host with its own header. A Demo key sent to
    the Pro host is refused on page 1, which fails the whole collector, and every
    asset then fails L1_NO_MCAP.
    """
    if not key:
        return settings.endpoints["coingecko"], {}
    if settings.universe.coingecko_plan == "pro":
        return settings.endpoints["coingecko_pro"], {"x-cg-pro-api-key": key}
    return settings.endpoints["coingecko"], {"x-cg-demo-api-key": key}


class CoinGeckoCollector(BaseCollector):
    """Daily market data for roughly the top 2000 assets by market cap."""

    name = "coingecko"
    rate_limit_key = "coingecko"
    tier = "C"

    async def fetch(self, as_of: datetime) -> list[dict]:
        universe = self.config.settings.universe
        key = self.config.secrets.coingecko_api_key
        base, headers = coingecko_endpoint(self.config.settings, key)
        if not key:
            self.log.info(
                "coingecko_free_tier",
                note="no API key set; paginating slowly to stay inside the free limit",
            )

        pages: list[dict] = []
        async with self.client(base, headers=headers) as client:
            for page in range(1, universe.coingecko_pages + 1):
                try:
                    payload = await self.request_json(
                        client,
                        "GET",
                        "/coins/markets",
                        params={
                            "vs_currency": "usd",
                            "order": "market_cap_desc",  # ORDER MATTERS -- see docstring
                            "per_page": 250,
                            "page": page,
                            "sparkline": "false",
                            "price_change_percentage": "24h",
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    # Partial coverage beats none. Pages arrive in market-cap
                    # order, so losing page 6 costs the SMALLEST assets -- and
                    # an asset with no market-cap row fails L1_NO_MCAP, which
                    # is the correct, conservative outcome. Losing every page
                    # instead would starve all nine checks and screen nothing.
                    if page == 1:
                        raise
                    self.warn(
                        "coingecko_pagination_truncated",
                        stopped_at_page=page,
                        pages_collected=page - 1,
                        error=str(exc)[:150],
                        effect="assets below the collected market-cap floor will fail L1_NO_MCAP",
                    )
                    break
                if not payload:
                    break
                pages.extend(payload)
                # The free tier is strict and unforgiving about bursts.
                await asyncio.sleep(universe.coingecko_page_pause_seconds)
        return pages

    def transform(self, raw: list[dict], as_of: datetime) -> list[dict[str, Any]]:
        fetched_at = utc_now_iso()
        snapshot_date = format_day(as_of)
        seen: set[str] = set()
        rows: list[dict[str, Any]] = []
        collisions = 0

        # raw arrives in market-cap-descending order, so the first time a symbol
        # appears it is the largest asset carrying that ticker.
        for coin in raw:
            symbol = str(coin.get("symbol") or "").upper()
            if not symbol:
                continue
            if symbol in seen:
                collisions += 1
                continue
            if coin.get("market_cap") in (None, 0):
                # No market cap is not the same as a market cap of zero. Skip,
                # and let L1_NO_MCAP fail the asset for unresolvable data.
                continue
            seen.add(symbol)

            price = _f(coin.get("current_price"))
            ath = _f(coin.get("ath"))
            rows.append(
                {
                    "snapshot_date": snapshot_date,
                    "base_asset": symbol,
                    "coingecko_id": coin.get("id"),
                    "price_usd": price,
                    "market_cap_usd": _f(coin.get("market_cap")),
                    "fdv_usd": _f(coin.get("fully_diluted_valuation")),
                    "circulating_supply": _f(coin.get("circulating_supply")),
                    "total_supply": _f(coin.get("total_supply")),
                    "max_supply": _f(coin.get("max_supply")),
                    "spot_volume_24h_usd": _f(coin.get("total_volume")),
                    "ath_usd": ath,
                    "ath_date": (coin.get("ath_date") or "")[:10] or None,
                    "pct_below_ath": (
                        (price - ath) / ath if price is not None and ath else None
                    ),
                    "price_change_24h_pct": _f(
                        coin.get("price_change_percentage_24h_in_currency")
                        if coin.get("price_change_percentage_24h_in_currency") is not None
                        else coin.get("price_change_percentage_24h")
                    ),
                    "fetched_at_utc": fetched_at,
                }
            )

        if collisions:
            self.log.info(
                "ticker_collisions_resolved",
                count=collisions,
                rule="kept the largest market cap per ticker",
                bias="under-flagging, which is the safe direction for a filter",
            )
        return rows

    def write(self, rows: list[dict[str, Any]]) -> int:
        with get_db() as db:
            written = upsert(db, "market_snapshot", rows)
            # Daily closes double as the price history the journal needs to
            # compute forward returns once a horizon elapses.
            price_rows = [
                {
                    "snapshot_date": r["snapshot_date"],
                    "base_asset": r["base_asset"],
                    "close_usd": r["price_usd"],
                    "volume_usd": r["spot_volume_24h_usd"],
                    "source": "coingecko",
                    "fetched_at_utc": r["fetched_at_utc"],
                }
                for r in rows
                if r.get("price_usd") is not None
            ]
            upsert(db, "price_daily", price_rows)
            return written


__all__ = ["CoinGeckoCollector"]
