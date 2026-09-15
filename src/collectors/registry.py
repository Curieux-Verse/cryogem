"""
# WHY: ------------------------------------------------------------------------
# One list of every collector, and which cadence tier it belongs to.
#
# The tiers exist because of a Terms-of-Service constraint, not a technical one:
#
#   Tier C (daily)  -> GitHub Actions. Everything L1, L2, journal and dashboard
#                      need. This tier alone is the whole product.
#   Tier B (hourly) -> GitHub Actions. Derivatives series, funding persistence.
#   Tier A (5 min)  -> A HOST YOU CONTROL. Never Actions. 288 runs/day of
#                      continuous collection is the "serverless computing"
#                      clause of the Actions ToS, and the penalty is losing
#                      Actions on the account that runs everything else.
#
# Ordering inside a tier matters: the universe must be collected before the
# collectors that read it to decide which symbols to poll. So a tier runs
# SEQUENTIALLY in listed order, not concurrently. Collection is I/O-bound but
# short; correctness of ordering is worth more than a few saved seconds.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

from datetime import datetime

from src.collectors.base import BaseCollector, CollectorRunResult
from src.collectors.binance import (
    BinanceDepthCollector,
    BinanceDerivativesCollector,
    BinanceSpotCollector,
    BinanceUniverseCollector,
)
from src.collectors.announcements import BinanceAnnouncementCollector
from src.collectors.attention import AttentionCollector, MarketRegimeCollector
from src.collectors.coinalyze import CoinalyzeLiquidationCollector
from src.collectors.coingecko import CoinGeckoCollector
from src.collectors.contracts import AssetContractCollector
from src.collectors.defillama import DefiLlamaCollector
from src.collectors.holders import HolderCollector
from src.collectors.hyperliquid import HyperliquidCollector
from src.collectors.klines import BinanceKlinesCollector
from src.collectors.news import NewsCollector
from src.collectors.unlocks import UnlockCollector
from src.logging_setup import get_logger

log = get_logger("collectors.registry")

#: name -> factory. A factory, not an instance: a collector reads config and
#: opens a limiter at construction, and that should happen per run.
COLLECTORS: dict[str, type[BaseCollector]] = {
    "binance_universe": BinanceUniverseCollector,
    "binance_derivatives": BinanceDerivativesCollector,
    "binance_spot": BinanceSpotCollector,
    "binance_depth": BinanceDepthCollector,
    "hyperliquid": HyperliquidCollector,
    "coingecko": CoinGeckoCollector,
    "defillama": DefiLlamaCollector,
    "coinalyze_liquidations": CoinalyzeLiquidationCollector,
    "unlocks": UnlockCollector,
    "asset_contracts": AssetContractCollector,
    "holders": HolderCollector,
    "announcements": BinanceAnnouncementCollector,
    "attention": AttentionCollector,
    "market_regime": MarketRegimeCollector,
    "news": NewsCollector,
    "binance_klines": BinanceKlinesCollector,
}

#: Ordered per tier. Universe first -- later collectors read it.
TIERS: dict[str, list[str]] = {
    # Daily. Everything L1 and L2 actually require.
    "daily": [
        "binance_universe",
        "binance_spot",
        "coingecko",
        "defillama",
        "coinalyze_liquidations",
        "announcements",
        "attention",
        "market_regime",
        # After coingecko, so its full OHLC replaces the close-only snapshot
        # rather than the reverse. Both write price_daily; only this one has a
        # high and a low, which is what the journal's excursions need.
        "binance_klines",
        "binance_derivatives",  # one daily snapshot even without the hourly tier
    ],
    # Hourly. Builds the derivatives time series.
    "hourly": [
        "binance_derivatives",
        "hyperliquid",
        "announcements",
        "news",
    ],
    # Supply: holder concentration (R1) and the unlock calendar (R4). Slow,
    # paced by free-tier limits, and refreshed in bounded batches, so it runs
    # in its own workflow (collect-supply.yml) and never spends collect-daily's
    # 20-minute budget. Order matters: holders read asset_contracts, and
    # unlocks resolve DefiLlama token addresses through the same table.
    "supply": [
        "asset_contracts",
        "holders",
        "unlocks",
    ],
    # 5-minute. HOST ONLY -- never wire this to GitHub Actions.
    "fast": [
        "binance_derivatives",
        "hyperliquid",
        "binance_depth",
    ],
}


def names() -> list[str]:
    return sorted(COLLECTORS) + sorted(TIERS) + ["all"]


def resolve(selector: str) -> list[BaseCollector]:
    """Turn a CLI argument into collector instances, in run order."""
    key = selector.strip().lower()
    if key in TIERS:
        return [COLLECTORS[n]() for n in TIERS[key]]
    if key == "all":
        return [COLLECTORS[n]() for n in COLLECTORS]
    if key in COLLECTORS:
        return [COLLECTORS[key]()]
    return []


def refuse_as_of(collectors: list[BaseCollector], as_of_day: str, today: str) -> list[str]:
    """Why each collector cannot run for `as_of_day`; empty when all can (D-048).

    Every API here but Binance klines serves only the present. Run for a past
    date, a collector stamps today's values onto that day, and its upsert
    overwrites what was recorded then -- point-in-time data corrupted for good.
    """
    if as_of_day > today:
        return [f"{c.name}: {as_of_day} is in the future" for c in collectors]
    if as_of_day == today:
        return []
    return [
        f"{c.name}: serves only live data, so it cannot record {as_of_day}"
        for c in collectors
        if not c.accepts_past_as_of
    ]


async def run_many(
    collectors: list[BaseCollector], as_of: datetime
) -> list[CollectorRunResult]:
    """Run collectors sequentially, in order. One failure does not stop the rest.

    Sequential because of the ordering dependency described in the module
    docstring. Continuing after a failure is deliberate: if CoinGecko is down,
    the derivatives snapshot for that hour is still worth recording, and the
    lost data cannot be re-fetched later.
    """
    results: list[CollectorRunResult] = []
    for collector in collectors:
        result = await collector.run(as_of)
        results.append(result)
        if not result.ok:
            log.error(
                "collector_failed_continuing",
                collector=collector.name,
                error=result.error_message,
                note="later collectors still run; missed data cannot be re-fetched",
            )
    return results


__all__ = ["COLLECTORS", "TIERS", "names", "refuse_as_of", "resolve", "run_many"]
