"""
# WHY: ------------------------------------------------------------------------
# config/protocol_map.yaml attaches a protocol's revenue to a token, and a wrong
# row is worse than no row: it scores another project's fees as this asset's
# fundamentals. The file says "confirm each entry against the source". This is
# that confirmation, made repeatable instead of done once by hand (D-071).
#
# THE RULE, applied to every screened asset with a CoinGecko id:
#   1. IDENTITY. A DefiLlama entry whose `gecko_id` equals the asset's CoinGecko
#      id -- the id the coingecko collector resolved, not the ticker. Ticker
#      collisions are how a micro-cap inherits a large-cap's numbers; an id
#      match is DefiLlama itself saying "this protocol's token is that coin".
#      A child protocol (aave-v3) resolves to its parent (parent#aave).
#   2. SUBSTANCE. Non-zero 30-day fees or revenue. A TVL-only entry would make
#      the whole fundamental block a TVL rank -- a capital snapshot, the
#      weakest metric in the block -- so it is not mapped.
#   3. UNAMBIGUOUS. Two live candidates for one coin (FLOW: the Flow chain and
#      FlowSwap both cite gecko_id "flow") are skipped, not chosen between.
#
# Usage (reads the universe from the configured database, read-only):
#     python scripts/verify_protocol_map.py --check   # exit 1 on a bad entry
#     python scripts/verify_protocol_map.py --write   # regenerate the map
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import yaml  # noqa: E402

from src.collectors.defillama import (  # noqa: E402
    PROTOCOL_MAP_PATH,
    load_protocol_map,
    sum_overview,
)
from src.config import get_config  # noqa: E402
from src.db.connection import get_db  # noqa: E402

HEADER = """\
# base_asset -> DefiLlama protocol slug, or "parent#<slug>" for a parent
# protocol whose children are summed (D-071).
#
# GENERATED AND VERIFIED by scripts/verify_protocol_map.py. Every row passed:
#   * identity  -- the DefiLlama entry's gecko_id equals this asset's CoinGecko
#                  id (not its ticker), and a child resolves to its parent;
#   * substance -- non-zero 30-day fees or revenue on DefiLlama;
#   * no rival  -- no second live DefiLlama entry claims the same coin.
# The trailing comment is the evidence as read on the date shown.
#
# Hand edits are allowed but must pass `--check`. Never guess a mapping:
# another protocol's revenue scores highly, while a missing one honestly
# scores None and has its weight redistributed.
"""


def fetch_sources(base: str, user_agent: str) -> dict[str, Any]:
    """The four DefiLlama payloads the rule needs."""
    with httpx.Client(base_url=base, timeout=120, headers={"User-Agent": user_agent}) as c:

        def get(path: str, **params: Any) -> Any:
            response = c.get(path, params=params or None)
            response.raise_for_status()
            return response.json()

        return {
            "protocols": get("/protocols"),
            "parents": get("/lite/protocols2", b=2).get("parentProtocols") or [],
            "fees": get("/overview/fees", dataType="dailyFees"),
            "revenue": get("/overview/fees", dataType="dailyRevenue"),
        }


def screened_gecko_ids() -> dict[str, str]:
    """base_asset -> coingecko_id for the newest screened universe."""
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


def candidates(
    sources: dict[str, Any],
) -> tuple[dict[str, set[str]], dict[str, dict[str, Any]]]:
    """gecko_id -> map values claiming it, and each value's evidence."""
    fees_slug = {i["slug"]: i for i in sources["fees"].get("protocols", []) if i.get("slug")}
    rev_slug = {i["slug"]: i for i in sources["revenue"].get("protocols", []) if i.get("slug")}
    fees_parent: dict[str, list[dict]] = defaultdict(list)
    rev_parent: dict[str, list[dict]] = defaultdict(list)
    for item in sources["fees"].get("protocols", []):
        if item.get("parentProtocol"):
            fees_parent[item["parentProtocol"]].append(item)
    for item in sources["revenue"].get("protocols", []):
        if item.get("parentProtocol"):
            rev_parent[item["parentProtocol"]].append(item)

    claims: dict[str, set[str]] = defaultdict(set)
    evidence: dict[str, dict[str, Any]] = {}
    for parent in sources["parents"]:
        pid = parent.get("id")
        if not pid:
            continue
        fees = sum_overview(fees_parent.get(pid, [])) or {}
        rev = sum_overview(rev_parent.get(pid, [])) or {}
        evidence[pid] = {
            "name": parent.get("name"),
            "gecko_id": parent.get("gecko_id"),
            "fees30d": fees.get("total30d"),
            "rev30d": rev.get("total30d"),
        }
        if parent.get("gecko_id"):
            claims[parent["gecko_id"]].add(pid)
    for p in sources["protocols"]:
        slug = p.get("slug")
        if not slug:
            continue
        evidence.setdefault(
            slug,
            {
                "name": p.get("name"),
                "gecko_id": p.get("gecko_id"),
                "fees30d": (fees_slug.get(slug) or {}).get("total30d"),
                "rev30d": (rev_slug.get(slug) or {}).get("total30d"),
                "parent": p.get("parentProtocol"),
            },
        )
        if p.get("gecko_id"):
            # A child that names a coin names its parent's coin: the parent is
            # what the token is the token of.
            claims[p["gecko_id"]].add(p.get("parentProtocol") or slug)
    return claims, evidence


def live(e: dict[str, Any]) -> bool:
    return (e.get("fees30d") or 0) > 0 or (e.get("rev30d") or 0) > 0


def build(
    assets: dict[str, str], claims: dict[str, set[str]], evidence: dict[str, dict[str, Any]]
) -> tuple[dict[str, str], list[str]]:
    accepted: dict[str, str] = {}
    skipped: list[str] = []
    for asset, gecko in sorted(assets.items()):
        keys = claims.get(gecko, set())
        good = sorted(k for k in keys if live(evidence.get(k, {})))
        if len(good) == 1:
            accepted[asset] = good[0]
        elif len(good) > 1:
            skipped.append(f"{asset}: ambiguous {good}")
        elif keys:
            skipped.append(f"{asset}: no fees or revenue on {sorted(keys)}")
    return accepted, skipped


def check(
    current: dict[str, str],
    assets: dict[str, str],
    claims: dict[str, set[str]],
    evidence: dict[str, dict[str, Any]],
) -> list[str]:
    """Problems with the map as it stands. Empty means every row verifies."""
    problems: list[str] = []
    for asset, value in sorted(current.items()):
        e = evidence.get(value)
        if e is None:
            problems.append(f"{asset}: {value} is not a DefiLlama protocol or parent id")
            continue
        gecko = assets.get(asset)
        if gecko is None:
            continue  # not screened today: nothing reads the row, nothing to judge
        if value not in claims.get(gecko, set()) and e.get("parent") in claims.get(gecko, set()):
            problems.append(f"{asset}: {value} is a child of {e['parent']}; map the parent")
        elif value not in claims.get(gecko, set()):
            problems.append(
                f"{asset}: {value} claims gecko_id {e.get('gecko_id')!r}, the asset is {gecko!r}"
            )
        elif not live(e):
            problems.append(f"{asset}: {value} reports no fees or revenue")
    return problems


def write(accepted: dict[str, str], evidence: dict[str, dict[str, Any]], path: Path) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [HEADER, "protocols:"]
    for asset in sorted(accepted):
        value = accepted[asset]
        e = evidence[value]
        fees = f"${e['fees30d']:,.0f}" if e.get("fees30d") is not None else "n/a"
        rev = f"${e['rev30d']:,.0f}" if e.get("rev30d") is not None else "n/a"
        lines.append(
            f'  "{asset}": "{value}"  # {e.get("name")}; gecko_id={e.get("gecko_id")}; '
            f"fees30d={fees} rev30d={rev} ({today})"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # Round-trip: the file must load back to exactly what was decided.
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))["protocols"]
    if {str(k).upper(): v for k, v in loaded.items()} != accepted:
        raise RuntimeError("protocol map did not round-trip; the file is wrong")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify or regenerate config/protocol_map.yaml")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="exit 1 if any row fails the rule")
    mode.add_argument("--write", action="store_true", help="regenerate the map from the rule")
    args = parser.parse_args()

    cfg = get_config()
    sources = fetch_sources(cfg.settings.endpoints["defillama"], cfg.settings.http.user_agent)
    assets = screened_gecko_ids()
    claims, evidence = candidates(sources)

    if args.check:
        problems = check(load_protocol_map(cfg.repo_root), assets, claims, evidence)
        for problem in problems:
            print("FAIL", problem)
        print(f"{len(problems)} problem(s)")
        return 1 if problems else 0

    accepted, skipped = build(assets, claims, evidence)
    write(accepted, evidence, cfg.repo_root / PROTOCOL_MAP_PATH)
    for line in skipped:
        print("skip", line)
    print(f"wrote {len(accepted)} verified mappings; skipped {len(skipped)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
