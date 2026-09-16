"""
# WHY: ------------------------------------------------------------------------
# Which token contract, on which chain, holder concentration is read from --
# or why there is none. The first half of R1.
#
# The universe is Binance perps keyed by ticker; a holder API wants a chain and
# a contract address. CoinGecko joins the two: market_snapshot carries each
# asset's coingecko_id, and /coins/list?include_platform=true maps every id to
# its contract on every chain, in one keyless call.
#
# Three outcomes, and conflating them is the bug this module exists to prevent:
#
#   measurable         a token contract on a chain GoPlus serves.
#   native_coin        a chain's own coin -- BTC, ETH, SOL, HYPE, AVAX. There is
#                      no token contract, so the holder check DOES NOT APPLY.
#                      That is not a data gap, and treating it as one would
#                      disqualify the largest assets in the market for being
#                      native (DECISIONS.md D-035).
#   unsupported_chain  a token whose origin chain no holder source covers
#                      (Sui, TON, ...). That IS a gap: we cannot see it.
#
# Native is decided from CoinGecko's own /asset_platforms, whose native_coin_id
# names each chain's coin -- not merely from an absence of platforms, because a
# native coin can also list a bridged token, and the holders of a bridge copy
# describe the bridge, not the coin.
#
# CHAINS ARE DATA, NOT CODE. A CoinGecko platform is readable when its EVM
# chain_identifier appears in GoPlus's own supported_chains, or it is one of
# the non-EVM chains named in settings (solana, tron). When GoPlus adds a
# chain, coverage widens with no code change.
#
# MULTI-CHAIN TOKENS. On 2026-09-13, 189 of 324 measurable screened assets
# existed on more than one supported chain, and only the ORIGIN chain's holders
# describe the token -- a bridged copy's top holder is the bridge. CoinGecko's
# asset_platform_id (/coins/{id}) is authoritative but costs one call each at
# 10/min, so it is looked up in bounded batches and cached. Until it arrives,
# the first platform coins/list names is used and marked
# resolved_via='coins_list_first_platform'; the lookup overwrites it.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from src.collectors.base import BaseCollector
from src.collectors.coingecko import coingecko_endpoint
from src.db.connection import get_db
from src.db.writes import json_dump, upsert
from src.timeutil import utc_now_iso

MEASURABLE = "measurable"
NATIVE_COIN = "native_coin"
UNSUPPORTED_CHAIN = "unsupported_chain"

#: resolved_via for a guess that an origin lookup should confirm.
PROVISIONAL = "coins_list_first_platform"


class _NotLookedUp:
    """Sentinel: no /coins/{id} lookup has been made. Distinct from None,
    which is a lookup that returned no origin platform (a native coin)."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "NOT_LOOKED_UP"


NOT_LOOKED_UP: Any = _NotLookedUp()


def platform_targets(
    asset_platforms: list[dict[str, Any]],
    supported_chain_ids: list[Any],
    non_evm_goplus_ids: dict[str, str],
) -> dict[str, str]:
    """CoinGecko platform key -> GoPlus chain id, for every platform GoPlus reads."""
    supported = {str(c) for c in supported_chain_ids}
    targets: dict[str, str] = {}
    for platform in asset_platforms or []:
        key = platform.get("id")
        if not key:
            continue
        if key in non_evm_goplus_ids:
            goplus_id = str(non_evm_goplus_ids[key])
            # Solana has its own GoPlus endpoint and is absent from
            # supported_chains; tron is listed there by name.
            if goplus_id == "solana" or goplus_id in supported:
                targets[key] = goplus_id
            continue
        chain_id = platform.get("chain_identifier")
        if chain_id is not None and str(chain_id) in supported:
            targets[key] = str(chain_id)
    return targets


def native_coin_ids(asset_platforms: list[dict[str, Any]]) -> frozenset[str]:
    """Every coin CoinGecko names as some chain's native coin."""
    return frozenset(p["native_coin_id"] for p in asset_platforms or [] if p.get("native_coin_id"))


def live_platforms(platforms: dict[str, Any] | None) -> dict[str, str]:
    """Platforms that carry an actual address, in CoinGecko's order."""
    return {
        str(key): str(value).strip()
        for key, value in (platforms or {}).items()
        if key and value and str(value).strip()
    }


def classify(
    coingecko_id: str,
    platforms: dict[str, Any] | None,
    natives: frozenset[str],
    targets: dict[str, str],
    origin: Any = NOT_LOOKED_UP,
) -> dict[str, Any]:
    """Decide where, if anywhere, this asset's holders are read from.

    `origin` is CoinGecko's asset_platform_id once a lookup has been made: a
    platform key, or None for a coin with no origin platform.
    """
    live = live_platforms(platforms)
    decided: dict[str, Any] = {
        "applicability": UNSUPPORTED_CHAIN,
        "platform": None,
        "goplus_chain": None,
        "contract_address": None,
        "resolved_via": "no_supported_platform",
        "origin_platform": None,
        "platforms": live,
    }

    def measurable(platform: str, via: str) -> dict[str, Any]:
        decided.update(
            applicability=MEASURABLE,
            platform=platform,
            goplus_chain=targets[platform],
            contract_address=live[platform],
            resolved_via=via,
        )
        return decided

    if coingecko_id in natives:
        decided.update(applicability=NATIVE_COIN, resolved_via="native_coin_id")
        return decided

    if origin is not NOT_LOOKED_UP:
        decided["origin_platform"] = origin
        decided["resolved_via"] = "asset_platform_id"
        if origin is None:
            # CoinGecko names no origin platform: the coin is its own chain's.
            decided["applicability"] = NATIVE_COIN
            return decided
        if origin in targets and origin in live:
            return measurable(origin, "asset_platform_id")
        # The origin is a chain we cannot read. A bridged copy on a chain we
        # can read is not the token -- its top holder would be the bridge.
        return decided

    if not live:
        # No contract listed anywhere. For a coin with a Binance perp that is
        # overwhelmingly a chain's own coin that CoinGecko does not name as a
        # platform's native coin (XRP, ADA, DOGE, LTC). resolved_via keeps the
        # rule visible, so the assumption can be audited per asset.
        decided.update(applicability=NATIVE_COIN, resolved_via="no_platforms")
        return decided

    first = next(iter(live))
    if len(live) == 1:
        return measurable(first, "single_platform") if first in targets else decided

    decided["resolved_via"] = PROVISIONAL
    return measurable(first, PROVISIONAL) if first in targets else decided


class AssetContractCollector(BaseCollector):
    """Resolves asset_contract for every screened asset that has a coingecko_id."""

    name = "asset_contracts"
    rate_limit_key = "coingecko"
    tier = "C"

    async def fetch(self, as_of: datetime) -> dict[str, Any]:
        settings = self.config.settings
        screened = self._screened_ids()
        confirmed = self._confirmed_origins()
        # The same host-and-header pair the market collector uses (D-054). Sent
        # keyless, /coins/{id} is limited to ~10 a minute, which is what makes
        # max_origin_lookups_per_run small and the origin table slow to fill.
        cg_base, cg_headers = coingecko_endpoint(settings, self.config.secrets.coingecko_api_key)

        async with self.client(cg_base, headers=cg_headers) as client:
            asset_platforms = await self.request_json(client, "GET", "/asset_platforms")
            coins = await self.request_json(
                client, "GET", "/coins/list", params={"include_platform": "true"}
            )
        async with self.client(settings.endpoints["goplus"]) as client:
            chains = await self.request_json(
                client,
                "GET",
                "/supported_chains",
                params={"name": "token_security"},
                limiter_key="goplus",
            )
        supported = [
            c.get("id") for c in ((chains or {}).get("result") or []) if c.get("id") is not None
        ]
        if not supported:
            # Never proceed on an empty chain list: it would mark every token
            # unsupported and, through L1, disqualify the measurable universe.
            raise RuntimeError("GoPlus supported_chains returned no chains; keeping the cached table")

        by_id = {c.get("id"): c for c in coins or [] if c.get("id") in screened}
        targets = platform_targets(asset_platforms, supported, settings.holders.non_evm_goplus_ids)
        natives = native_coin_ids(asset_platforms)

        pending = [
            cid
            for cid in sorted(by_id)
            if cid not in confirmed
            and classify(cid, by_id[cid].get("platforms"), natives, targets)["resolved_via"]
            == PROVISIONAL
        ]
        budget = settings.contracts.max_origin_lookups_per_run
        looked_up: dict[str, str | None] = {}
        failures = 0
        if pending:
            async with self.client(cg_base, headers=cg_headers) as client:
                for cid in pending[:budget]:
                    try:
                        detail = await self.request_json(
                            client,
                            "GET",
                            f"/coins/{cid}",
                            params={
                                "localization": "false",
                                "tickers": "false",
                                "market_data": "false",
                                "community_data": "false",
                                "developer_data": "false",
                                "sparkline": "false",
                            },
                        )
                    except Exception as exc:  # noqa: BLE001 - one coin, not the run
                        failures += 1
                        self.log.warning("origin_lookup_failed", coingecko_id=cid, error=str(exc)[:150])
                        continue
                    looked_up[cid] = (detail or {}).get("asset_platform_id")
        if failures:
            self.warn("origin_lookups_failed", count=failures)
        if len(pending) > budget:
            self.log.info(
                "origin_lookups_deferred",
                remaining=len(pending) - budget,
                note="first-platform rule stands for these until a later run confirms them",
            )

        missing = sorted(screened - set(by_id))
        if missing:
            self.log.info("coingecko_ids_not_in_coins_list", count=len(missing), sample=missing[:10])

        return {
            "coins": by_id,
            "asset_platforms": asset_platforms,
            "supported_chains": supported,
            "origins": {**confirmed, **looked_up},
            "fetched_at": utc_now_iso(),
        }

    def transform(self, raw: dict[str, Any], as_of: datetime) -> list[dict[str, Any]]:
        targets = platform_targets(
            raw["asset_platforms"],
            raw["supported_chains"],
            self.config.settings.holders.non_evm_goplus_ids,
        )
        natives = native_coin_ids(raw["asset_platforms"])
        origins = raw.get("origins") or {}

        rows: list[dict[str, Any]] = []
        for cid, coin in sorted((raw.get("coins") or {}).items()):
            origin = origins[cid] if cid in origins else NOT_LOOKED_UP
            decided = classify(cid, coin.get("platforms"), natives, targets, origin)
            rows.append(
                {
                    "coingecko_id": cid,
                    "applicability": decided["applicability"],
                    "platform": decided["platform"],
                    "goplus_chain": decided["goplus_chain"],
                    "contract_address": decided["contract_address"],
                    "resolved_via": decided["resolved_via"],
                    "origin_platform": decided["origin_platform"],
                    "platforms_json": json_dump(decided["platforms"]),
                    "fetched_at_utc": raw["fetched_at"],
                }
            )

        counts = Counter(r["applicability"] for r in rows)
        self.log.info(
            "asset_contracts_resolved",
            measurable=counts[MEASURABLE],
            native_coin=counts[NATIVE_COIN],
            unsupported_chain=counts[UNSUPPORTED_CHAIN],
            provisional=sum(1 for r in rows if r["resolved_via"] == PROVISIONAL),
            chains_readable=len(targets),
        )
        return rows

    def write(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        with get_db() as db:
            return upsert(db, "asset_contract", rows)

    # -- database reads --------------------------------------------------------
    def _screened_ids(self) -> set[str]:
        with get_db() as db:
            universe_date = db.scalar(
                "SELECT MAX(snapshot_date) FROM universe_snapshot WHERE exchange = 'binance'"
            )
            market_date = db.scalar("SELECT MAX(snapshot_date) FROM market_snapshot")
            if not universe_date or not market_date:
                raise RuntimeError("no universe or market snapshot yet: run `collect daily` first")
            rows = db.query(
                "SELECT DISTINCT m.coingecko_id FROM market_snapshot m "
                "JOIN universe_snapshot u ON u.base_asset = m.base_asset "
                "AND u.snapshot_date = ? AND u.exchange = 'binance' AND u.status = 'TRADING' "
                "WHERE m.snapshot_date = ? AND m.coingecko_id IS NOT NULL",
                (universe_date, market_date),
            )
        return {r["coingecko_id"] for r in rows}

    def _confirmed_origins(self) -> dict[str, str | None]:
        """Origins already settled by a /coins/{id} lookup. Never re-asked."""
        with get_db() as db:
            rows = db.query(
                "SELECT coingecko_id, origin_platform FROM asset_contract "
                "WHERE resolved_via = 'asset_platform_id'"
            )
        return {r["coingecko_id"]: r["origin_platform"] for r in rows}


__all__ = [
    "MEASURABLE",
    "NATIVE_COIN",
    "NOT_LOOKED_UP",
    "PROVISIONAL",
    "UNSUPPORTED_CHAIN",
    "AssetContractCollector",
    "classify",
    "live_platforms",
    "native_coin_ids",
    "platform_targets",
]
