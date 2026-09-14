"""
# WHY: ------------------------------------------------------------------------
# Top-holder concentration, the input to L1_HOLDER_CONC. The second half of R1.
#
# The check exists for RAVE (9 wallets held ~95% of supply, 3 of them the
# team's) and TRB (~20 whales, ~95%): a token whose price one party sets.
# Source: GoPlus token_security -- free, keyless, 40+ EVM chains plus Solana,
# one contract per call.
#
# THE TRAP, measured on 2026-09-13 rather than assumed. A raw sum of the top-10
# balances fails healthy tokens, because the largest "holders" are often not
# holders at all:
#
#     CAKE  0x...dead, burned            92.6%    raw top-10 96%
#     AERO  VotingEscrow (veAERO)        50.0%    raw top-10 67% -> FAILS at 0.60
#     AAVE  Staked AAVE (safety module)  15.5%
#
# GoPlus's `tag` was EMPTY on all 40 holders pulled, so filtering on the labels
# its documentation describes excludes nothing. What does work:
#
#   * burn addresses, from config/excluded_addresses.yaml
#   * GoPlus is_locked = 1
#   * the token's own DEX pairs, from GoPlus dex[].pair
#   * addresses curated in config/excluded_addresses.yaml (exchange wallets)
#   * a contract whose NAME marks pooled or escrowed balances. GoPlus gives no
#     names, so they come from Blockscout, which reports the implementation
#     behind a proxy: 'ATokenWithDelegationInstance' (lenders' deposits) is
#     excluded; 'Safe' (a multisig) and 'AaveEcosystemReserveV2' (a treasury)
#     are NOT -- those are one party, which is what the check looks for.
#
# The excluded share leaves the denominator too: if half the supply is in
# escrow, 30% of supply held by the top holders is 60% of what can trade.
# Every exclusion is recorded with its reason in holders_json, and the raw sum
# is stored beside the effective one, so the gap stays auditable.
#
# LIMITS, recorded rather than hidden:
#   * Only the top 10 are visible. After exclusions, fewer than ten real
#     holders remain, so holders 11+ are missing from the numerator.
#   * Solana returns TOKEN ACCOUNTS, not owners, and no DEX pairs. One owner can
#     hold several accounts and a pool vault is an account too, so a Solana
#     reading is approximate in both directions (data_quality says so).
#   * Chains without a Blockscout instance (BSC among them) get no contract
#     names, so pooled contracts there count as holders -- conservative, and
#     flagged names_unavailable.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from src.collectors.base import BaseCollector
from src.collectors.contracts import MEASURABLE
from src.db.connection import get_db
from src.db.writes import json_dump, upsert
from src.timeutil import format_day, utc_now_iso

#: Chains whose addresses are case-sensitive (base58). EVM hex is not.
CASE_SENSITIVE_CHAINS = frozenset({"solana", "tron"})

#: GoPlus signals rate limiting in the response BODY, with HTTP 200.
GOPLUS_RATE_LIMITED = 4029


def normalise_address(address: Any, chain: str) -> str:
    text = str(address or "").strip()
    return text if chain in CASE_SENSITIVE_CHAINS else text.lower()


def load_exclusions(path: Path) -> dict[str, Any]:
    """Burn addresses and curated per-chain exclusions from the R5 file."""
    if not path.exists():
        return {"burn": frozenset(), "curated": {}}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    burn = frozenset(str(a).strip().lower() for a in data.get("burn_addresses") or [])
    curated: dict[str, dict[str, str]] = {}
    for chain, entries in (data.get("addresses") or {}).items():
        for entry in entries or []:
            address = normalise_address(entry.get("address"), str(chain))
            if address:
                curated.setdefault(str(chain), {})[address] = str(entry.get("category") or "curated")
    return {"burn": burn, "curated": curated}


def _share(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def measure(
    entry: dict[str, Any],
    chain: str,
    labels: dict[tuple[str, str], dict[str, Any]],
    exclusions: dict[str, Any],
    name_patterns: list[str],
    names_available: bool,
) -> dict[str, Any]:
    """Raw and effective top-10 share for one GoPlus token_security entry.

    Shares are fractions of total supply; GoPlus uses 1 = 100%. `labels` maps
    (chain, address) to {"name", "implementation_name", "is_contract"}.
    """
    holders = entry.get("holders") or []
    pairs = {normalise_address(d.get("pair"), chain) for d in entry.get("dex") or [] if d.get("pair")}
    curated = exclusions.get("curated", {}).get(chain, {})
    burn = exclusions.get("burn", frozenset())
    patterns = [p.lower() for p in name_patterns]

    listed: list[dict[str, Any]] = []
    raw = excluded = 0.0
    kept: list[float] = []
    unnamed_contracts = 0

    for holder in holders:
        address = normalise_address(holder.get("address") or holder.get("token_account"), chain)
        share = _share(holder.get("percent"))
        raw += share
        is_contract = holder.get("is_contract") == 1
        label = labels.get((chain, address)) or {}
        name = label.get("implementation_name") or label.get("name")
        tag = str(holder.get("tag") or "").lower()

        reason: str | None = None
        if address.lower() in burn or "burn" in tag or "black hole" in tag:
            reason = "burn_address"
        elif holder.get("is_locked") == 1:
            reason = "locked"
        elif address and address in pairs:
            reason = "dex_pair"
        elif address in curated:
            reason = f"curated:{curated[address]}"
        elif is_contract and name:
            lowered = str(name).lower()
            match = next((p for p in patterns if p in lowered), None)
            if match:
                reason = f"contract_name:{match}"
        elif is_contract:
            unnamed_contracts += 1

        if reason:
            excluded += share
        else:
            kept.append(share)
        listed.append(
            {
                "address": address,
                "percent": round(share, 6),
                "is_contract": holder.get("is_contract"),
                "is_locked": holder.get("is_locked"),
                "name": name,
                "excluded": reason,
            }
        )

    remaining = 1.0 - excluded
    effective: float | None = None
    top1: float | None = None
    if not holders:
        quality = "no_holders_returned"
    elif remaining <= 0.0 or not kept:
        # Every visible top holder is excluded. There is no tradeable float in
        # view to measure, and inventing 0% would pass the asset on nothing.
        quality = "all_top_holders_excluded"
    else:
        effective = min(1.0, sum(kept) / remaining)
        top1 = min(1.0, max(kept) / remaining)
        if chain == "solana":
            quality = "token_accounts"
        elif unnamed_contracts and not names_available:
            quality = "names_unavailable"
        elif unnamed_contracts:
            quality = "names_partial"
        else:
            quality = "complete"

    return {
        "top10_share": effective,
        "top10_share_raw": min(1.0, raw) if holders else None,
        "top1_share": top1,
        "excluded_share": round(excluded, 6) if holders else None,
        "holders": listed,
        "data_quality": quality,
    }


def _first_result(body: Any) -> dict[str, Any] | None:
    result = body.get("result") if isinstance(body, dict) else None
    if isinstance(result, dict) and result:
        first = next(iter(result.values()))
        return first if isinstance(first, dict) else None
    return None


def _as_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


class HolderCollector(BaseCollector):
    """Writes holder_snapshot for the screened universe, stalest first."""

    name = "holders"
    rate_limit_key = "goplus"
    tier = "C"

    async def fetch(self, as_of: datetime) -> dict[str, Any]:
        settings = self.config.settings
        targets = self._targets()
        measurable = sorted(
            (t for t in targets if t["applicability"] == MEASURABLE),
            key=lambda t: (t.get("last_measured") or "", t["base_asset"]),
        )
        batch = measurable[: settings.holders.max_tokens_per_run]

        payloads: dict[str, dict[str, Any]] = {}
        failures = 0
        rate_limited = 0
        async with self.client(settings.endpoints["goplus"]) as client:
            for target in batch:
                entry, code = await self._read_token(client, target)
                if entry is None:
                    failures += 1
                    rate_limited += int(code == GOPLUS_RATE_LIMITED)
                    continue
                payloads[target["base_asset"]] = entry
        if failures:
            # No row is written for these, so the last good measurement stands
            # until it ages out -- a failed read is not evidence about a token.
            self.warn(
                "goplus_tokens_unavailable",
                count=failures,
                still_rate_limited=rate_limited,
                of=len(batch),
            )

        labels, new_labels = await self._contract_names(batch, payloads)
        return {
            "targets": targets,
            "payloads": payloads,
            "labels": labels,
            "new_labels": new_labels,
            "exclusions": load_exclusions(
                self.config.repo_root / settings.holders.excluded_addresses_file
            ),
            "snapshot_date": format_day(as_of),
            "fetched_at": utc_now_iso(),
        }

    async def _read_token(
        self, client: Any, target: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, Any]:
        # One GoPlus read, returning (entry, last code); entry is None on failure.
        #
        # GoPlus reports rate limiting as HTTP 200 with code 4029, so the base
        # class's HTTP-429 retry never sees it. The first live run lost 136 of
        # 315 reads that way -- and to Layer 1 an unread token looks exactly
        # like one with no holder data. So the body code is retried here, with
        # a doubling wait, and the attempts are bounded.
        cfg = self.config.settings.holders
        chain = target["goplus_chain"]
        path = "/solana/token_security" if chain == "solana" else f"/token_security/{chain}"
        code: Any = None
        for attempt in range(cfg.goplus_rate_limit_retries + 1):
            if attempt:
                wait = cfg.goplus_rate_limit_backoff_seconds * (2 ** (attempt - 1))
                self.log.info(
                    "goplus_rate_limited", asset=target["base_asset"], attempt=attempt, wait_seconds=wait
                )
                await asyncio.sleep(wait)
            try:
                body = await self.request_json(
                    client, "GET", path, params={"contract_addresses": target["contract_address"]}
                )
            except Exception as exc:  # noqa: BLE001 - one token, not the run
                self.log.warning(
                    "goplus_token_unavailable",
                    asset=target["base_asset"],
                    chain=chain,
                    error=str(exc)[:150],
                )
                return None, None
            code = body.get("code") if isinstance(body, dict) else None
            if code == GOPLUS_RATE_LIMITED:
                continue
            entry = _first_result(body)
            if code != 1 or entry is None:
                self.log.warning("goplus_no_result", asset=target["base_asset"], chain=chain, code=code)
                return None, code
            return entry, code
        self.log.warning(
            "goplus_rate_limit_exhausted",
            asset=target["base_asset"],
            chain=chain,
            retries=cfg.goplus_rate_limit_retries,
        )
        return None, code

    def transform(self, raw: dict[str, Any], as_of: datetime) -> list[dict[str, Any]]:
        holders_cfg = self.config.settings.holders
        rows: list[dict[str, Any]] = []
        for target in raw["targets"]:
            asset = target["base_asset"]
            chain = target["goplus_chain"]
            row: dict[str, Any] = {
                "_kind": "holder",
                "snapshot_date": raw["snapshot_date"],
                "base_asset": asset,
                "chain": chain,
                "contract_address": target["contract_address"],
                "applicability": target["applicability"],
                "top10_share": None,
                "top10_share_raw": None,
                "top1_share": None,
                "top50_share": None,
                "excluded_share": None,
                "holder_count": None,
                "holders_json": None,
                "excluded_addresses": None,
                "data_quality": None,
                "source": "asset_contract",
                "fetched_at_utc": raw["fetched_at"],
            }
            if target["applicability"] != MEASURABLE:
                # Written every run: the screen needs to know an asset is
                # native, not merely that no measurement exists.
                rows.append(row)
                continue

            entry = raw["payloads"].get(asset)
            if entry is None:
                continue  # outside this batch, or the read failed

            result = measure(
                entry,
                chain,
                raw["labels"],
                raw["exclusions"],
                holders_cfg.exclude_contract_name_patterns,
                names_available=bool(holders_cfg.blockscout_hosts.get(target.get("platform") or "")),
            )
            row.update(
                top10_share=result["top10_share"],
                top10_share_raw=result["top10_share_raw"],
                top1_share=result["top1_share"],
                excluded_share=result["excluded_share"],
                holder_count=_as_int(entry.get("holder_count")),
                holders_json=json_dump(result["holders"]),
                excluded_addresses=json_dump(
                    [h["address"] for h in result["holders"] if h["excluded"]]
                ),
                data_quality=result["data_quality"],
                source="goplus",
            )
            rows.append(row)

        for (chain, address), label in (raw.get("new_labels") or {}).items():
            rows.append(
                {
                    "_kind": "label",
                    "chain": chain,
                    "address": address,
                    "name": label.get("name"),
                    "implementation_name": label.get("implementation_name"),
                    "is_contract": label.get("is_contract"),
                    "source": "blockscout",
                    "fetched_at_utc": raw["fetched_at"],
                }
            )
        return rows

    def write(self, rows: list[dict[str, Any]]) -> int:
        holders = [_strip(r) for r in rows if r.get("_kind") == "holder"]
        labels = [_strip(r) for r in rows if r.get("_kind") == "label"]
        with get_db() as db:
            written = upsert(db, "holder_snapshot", holders) if holders else 0
            if labels:
                upsert(db, "address_label", labels)
        return written

    # -- contract names -------------------------------------------------------
    async def _contract_names(
        self, batch: list[dict[str, Any]], payloads: dict[str, dict[str, Any]]
    ) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
        holders_cfg = self.config.settings.holders
        cached = self._cached_labels()
        wanted: dict[str, list[tuple[str, str]]] = {}
        seen: set[tuple[str, str]] = set()
        for target in batch:
            entry = payloads.get(target["base_asset"])
            host = holders_cfg.blockscout_hosts.get(target.get("platform") or "")
            if not entry or not host:
                continue
            chain = target["goplus_chain"]
            for holder in entry.get("holders") or []:
                if holder.get("is_contract") != 1:
                    continue
                key = (chain, normalise_address(holder.get("address"), chain))
                if not key[1] or key in cached or key in seen:
                    continue
                seen.add(key)
                wanted.setdefault(host, []).append(key)

        budget = holders_cfg.max_name_lookups_per_run
        new: dict[tuple[str, str], dict[str, Any]] = {}
        failures = 0
        attempted = 0
        for host, keys in wanted.items():
            if attempted >= budget:
                break
            async with self.client(f"https://{host}") as client:
                for key in keys:
                    if attempted >= budget:
                        break
                    attempted += 1
                    try:
                        body = await self.request_json(
                            client, "GET", f"/api/v2/addresses/{key[1]}", limiter_key="blockscout"
                        )
                    except Exception as exc:  # noqa: BLE001 - a name, not the run
                        failures += 1
                        self.log.warning(
                            "contract_name_unavailable", host=host, address=key[1], error=str(exc)[:120]
                        )
                        continue
                    body = body if isinstance(body, dict) else {}
                    implementations = [
                        i.get("name") for i in body.get("implementations") or [] if i.get("name")
                    ]
                    new[key] = {
                        "name": body.get("name"),
                        "implementation_name": implementations[0] if implementations else None,
                        "is_contract": 1 if body.get("is_contract") else 0,
                    }
        if failures:
            self.warn("contract_names_unavailable", count=failures)
        deferred = sum(len(keys) for keys in wanted.values()) - attempted
        if deferred > 0:
            self.log.info("contract_name_lookups_deferred", remaining=deferred)
        return {**cached, **new}, new

    # -- database reads ---------------------------------------------------------
    def _targets(self) -> list[dict[str, Any]]:
        with get_db() as db:
            universe_date = db.scalar(
                "SELECT MAX(snapshot_date) FROM universe_snapshot WHERE exchange = 'binance'"
            )
            market_date = db.scalar("SELECT MAX(snapshot_date) FROM market_snapshot")
            if not universe_date or not market_date:
                raise RuntimeError("no universe or market snapshot yet: run `collect daily` first")
            rows = db.query(
                "SELECT DISTINCT m.base_asset, m.coingecko_id, c.applicability, c.platform, "
                "c.goplus_chain, c.contract_address, "
                "(SELECT MAX(h.snapshot_date) FROM holder_snapshot h "
                " WHERE h.base_asset = m.base_asset AND h.top10_share IS NOT NULL) AS last_measured "
                "FROM market_snapshot m "
                "JOIN universe_snapshot u ON u.base_asset = m.base_asset "
                "AND u.snapshot_date = ? AND u.exchange = 'binance' AND u.status = 'TRADING' "
                "JOIN asset_contract c ON c.coingecko_id = m.coingecko_id "
                "WHERE m.snapshot_date = ?",
                (universe_date, market_date),
            )
        if not rows:
            raise RuntimeError(
                "asset_contract has no rows for the screened universe: run `collect asset_contracts` first"
            )
        return rows

    def _cached_labels(self) -> dict[tuple[str, str], dict[str, Any]]:
        with get_db() as db:
            rows = db.query(
                "SELECT chain, address, name, implementation_name, is_contract FROM address_label"
            )
        return {
            (r["chain"], r["address"]): {
                "name": r["name"],
                "implementation_name": r["implementation_name"],
                "is_contract": r["is_contract"],
            }
            for r in rows
        }


def _strip(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k != "_kind"}


__all__ = ["HolderCollector", "load_exclusions", "measure", "normalise_address"]
