"""
# WHY: ------------------------------------------------------------------------
# config/sectors.yaml says "never guess a mapping". This is how an unmapped
# screened asset gets a sector from a SOURCE instead (D-073), repeatably.
#
# SOURCE: CoinGecko's own category membership, read in bulk from
# /coins/markets?category=<id> (one call per 250 coins per category, ~45 calls,
# not one per coin). An asset takes the FIRST sector in PRECEDENCE whose
# categories it belongs to: specific themes before generic platforms, so a
# memecoin on its own chain is a memecoin and an oracle is infrastructure.
#
# Only the eleven curated sectors are targets. A category with no sector here
# (Fan Token, SocialFi, Education) leaves the asset unclassified -- a new
# sector needs min_members_for_index survivors to ever produce an index, and
# none of those came close on 2026-09-19.
#
# OVERRIDES pick a DIFFERENT CoinGecko-listed category for an asset whose
# precedence result misfiles it; each carries its reason, and each target is
# a category CoinGecko itself lists for the coin.
#
# Usage:
#     python scripts/propose_sectors.py                 # print proposals
#     python scripts/propose_sectors.py --write         # append to sectors.yaml
#     --members-cache FILE   reuse a saved membership fetch (JSON)
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import yaml  # noqa: E402

from src.config import CONFIG_DIR, get_config  # noqa: E402
from src.db.connection import get_db  # noqa: E402

#: sector -> CoinGecko category ids, most specific first. First match wins.
PRECEDENCE: list[tuple[str, frozenset[str]]] = [
    ("memecoin", frozenset({"meme-token"})),
    ("privacy", frozenset({"privacy-coins"})),
    # CEX tokens only: CoinGecko's "exchange-based-tokens" also holds DEX
    # tokens (BNT, ZRX), which are DeFi here.
    ("exchange", frozenset({"centralized-exchange-token-cex"})),
    # Oracles before RWA and DeFi: TRB, RED and UMA carry both tags.
    ("infrastructure", frozenset({"oracle"})),
    ("rwa", frozenset({"real-world-assets-rwa"})),
    ("depin", frozenset({"depin", "internet-of-things-iot", "storage"})),
    ("gaming", frozenset({"gaming", "metaverse"})),
    ("ai", frozenset({"artificial-intelligence", "ai-agents"})),
    ("defi", frozenset({
        "decentralized-finance-defi", "decentralized-exchange", "lending-borrowing",
        "decentralized-perpetuals", "liquid-staking",
    })),
    ("l2", frozenset({"layer-2"})),
    # An explicit Layer 1 tag beats the broad "Infrastructure" tag (STABLE, INIT).
    ("l1", frozenset({"layer-1"})),
    ("infrastructure", frozenset({"interoperability", "infrastructure", "data-availability"})),
    ("l1", frozenset({"smart-contract-platform", "bitcoin-fork", "payment-solutions"})),
]

#: Pages of 250 fetched per category, by market cap. The screened universe sits
#: well inside these depths.
CATEGORY_PAGES = {
    "meme-token": 4, "privacy-coins": 1, "centralized-exchange-token-cex": 1, "oracle": 1,
    "real-world-assets-rwa": 3, "depin": 3, "internet-of-things-iot": 1, "storage": 1,
    "gaming": 3, "metaverse": 2, "artificial-intelligence": 4, "ai-agents": 2,
    "decentralized-finance-defi": 4, "decentralized-exchange": 2, "lending-borrowing": 2,
    "decentralized-perpetuals": 1, "liquid-staking": 1, "layer-2": 2, "layer-1": 2,
    "interoperability": 2, "infrastructure": 3, "data-availability": 1,
    "smart-contract-platform": 2, "bitcoin-fork": 1, "payment-solutions": 2,
}

#: asset -> (sector, reason). The sector's category must be one CoinGecko lists
#: for the coin; the override only chooses between CoinGecko's own labels.
OVERRIDES: dict[str, tuple[str, str]] = {
    "RVN": ("l1", "PoW base chain; its RWA tag is for the asset-issuance feature"),
    "ASTR": ("l1", "Polkadot parachain beside DOT/KSM; the L2 tag is Astar zkEVM"),
    "LUNC": ("l1", "Terra Classic's native chain token; the DeFi tag is historical"),
    "ONE": ("l1", "Harmony's base-layer token; the gaming tag is ecosystem"),
}


def fetch_members(pause: float) -> dict[str, list[str]]:
    """category id -> CoinGecko coin ids, from /coins/markets."""
    base = get_config().settings.endpoints["coingecko"]
    out: dict[str, list[str]] = {}
    with httpx.Client(base_url=base, timeout=40) as client:
        for category, pages in CATEGORY_PAGES.items():
            for page in range(1, pages + 1):
                for _attempt in range(8):
                    r = client.get(
                        "/coins/markets",
                        params={
                            "vs_currency": "usd", "category": category,
                            "order": "market_cap_desc", "per_page": 250, "page": page,
                        },
                    )
                    if r.status_code != 429:
                        break
                    time.sleep(45)
                r.raise_for_status()
                rows = r.json()
                out.setdefault(category, []).extend(c["id"] for c in rows)
                time.sleep(pause)
                if len(rows) < 250:
                    break
    return out


def screened() -> dict[str, str]:
    """base_asset -> coingecko_id for the newest screen."""
    with get_db() as db:
        run_date = db.scalar("SELECT MAX(run_date) FROM layer1_result")
        market = db.scalar(
            "SELECT MAX(snapshot_date) FROM market_snapshot WHERE snapshot_date <= ?", (run_date,)
        )
        rows = db.query(
            "SELECT l.base_asset, m.coingecko_id FROM layer1_result l "
            "JOIN market_snapshot m ON m.base_asset = l.base_asset AND m.snapshot_date = ? "
            "WHERE l.run_date = ? AND m.coingecko_id IS NOT NULL",
            (market, run_date),
        )
    return {r["base_asset"]: r["coingecko_id"] for r in rows}


def propose(
    assets: dict[str, str], members: dict[str, list[str]], mapped: set[str]
) -> tuple[dict[str, str], list[str]]:
    by_coin: dict[str, set[str]] = defaultdict(set)
    for category, ids in members.items():
        for coin in ids:
            by_coin[coin].add(category)
    proposals: dict[str, str] = {}
    unclassified: list[str] = []
    for asset, gecko in sorted(assets.items()):
        if asset.upper() in mapped:
            continue
        categories = by_coin.get(gecko, set())
        sector = next((s for s, cats in PRECEDENCE if categories & cats), None)
        if asset in OVERRIDES:
            target, _reason = OVERRIDES[asset]
            allowed = set().union(*(cats for s, cats in PRECEDENCE if s == target))
            if categories & allowed:  # never override onto a label CoinGecko does not give
                sector = target
        if sector:
            proposals[asset] = sector
        else:
            unclassified.append(asset)
    return proposals, unclassified


def append_to_yaml(path: Path, proposals: dict[str, str]) -> None:
    """Add each ticker under its sector with a dated provenance comment.

    Text insertion, not a YAML round-trip, so the file's comments survive.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = path.read_text(encoding="utf-8").splitlines()
    by_sector: dict[str, list[str]] = defaultdict(list)
    for asset, sector in sorted(proposals.items()):
        by_sector[sector].append(asset)
    for sector, assets in by_sector.items():
        start = lines.index(f"  {sector}:")
        end = start + 1
        while end < len(lines) and lines[end].startswith("    "):
            end += 1
        block = [f"    # From CoinGecko categories, {today} (D-073; scripts/propose_sectors.py)"]
        for asset in assets:
            note = f"  # override: {OVERRIDES[asset][1]}" if asset in OVERRIDES else ""
            block.append(f"    - {json.dumps(asset, ensure_ascii=False)}{note}")
        lines[end:end] = block
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    placed = {str(t).upper() for ts in data["sectors"].values() for t in ts}
    missing = {a.upper() for a in proposals} - placed
    if missing:
        raise RuntimeError(f"not written: {sorted(missing)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Propose sectors from CoinGecko categories")
    parser.add_argument("--write", action="store_true", help="append proposals to sectors.yaml")
    parser.add_argument("--members-cache", type=Path, help="saved {category: [coin ids]} JSON")
    parser.add_argument("--pause", type=float, default=7.0, help="seconds between calls")
    args = parser.parse_args()

    if args.members_cache and args.members_cache.exists():
        members = json.loads(args.members_cache.read_text(encoding="utf-8"))
    else:
        members = fetch_members(args.pause)
        if args.members_cache:
            args.members_cache.write_text(json.dumps(members), encoding="utf-8")

    mapped = {t.upper() for ts in get_config().sectors.sectors.values() for t in ts}
    proposals, unclassified = propose(screened(), members, mapped)
    for asset, sector in sorted(proposals.items(), key=lambda kv: (kv[1], kv[0])):
        note = f"   (override: {OVERRIDES[asset][1]})" if asset in OVERRIDES else ""
        print(f"{sector:15s} {asset}{note}")
    print(f"\n{len(proposals)} proposed; unclassified (no category in the taxonomy): {unclassified}")
    if args.write:
        append_to_yaml(CONFIG_DIR / "sectors.yaml", proposals)
        print("written to config/sectors.yaml")
    return 0


if __name__ == "__main__":
    sys.exit(main())
