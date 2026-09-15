"""
# WHY: ------------------------------------------------------------------------
# Assembles the daily screen: load inputs, decide which checks can run, apply
# Layer 1, then rank the survivors with Layer 2.
#
# Two responsibilities live here rather than inside the checks themselves:
#
#   1. FRESHNESS. Before anything is screened, assert the newest data is inside
#      max_data_age_hours. A confident dashboard built on three-day-old prices
#      is the worst output this system can produce, because nothing about it
#      looks wrong.
#
#   2. SOURCE COVERAGE (DECISIONS.md D-007). A check whose input is missing for
#      ONE asset fails that asset. A check whose input is missing for the WHOLE
#      universe is our outage, not the market's, and is disabled for the run
#      and reported loudly. Coverage is a run-level property, so it cannot be
#      computed inside a per-asset check -- hence it is computed once, here,
#      and handed to the screener.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

from typing import Any

from src.config import get_config
from src.db.connection import Database, get_db
from src.db.writes import json_dump, upsert
from src.events.features import compute_all
from src.logging_setup import get_logger
from src.ops.doctor import FRESHNESS_TARGETS
from src.screening.layer1_kill import AssetSnapshot, Layer1Result, Layer1Screener
from src.timeutil import add_days, age_hours, today_utc, utc_now_iso

log = get_logger("screening.pipeline")

#: Which checks can be switched off by a source outage, and the input each one
#: needs. L1_NO_MCAP is absent deliberately: it IS the availability check, and
#: disabling it when market data is missing would defeat its purpose.
COVERAGE_INPUTS: dict[str, str] = {
    # A native coin counts: its holder check resolves to not-applicable.
    "L1_HOLDER_CONC": "holder_check_resolvable",
    # A listing on file is not an unlock schedule (D-038).
    "L1_UNLOCK": "has_unlock_record",
    "L1_MCAP_LIQ": "liquidations_24h_usd",
    "L1_PERP_SPOT": "has_spot_pair",
    "L1_OI_MCAP": "open_interest_usd",
}


class StaleDataError(RuntimeError):
    """Raised when the newest data is too old to screen on."""


def assert_fresh(db: Database, run_date: str) -> None:
    """Refuse to screen on stale data. Fails the CI job loudly."""
    cfg = get_config()
    limit = cfg.thresholds.collectors.max_data_age_hours
    for table, column, where in FRESHNESS_TARGETS:
        newest = db.scalar(f"SELECT MAX({column}) FROM {table} WHERE {where}")
        if not newest:
            raise StaleDataError(f"{table} is empty: nothing to screen")
        hours = age_hours(newest)
        if hours > limit:
            raise StaleDataError(
                f"{table} newest row is {hours:.1f}h old, over the {limit:g}h limit. "
                "Refusing to publish a screen built on stale data."
            )


def load_snapshots(db: Database, run_date: str) -> list[AssetSnapshot]:
    """Assemble one AssetSnapshot per tradeable perp, in a handful of queries.

    Batched deliberately: Turso meters row reads, and a per-asset query across
    500 assets is 500 round trips for data that fits in a few indexed scans.
    """
    universe_date = db.scalar(
        "SELECT MAX(snapshot_date) FROM universe_snapshot "
        "WHERE snapshot_date <= ? AND exchange = 'binance'",
        (run_date,),
    )
    if not universe_date:
        return []

    universe = db.query(
        "SELECT symbol, base_asset, onboard_date, price_multiplier "
        "FROM universe_snapshot WHERE snapshot_date = ? AND exchange = 'binance' "
        "AND status = 'TRADING'",
        (universe_date,),
    )
    if not universe:
        return []
    universe = _one_contract_per_asset(universe)

    market = _index_by_asset(db, "market_snapshot", run_date)
    spot = _index_by_asset(db, "spot_snapshot", run_date, key="base_asset")
    liquidations = _index_by_asset(db, "liquidation_snapshot", run_date, key="base_asset")
    holders = _latest_holders(db, run_date)
    derivatives = _latest_derivatives(db, run_date)
    assets = sorted({row["base_asset"] for row in universe})
    events = compute_all(db, assets, run_date)

    snapshots: list[AssetSnapshot] = []
    for row in universe:
        asset = row["base_asset"]
        m = market.get(asset, {})
        d = derivatives.get(row["symbol"], {})
        s = spot.get(asset, {})
        liq = liquidations.get(asset, {})
        ev = events.get(asset)
        h = holders.get(asset, {})

        market_cap = m.get("market_cap_usd")
        change_pct = m.get("price_change_24h_pct")
        # Market-cap change implied by the 24h move, for the liquidation ratio.
        mcap_change = None
        if market_cap is not None and change_pct is not None:
            previous = market_cap / (1.0 + change_pct / 100.0) if change_pct != -100 else 0.0
            mcap_change = market_cap - previous

        snapshots.append(
            AssetSnapshot(
                base_asset=asset,
                market_cap_usd=market_cap,
                circulating_supply=m.get("circulating_supply"),
                total_supply=m.get("total_supply"),
                price_change_24h_pct=change_pct,
                open_interest_usd=d.get("open_interest_usd"),
                perp_volume_24h_usd=d.get("volume_24h_usd"),
                spot_volume_24h_usd=s.get("volume_24h_usd"),
                has_spot_pair=bool(s),
                contract_age_days=_age_days(row.get("onboard_date"), run_date),
                top10_holder_share=h.get("top10_share"),
                holder_data_quality=h.get("data_quality") or "unavailable",
                holder_applicability=h.get("applicability"),
                days_to_next_major_unlock=ev.days_to_next_major_unlock if ev else None,
                next_unlock_pct_circulating=ev.next_unlock_pct_circulating if ev else None,
                next_unlock_recipient_type=ev.next_unlock_recipient_type if ev else None,
                has_event_data=bool(ev and ev.has_event_data),
                has_unlock_record=bool(ev and ev.has_unlock_record),
                liquidations_24h_usd=liq.get("liq_total_usd_24h"),
                market_cap_change_24h_usd=mcap_change,
            )
        )
    return snapshots


def _one_contract_per_asset(universe: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One contract per base asset, preferring the unmultiplied one.

    Universe snapshots written before D-046 can hold two contracts under one
    base (BOBUSDT and 1000000BOBUSDT were both BOB). Every lookup below is
    keyed on base_asset, so the second would silently overwrite the first.
    """
    chosen: dict[str, dict[str, Any]] = {}
    for row in universe:
        asset = row["base_asset"]
        current = chosen.get(asset)
        if current is None:
            chosen[asset] = row
            continue
        keep = (
            row
            if (row.get("price_multiplier") or 1) < (current.get("price_multiplier") or 1)
            else current
        )
        drop = current if keep is row else row
        log.warning(
            "duplicate_base_asset",
            asset=asset,
            kept=keep["symbol"],
            dropped=drop["symbol"],
            rule="the unmultiplied contract is kept (D-046)",
        )
        chosen[asset] = keep
    return list(chosen.values())


def _latest_holders(db: Database, run_date: str) -> dict[str, dict[str, Any]]:
    # Holder snapshots refresh in rolling batches on their own workflow, so the
    # newest date in the table covers only the latest batch. Read the newest
    # row PER ASSET inside the allowed age instead; older than that, the asset
    # is unmeasured rather than judged on a stale reading.
    max_age = get_config().thresholds.layer1.holder_snapshot_max_age_days
    rows = db.query(
        "SELECT h.* FROM holder_snapshot h "
        "JOIN (SELECT base_asset, MAX(snapshot_date) AS latest FROM holder_snapshot "
        "      WHERE snapshot_date <= ? AND snapshot_date >= ? AND applicability IS NOT NULL "
        "      GROUP BY base_asset) newest "
        "ON newest.base_asset = h.base_asset AND newest.latest = h.snapshot_date",
        (run_date, add_days(run_date, -max_age)),
    )
    return {row["base_asset"]: row for row in rows}


def _index_by_asset(
    db: Database, table: str, run_date: str, key: str = "base_asset"
) -> dict[str, dict[str, Any]]:
    """Newest row per asset in a daily table, at or before run_date."""
    snapshot_date = db.scalar(
        f"SELECT MAX(snapshot_date) FROM {table} WHERE snapshot_date <= ?", (run_date,)
    )
    if not snapshot_date:
        return {}
    rows = db.query(f"SELECT * FROM {table} WHERE snapshot_date = ?", (snapshot_date,))
    return {row[key]: row for row in rows if row.get(key)}


def _latest_derivatives(db: Database, run_date: str) -> dict[str, dict[str, Any]]:
    """Most recent derivatives row per symbol, on or before run_date."""
    # Binance's newest timestamp, not the table's. Hyperliquid writes this table
    # hourly, and its newer ts would match no Binance row at all -- every OI and
    # perp-volume input would read as missing (D-050).
    latest_ts = db.scalar(
        "SELECT MAX(ts_utc) FROM derivatives_snapshot WHERE ts_utc <= ? AND exchange = 'binance'",
        (f"{run_date}T23:59:59Z",),
    )
    if not latest_ts:
        return {}
    rows = db.query(
        "SELECT * FROM derivatives_snapshot WHERE ts_utc = ? AND exchange = 'binance'",
        (latest_ts,),
    )
    return {row["symbol"]: row for row in rows}


def _age_days(onboard_date: str | None, run_date: str) -> int | None:
    if not onboard_date:
        return None
    from src.timeutil import days_between

    return days_between(onboard_date[:10], run_date)


def compute_dark_checks(snapshots: list[AssetSnapshot]) -> tuple[frozenset[str], dict[str, float]]:
    """Decide which checks are dark because their SOURCE is unavailable.

    Returns (dark check ids, coverage per check). Coverage is the share of the
    universe for which the check's input is present. Below the configured floor
    the check is disabled for the run rather than failing every asset -- see
    DECISIONS.md D-007.
    """
    if not snapshots:
        return frozenset(), {}

    floor = get_config().thresholds.layer1.min_source_coverage
    total = len(snapshots)
    coverage: dict[str, float] = {}
    dark: set[str] = set()

    for check_id, field in COVERAGE_INPUTS.items():
        present = sum(1 for s in snapshots if _has_value(getattr(s, field, None)))
        share = present / total
        coverage[check_id] = round(share, 4)
        if share < floor:
            dark.add(check_id)
            log.warning(
                "check_source_unavailable",
                check=check_id,
                coverage=f"{share:.1%}",
                floor=f"{floor:.0%}",
                effect="check disabled for this run and reported as dark",
                rationale="an outage of ours is not a judgement about the market (D-007)",
            )
    return frozenset(dark), coverage


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return True


def run_layer1(
    db: Database, run_date: str, snapshots: list[AssetSnapshot]
) -> tuple[list[Layer1Result], dict[str, float]]:
    """Screen every asset and persist the results."""
    dark, coverage = compute_dark_checks(snapshots)
    screener = Layer1Screener(dark_checks=dark)
    results = [screener.run(snapshot, run_date) for snapshot in snapshots]

    fetched_at = utc_now_iso()
    upsert(
        db,
        "layer1_result",
        [
            {
                "run_date": run_date,
                "base_asset": r.base_asset,
                "passed": 1 if r.passed else 0,
                "failed_checks": json_dump(r.failed_checks),
                "check_values": json_dump(r.check_values()),
                "fetched_at_utc": fetched_at,
            }
            for r in results
        ],
    )

    survivors = sum(1 for r in results if r.passed)
    rate = survivors / len(results) if results else 0.0
    log.info(
        "layer1_complete",
        universe=len(results),
        survivors=survivors,
        survival_rate=f"{rate:.1%}",
        dark_checks=sorted(dark),
    )
    # The spec's acceptance band. Outside it, thresholds need REVIEW -- which
    # means a DECISIONS.md entry, not a quiet edit to make the number look nice.
    if results and not 0.20 <= rate <= 0.50:
        log.warning(
            "survival_rate_outside_expected_band",
            rate=f"{rate:.1%}",
            expected="20% to 50%",
            action="review thresholds and record the reasoning in DECISIONS.md",
        )
    return results, coverage


def run_screen(
    run_date: str | None = None,
    enforce_freshness: bool = True,
    layer3: bool = True,
) -> dict[str, Any]:
    """The daily screen: L1 then L2. Returns a summary for the CLI."""
    date = run_date or today_utc()
    with get_db() as db:
        if enforce_freshness:
            assert_fresh(db, date)

        snapshots = load_snapshots(db, date)
        if not snapshots:
            raise RuntimeError(
                f"no universe rows on or before {date}. Run `collect daily` first."
            )

        results, coverage = run_layer1(db, date, snapshots)
        survivors = [r.base_asset for r in results if r.passed]

        from src.screening.layer2_score import run_layer2

        ranked = run_layer2(db, date, survivors)

        # Layer 3 runs on the ranked head only, and ONLY after the ranking
        # exists. It is advisory: it annotates and can veto, and there is no
        # path by which it reorders or promotes anything. Running it here means
        # the journal captures the structural read that was live on the day,
        # rather than one re-derived later from a different chart.
        from src.screening.layer3_structure import run_layer3

        layer3_rows: list[dict[str, Any]] = []
        if layer3 and ranked:
            top_n = get_config().thresholds.journal.top_n_to_journal
            layer3_rows = run_layer3(
                db, date, [r["base_asset"] for r in ranked[:top_n]]
            )

    report_n = get_config().thresholds.journal.report_top_n
    return {
        "run_date": date,
        "universe": len(results),
        "survivors": len(survivors),
        "survival_rate": len(survivors) / len(results) if results else 0.0,
        "ranked": len(ranked),
        "coverage": coverage,
        "dark_checks": sorted({c for r in results for c in r.dark_checks}),
        "layer3_analysed": len(layer3_rows),
        "layer3_setups": sum(r["setup_detected"] for r in layer3_rows),
        "layer3_flagged": sum(1 for r in layer3_rows if r["risk_flags"] not in (None, "[]")),
        "top": [
            {"rank": row["rank"], "base_asset": row["base_asset"], "total_score": row["total_score"]}
            for row in ranked[:report_n]
        ],
    }


__all__ = [
    "COVERAGE_INPUTS",
    "StaleDataError",
    "assert_fresh",
    "compute_dark_checks",
    "load_snapshots",
    "run_layer1",
    "run_screen",
]
