"""
# WHY: ------------------------------------------------------------------------
# Tier-2 attention. Unlike news (Tier 3), this one IS scored -- weight 15.
#
# THE SCORING RULE THAT MAKES OR BREAKS THIS BLOCK:
#
#     Never use raw social volume. Use the Z-SCORE of an asset's social volume
#     against ITS OWN trailing 30-day distribution, then percentile-rank that
#     cross-sectionally.
#
# Raw volume just ranks by market cap. BTC is always discussed more than a
# mid-cap, so a raw-volume factor is a market-cap factor wearing a disguise and
# will correlate with everything else in the score. The z-score asks the only
# question that matters: is this asset being discussed unusually much FOR
# ITSELF, right now?
#
# THE GATE: this block contributes only when |z| > 2. Sentiment's predictive
# power concentrates at extreme market states, not in the middle of the
# distribution, so scoring the middle adds noise wearing a confident face.
#
# CAVEATS THAT DO NOT GO AWAY, recorded because they bound what this block can
# ever be worth:
#   * Bot contamination is unresolved. Roughly 14% of crypto tweets have been
#     attributed to bot accounts, and no vendor eliminates bot influence.
#   * Social data is the easiest input in this system to manufacture. An asset
#     whose attention spikes may simply have paid for the spike.
#
# Both are why this block is weighted 15 and gated rather than trusted.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

import statistics
from datetime import datetime
from typing import Any

from src.collectors.base import BaseCollector
from src.db.connection import get_db
from src.db.writes import upsert
from src.timeutil import add_days, format_day, utc_now_iso


def zscore(current: float, history: list[float]) -> float | None:
    """Z-score of `current` against an asset's own history.

    Returns None when there is too little history or no variance. None is the
    correct answer: a z-score computed from three observations is a number, not
    a measurement, and the gate would act on it just as readily.
    """
    usable = [v for v in history if v is not None]
    if len(usable) < 10:
        return None
    mean = statistics.fmean(usable)
    try:
        stdev = statistics.stdev(usable)
    except statistics.StatisticsError:
        return None
    if stdev == 0:
        return None
    return (current - mean) / stdev


class AttentionCollector(BaseCollector):
    """Social volume and dominance per asset, stored as a z-score."""

    name = "attention"
    rate_limit_key = "lunarcrush"
    tier = "C"

    async def fetch(self, as_of: datetime) -> dict[str, Any]:
        out: dict[str, Any] = {"lunarcrush": None}
        key = self.config.secrets.lunarcrush_api_key
        if not key:
            self.warn(
                "lunarcrush_key_missing",
                effect="the attention block scores None and its weight is redistributed",
                note="honest degradation, never a zero",
            )
            return out
        try:
            base = self.config.settings.endpoints["lunarcrush"]
            async with self.client(base, headers={"Authorization": f"Bearer {key}"}) as client:
                out["lunarcrush"] = await self.request_json(client, "GET", "/coins/list/v1")
        except Exception as exc:  # noqa: BLE001
            self.warn("lunarcrush_unavailable", error=str(exc)[:150])
        return out

    def transform(self, raw: dict[str, Any], as_of: datetime) -> list[dict[str, Any]]:
        fetched_at = utc_now_iso()
        snapshot_date = format_day(as_of)
        payload = raw.get("lunarcrush")
        if not payload:
            return []

        history = self._volume_history(snapshot_date)
        rows: list[dict[str, Any]] = []
        for coin in payload.get("data", []):
            asset = str(coin.get("symbol") or "").upper()
            if not asset:
                continue
            volume = coin.get("social_volume_24h") or coin.get("social_volume")
            rows.append(
                {
                    "snapshot_date": snapshot_date,
                    "base_asset": asset,
                    "social_volume": volume,
                    # Score on THIS, never on raw volume.
                    "social_volume_z": (
                        zscore(float(volume), history.get(asset, []))
                        if volume is not None
                        else None
                    ),
                    "social_dominance": coin.get("social_dominance"),
                    "social_engagement": coin.get("interactions_24h"),
                    "sentiment_score": coin.get("sentiment"),
                    "galaxy_score": coin.get("galaxy_score"),
                    "alt_rank": coin.get("alt_rank"),
                    "google_trends": None,
                    "source": "lunarcrush",
                    "fetched_at_utc": fetched_at,
                }
            )
        return rows

    def _volume_history(self, snapshot_date: str) -> dict[str, list[float]]:
        """Trailing social-volume window per asset: the z-score denominator."""
        window = self.config.thresholds.layer2.attention_zscore_window_days
        since = add_days(snapshot_date, -window)
        with get_db() as db:
            rows = db.query(
                "SELECT base_asset, social_volume FROM attention_snapshot "
                "WHERE snapshot_date >= ? AND snapshot_date < ? AND social_volume IS NOT NULL",
                (since, snapshot_date),
            )
        history: dict[str, list[float]] = {}
        for row in rows:
            history.setdefault(row["base_asset"], []).append(float(row["social_volume"]))
        return history

    def write(self, rows: list[dict[str, Any]]) -> int:
        with get_db() as db:
            return upsert(db, "attention_snapshot", rows)


class MarketRegimeCollector(BaseCollector):
    """Market-wide Fear and Greed, plus the BTC regime label the backtest splits on."""

    name = "market_regime"
    rate_limit_key = "defillama"
    tier = "C"

    async def fetch(self, as_of: datetime) -> dict[str, Any]:
        async with self.client() as client:
            return await self.request_json(
                client,
                "GET",
                self.config.settings.endpoints["alternative_me"],
                params={"limit": 1},
            )

    def transform(self, raw: dict[str, Any], as_of: datetime) -> list[dict[str, Any]]:
        entries = (raw or {}).get("data") or []
        if not entries:
            self.warn("fear_greed_empty")
            return []
        entry = entries[0]
        snapshot_date = format_day(as_of)
        btc_30d = self._btc_return_30d(snapshot_date)
        cfg = self.config.thresholds.backtest

        regime = None
        if btc_30d is not None:
            if btc_30d >= cfg.regime_btc_up_pct:
                regime = "btc_up"
            elif btc_30d <= cfg.regime_btc_down_pct:
                regime = "btc_down"
            else:
                regime = "btc_flat"

        value = entry.get("value")
        return [
            {
                "snapshot_date": snapshot_date,
                "fear_greed_value": int(value) if value not in (None, "") else None,
                "fear_greed_label": entry.get("value_classification"),
                "btc_return_30d": btc_30d,
                "regime": regime,
                "fetched_at_utc": utc_now_iso(),
            }
        ]

    def _btc_return_30d(self, snapshot_date: str) -> float | None:
        with get_db() as db:
            now = db.scalar(
                "SELECT close_usd FROM price_daily WHERE base_asset='BTC' "
                "AND snapshot_date <= ? ORDER BY snapshot_date DESC LIMIT 1",
                (snapshot_date,),
            )
            then = db.scalar(
                "SELECT close_usd FROM price_daily WHERE base_asset='BTC' "
                "AND snapshot_date <= ? ORDER BY snapshot_date DESC LIMIT 1",
                (add_days(snapshot_date, -30),),
            )
        if not now or not then:
            return None
        return (float(now) - float(then)) / float(then)

    def write(self, rows: list[dict[str, Any]]) -> int:
        with get_db() as db:
            return upsert(db, "market_regime", rows)


__all__ = ["AttentionCollector", "MarketRegimeCollector", "zscore"]
