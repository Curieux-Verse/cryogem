"""
# WHY: ------------------------------------------------------------------------
# Net issuance: how fast an asset's circulating supply is growing, from the
# supply history CoinGecko itself keeps. It feeds the L2 supply block (D-072).
#
# The supply block was written for four inputs -- an emissions trajectory,
# burned share, staked share and the unlock clock -- and three of them had no
# writer at all: supply_metrics was read every morning and was always empty, so
# the block reduced to one ratio (circulating / total) that 36 fully-circulating
# assets tied on. This collector makes the emissions input real, and the two
# with no reliable free source (burned, staked) are dropped from scoring rather
# than left looking live.
#
# WHY CIRCULATING GROWTH, AND NOT A SCHEDULE. DefiLlama's emissions datasets
# stop at their last documented day: an open-ended inflation (RPL, ~5%/year)
# reads as ZERO future emission there, and only a few burn-heavy tokens carry a
# `burned` series (BNB yes, CAKE no). Circulating supply measured over time
# covers every asset CoinGecko prices, and it is NET: a burn is simply negative
# issuance, so "burned share" needs no column of its own.
#
# THE SOURCE. /coins/{id}/market_chart at interval=daily returns market cap and
# price at 00:00 UTC; their ratio reproduces the circulating_supply CoinGecko
# reports on /coins/markets -- RPL identical to every digit, BNB within 0.001%
# (checked 2026-09-19). So each asset is backfilled ONCE (365 days, one call),
# then extended daily from market_snapshot, which the coingecko collector has
# already written this morning. Same measurement, no extra API cost.
#
# NOISE. CoinGecko revises a supply figure and reverts it a day later often
# enough to matter (CAKE, 2026-02-11: +17.8%, -15.1% the next day). Each end of
# the window is therefore a 7-day MEDIAN, never a single reading. A revision
# that sticks is indistinguishable from issuance and is scored as issuance --
# which is usually what it is.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

import statistics
from datetime import datetime
from typing import Any

from src.collectors.base import BaseCollector
from src.collectors.coingecko import coingecko_endpoint
from src.db.connection import get_db
from src.db.writes import upsert
from src.timeutil import add_days, days_between, format_day, from_millis, utc_now_iso

#: supply_history.source for a backfilled day.
CHART_SOURCE = "coingecko_market_chart"
#: supply_history.source for the day read from this morning's market_snapshot.
SNAPSHOT_SOURCE = "coingecko_markets"
#: supply_metrics.source: what the metric was computed from.
METRIC_SOURCE = "coingecko_circulating_history"


# ==============================================================================
# Pure functions -- the whole metric, testable without a network or a database
# ==============================================================================
def chart_to_daily(payload: dict[str, Any], before_day: str) -> dict[str, float]:
    """day -> circulating supply implied by one /market_chart response.

    Only days strictly before `before_day`: the last point of the response is
    "now", a partial day whose price and cap may not even share a timestamp.
    The FIRST point of each day is kept, which at interval=daily is 00:00 UTC.
    A non-positive cap or price means CoinGecko had no supply figure that day,
    and is skipped rather than recorded as zero supply.
    """
    out: dict[str, float] = {}
    prices = payload.get("prices") or []
    caps = payload.get("market_caps") or []
    for price_point, cap_point in zip(prices, caps):
        if len(price_point) < 2 or len(cap_point) < 2 or price_point[0] != cap_point[0]:
            continue  # misaligned arrays: pairing them would divide one day by another
        day = format_day(from_millis(price_point[0]))
        if day >= before_day or day in out:
            continue
        price, cap = price_point[1], cap_point[1]
        if not price or not cap or price <= 0 or cap <= 0:
            continue
        out[day] = float(cap) / float(price)
    return out


def smoothed_level(series: dict[str, float], day: str, smoothing: int) -> float | None:
    """Median circulating supply over the `smoothing` days ending on `day`.

    Needs a majority of those days present: a median of one reading is a
    single reading, which is exactly what the smoothing exists to avoid.
    """
    values = [series[d] for d in (add_days(day, -k) for k in range(smoothing)) if d in series]
    if len(values) < smoothing // 2 + 1:
        return None
    return float(statistics.median(values))


def annualise(later: float | None, earlier: float | None, window_days: int) -> float | None:
    """Growth from `earlier` to `later`, compounded to a year. None if unmeasurable."""
    if later is None or earlier is None or earlier <= 0 or later <= 0:
        return None
    return (later / earlier) ** (365.0 / window_days) - 1.0


def net_issuance(
    series: dict[str, float], as_of_day: str, window_days: int, smoothing: int
) -> tuple[float | None, float | None]:
    """(latest window, the window before it), each annualised."""
    now = smoothed_level(series, as_of_day, smoothing)
    mid = smoothed_level(series, add_days(as_of_day, -window_days), smoothing)
    old = smoothed_level(series, add_days(as_of_day, -2 * window_days), smoothing)
    return annualise(now, mid, window_days), annualise(mid, old, window_days)


def trajectory(current: float | None, previous: float | None, tolerance: float) -> str | None:
    """falling | flat | rising: whether issuance is slowing, steady or speeding up."""
    if current is None or previous is None:
        return None
    change = current - previous
    if abs(change) <= tolerance:
        return "flat"
    return "falling" if change < 0 else "rising"


def plan_backfills(
    universe: dict[str, dict[str, Any]],
    state: dict[str, dict[str, Any]],
    priority: set[str],
    today: str,
    refresh_days: int,
) -> list[str]:
    """Assets to backfill this run, most useful first.

    Never backfilled -- or backfilled under a different CoinGecko id, which is a
    different coin -- comes first, yesterday's Layer 1 survivors ahead of the
    rest, larger market caps next. Then backfills older than `refresh_days`,
    stalest first, which heals any day the daily extension missed.
    """
    fresh: list[tuple[int, float, str]] = []
    stale: list[tuple[int, str]] = []
    for asset, info in universe.items():
        record = state.get(asset)
        if record is None or record.get("coingecko_id") != info.get("coingecko_id"):
            fresh.append((0 if asset in priority else 1, -(info.get("market_cap_usd") or 0.0), asset))
            continue
        age = days_between(str(record["backfilled_utc"])[:10], today)
        if age >= refresh_days:
            stale.append((-age, asset))
    return [a for *_, a in sorted(fresh)] + [a for _, a in sorted(stale)]


# ==============================================================================
# Collector
# ==============================================================================
class SupplyHistoryCollector(BaseCollector):
    """Backfills and extends circulating supply; writes net issuance to supply_metrics."""

    name = "supply_history"
    rate_limit_key = "coingecko"
    tier = "C"

    async def fetch(self, as_of: datetime) -> dict[str, Any]:
        cfg = self.config.settings.supply_history
        l2 = self.config.thresholds.layer2
        fetched_at = utc_now_iso()
        today = format_day(as_of)

        universe = self._universe()
        plan = plan_backfills(
            universe, self._backfill_state(), self._survivors(), today, cfg.refresh_days
        )
        batch = plan[: cfg.max_fetches_per_run]

        key = self.config.secrets.coingecko_api_key
        base, headers = coingecko_endpoint(self.config.settings, key)
        charts: dict[str, dict[str, Any]] = {}
        failed: list[str] = []
        if batch:
            async with self.client(base, headers=headers) as client:
                for asset in batch:
                    try:
                        charts[asset] = await self.request_json(
                            client,
                            "GET",
                            f"/coins/{universe[asset]['coingecko_id']}/market_chart",
                            params={
                                "vs_currency": "usd",
                                "days": cfg.history_days,
                                "interval": "daily",
                            },
                        )
                    except Exception as exc:  # noqa: BLE001 - one asset, not the run
                        failed.append(asset)
                        self.log.info(
                            "supply_history_asset_failed", asset=asset, error=str(exc)[:150]
                        )
        if failed:
            self.warn(
                "supply_history_backfills_failed",
                count=len(failed),
                assets=failed[:20],
                effect="retried next run; their net issuance stays unmeasured until then",
            )
        if len(plan) > len(batch):
            self.log.info("supply_history_backfills_deferred", remaining=len(plan) - len(batch))

        lookback = 2 * l2.supply_growth_window_days + l2.supply_smoothing_days + 2
        return {
            "today": today,
            "fetched_at": fetched_at,
            "universe": universe,
            "charts": charts,
            "stored": self._stored(add_days(today, -lookback)),
        }

    def transform(self, raw: dict[str, Any], as_of: datetime) -> list[dict[str, Any]]:
        l2 = self.config.thresholds.layer2
        today = raw["today"]
        fetched_at = raw["fetched_at"]
        universe: dict[str, dict[str, Any]] = raw.get("universe") or {}

        series: dict[str, dict[str, float]] = {}
        for row in raw.get("stored") or []:
            series.setdefault(row["base_asset"], {})[row["snapshot_date"]] = float(
                row["circulating_supply"]
            )

        rows: list[dict[str, Any]] = []
        for asset, payload in (raw.get("charts") or {}).items():
            gecko = universe.get(asset, {}).get("coingecko_id")
            daily = chart_to_daily(payload or {}, today)
            for day, supply in sorted(daily.items()):
                rows.append(self._history(day, asset, gecko, supply, CHART_SOURCE, fetched_at))
            series.setdefault(asset, {}).update(daily)
            rows.append(
                {
                    "_kind": "backfill",
                    "base_asset": asset,
                    "coingecko_id": gecko,
                    "days_returned": len(daily),
                    "backfilled_utc": fetched_at,
                    "fetched_at_utc": fetched_at,
                }
            )

        # Today's reading, from the snapshot the coingecko collector wrote this
        # morning. Only when that snapshot IS today's: yesterday's figure
        # stamped with today's date would be a fabricated reading.
        for asset, info in universe.items():
            supply = info.get("circulating_supply")
            if info.get("snapshot_date") == today and supply and supply > 0:
                rows.append(
                    self._history(
                        today, asset, info.get("coingecko_id"), supply, SNAPSHOT_SOURCE, fetched_at
                    )
                )
                series.setdefault(asset, {})[today] = float(supply)

        measured = 0
        for asset in universe:
            current, previous = net_issuance(
                series.get(asset, {}),
                today,
                l2.supply_growth_window_days,
                l2.supply_smoothing_days,
            )
            if current is None:
                continue  # too little history: NO row, so the metric reads None
            measured += 1
            rows.append(
                {
                    "_kind": "metric",
                    "snapshot_date": today,
                    "base_asset": asset,
                    "emissions_annual": current,
                    "emissions_prev_annual": previous,
                    "emissions_trajectory": trajectory(
                        current, previous, l2.emissions_trajectory_tolerance
                    ),
                    "source": METRIC_SOURCE,
                    "fetched_at_utc": fetched_at,
                }
            )

        self.log.info(
            "supply_history_processed",
            universe=len(universe),
            backfilled=len(raw.get("charts") or {}),
            measured=measured,
            unmeasured=len(universe) - measured,
            note="unmeasured assets score net issuance None, not zero",
        )
        return rows

    @staticmethod
    def _history(
        day: str, asset: str, gecko: str | None, supply: float, source: str, fetched_at: str
    ) -> dict[str, Any]:
        return {
            "_kind": "history",
            "snapshot_date": day,
            "base_asset": asset,
            "coingecko_id": gecko,
            "circulating_supply": float(supply),
            "source": source,
            "fetched_at_utc": fetched_at,
        }

    def write(self, rows: list[dict[str, Any]]) -> int:
        def of(kind: str) -> list[dict[str, Any]]:
            return [{k: v for k, v in r.items() if k != "_kind"} for r in rows if r["_kind"] == kind]

        with get_db() as db:
            written = upsert(db, "supply_history", of("history"))
            upsert(db, "supply_backfill", of("backfill"))
            written += upsert(db, "supply_metrics", of("metric"))
        return written

    # -- database reads ---------------------------------------------------------
    def _universe(self) -> dict[str, dict[str, Any]]:
        """Screened perps with a CoinGecko id, keyed by base asset."""
        with get_db() as db:
            universe_date = db.scalar(
                "SELECT MAX(snapshot_date) FROM universe_snapshot WHERE exchange = 'binance'"
            )
            market_date = db.scalar("SELECT MAX(snapshot_date) FROM market_snapshot")
            if not universe_date or not market_date:
                return {}
            rows = db.query(
                "SELECT DISTINCT m.base_asset, m.coingecko_id, m.circulating_supply, "
                "m.market_cap_usd, m.snapshot_date FROM market_snapshot m "
                "JOIN universe_snapshot u ON u.base_asset = m.base_asset "
                "AND u.snapshot_date = ? AND u.exchange = 'binance' AND u.status = 'TRADING' "
                "WHERE m.snapshot_date = ? AND m.coingecko_id IS NOT NULL",
                (universe_date, market_date),
            )
        return {r["base_asset"]: dict(r) for r in rows}

    def _backfill_state(self) -> dict[str, dict[str, Any]]:
        with get_db() as db:
            rows = db.query("SELECT base_asset, coingecko_id, backfilled_utc FROM supply_backfill")
        return {r["base_asset"]: dict(r) for r in rows}

    def _survivors(self) -> set[str]:
        """The newest Layer 1 survivors: the assets whose score this changes first."""
        with get_db() as db:
            rows = db.query(
                "SELECT base_asset FROM layer1_result WHERE passed = 1 AND run_date = "
                "(SELECT MAX(run_date) FROM layer1_result)"
            )
        return {r["base_asset"] for r in rows}

    def _stored(self, since: str) -> list[dict[str, Any]]:
        with get_db() as db:
            return db.query(
                "SELECT snapshot_date, base_asset, circulating_supply FROM supply_history "
                "WHERE snapshot_date >= ?",
                (since,),
            )


__all__ = [
    "CHART_SOURCE",
    "METRIC_SOURCE",
    "SNAPSHOT_SOURCE",
    "SupplyHistoryCollector",
    "annualise",
    "chart_to_daily",
    "net_issuance",
    "plan_backfills",
    "smoothed_level",
    "trajectory",
]
