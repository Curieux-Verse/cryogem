"""
# WHY: ------------------------------------------------------------------------
# Aggregated liquidation history -- the input to L1 check 9, the RAVE detector.
#
# RAVE destroyed roughly $6B of market cap on roughly $52M of liquidations.
# That ratio is arithmetically impossible in an organic market: it means the
# market cap was a small float multiplied by a controlled price. A mcap-move to
# liquidation ratio above ~50:1 on a 24h move over 100% is the signature.
#
# TWO DATA-QUALITY WARNINGS THAT MUST NEVER BE FORGOTTEN, because they change
# how the output may be interpreted:
#
#   1. LIQUIDATION FEEDS ARE THROTTLED AT SOURCE. Binance's forceOrder stream
#      pushes only the LARGEST single liquidation per symbol per 1000ms.
#      Binance and Bybit both moved to one liquidation per second around
#      mid-2021; OKX caps at one per second per contract; Bybit only restored
#      full data in Feb 2025. Every liquidation total is therefore a FLOOR,
#      not a measurement.
#
#      Consequence for check 9, and it is asymmetric: the true ratio is always
#      HIGHER than the computed one (the denominator is understated). So a FAIL
#      is high-confidence, and a PASS is not evidence of anything. The screener
#      and the report both state this rather than presenting a clean number.
#
#   2. Coinalyze DELETES intraday data daily and retains only 1500-2000 points.
#      Poll and persist; never rely on their retention to backfill later.
#
# Optional collector: needs a free API key. Without one it degrades to a no-op
# with a WARN, and check 9 records data_unavailable (which counts as a FAIL --
# a screener that cannot see should say no).
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from src.collectors.base import BaseCollector
from src.db.connection import get_db
from src.db.writes import upsert
from src.symbols import parse_universe
from src.timeutil import format_day, millis_from, utc_now_iso


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


class CoinalyzeLiquidationCollector(BaseCollector):
    """Daily aggregated liquidations per symbol. Optional; needs a free key."""

    name = "coinalyze_liquidations"
    rate_limit_key = "coinalyze"
    tier = "C"

    async def fetch(self, as_of: datetime) -> dict[str, Any]:
        key = self.config.secrets.coinalyze_api_key
        if not key:
            self.warn(
                "coinalyze_key_missing",
                effect="L1 check 9 (mcap-to-liquidation) will record data_unavailable",
                remedy="set COINALYZE_API_KEY in .env or Actions secrets",
            )
            return {}

        base = self.config.settings.endpoints["coinalyze"]
        symbols = self._universe_symbols()
        if not symbols:
            return {}

        start = millis_from(as_of - timedelta(days=1))
        end = millis_from(as_of)
        out: dict[str, Any] = {}

        async with self.client(base, headers={"api_key": key}) as client:
            # Coinalyze accepts comma-separated symbols; batch to respect 40/min.
            for chunk_start in range(0, len(symbols), 20):
                chunk = symbols[chunk_start : chunk_start + 20]
                try:
                    payload = await self.request_json(
                        client,
                        "GET",
                        "/liquidation-history",
                        params={
                            "symbols": ",".join(chunk),
                            "interval": "daily",
                            "from": start,
                            "to": end,
                            "convert_to_usd": "true",
                        },
                    )
                    for entry in payload or []:
                        out[entry.get("symbol")] = entry
                except Exception as exc:  # noqa: BLE001
                    self.warn("coinalyze_chunk_failed", error=str(exc)[:150])
        return out

    def _universe_symbols(self) -> list[str]:
        """Coinalyze uses a '<SYMBOL>_PERP.A' style id for Binance perps."""
        with get_db() as db:
            latest = db.scalar(
                "SELECT MAX(snapshot_date) FROM universe_snapshot WHERE exchange='binance'"
            )
            if not latest:
                return []
            rows = db.query(
                "SELECT symbol FROM universe_snapshot "
                "WHERE exchange='binance' AND snapshot_date=? AND status='TRADING'",
                (latest,),
            )
        return [f"{r['symbol']}_PERP.A" for r in rows]

    def transform(self, raw: dict[str, Any], as_of: datetime) -> list[dict[str, Any]]:
        fetched_at = utc_now_iso()
        snapshot_date = format_day(as_of)
        rows: list[dict[str, Any]] = []

        exchange_symbols = {key: str(key).split("_PERP")[0] for key in raw if key}
        resolved = parse_universe(
            exchange_symbols.values(), self.config.settings.universe.quote_asset
        )

        for coinalyze_symbol, entry in raw.items():
            if not coinalyze_symbol:
                continue
            exchange_symbol = exchange_symbols[coinalyze_symbol]
            parsed = resolved.get(exchange_symbol)
            if parsed is None:
                continue

            history = entry.get("history") or []
            longs = sum(_f(h.get("l")) or 0.0 for h in history)
            shorts = sum(_f(h.get("s")) or 0.0 for h in history)
            rows.append(
                {
                    "snapshot_date": snapshot_date,
                    "exchange": "binance",
                    "symbol": exchange_symbol,
                    "base_asset": parsed.base_asset,
                    "liq_long_usd_24h": longs,
                    "liq_short_usd_24h": shorts,
                    "liq_total_usd_24h": longs + shorts,
                    # Always 1. Kept as an explicit column so no downstream
                    # consumer can present these totals as exact measurements.
                    "is_floor": 1,
                    "fetched_at_utc": fetched_at,
                }
            )
        return rows

    def write(self, rows: list[dict[str, Any]]) -> int:
        with get_db() as db:
            return upsert(db, "liquidation_snapshot", rows)


__all__ = ["CoinalyzeLiquidationCollector"]
