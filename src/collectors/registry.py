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
from src.collectors.defillama import DefiLlamaCollector
from src.collectors.hyperliquid import HyperliquidCollector
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
    "announcements": BinanceAnnouncementCollector,
    "attention": AttentionCollector,
    "market_regime": MarketRegimeCollector,
    "news": NewsCollector,
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
        "unlocks",
        "announcements",
        "attention",
        "market_regime",
        "binance_derivatives",  # one daily snapshot even without the hourly tier
    ],
    # Hourly. Builds the derivatives time series.
    "hourly": [
        "binance_derivatives",
        "hyperliquid",
        "announcements",
        "news",
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


__all__ = ["COLLECTORS", "TIERS", "names", "resolve", "run_many"]
