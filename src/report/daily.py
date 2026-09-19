"""
# WHY: ------------------------------------------------------------------------
# The daily markdown report (spec 14).
#
# Its structure is an argument, not a layout. Four of its five sections exist to
# make the system harder to fool yourself with:
#
#   * "Notable disqualifications" shows what the filter REMOVED. Every screener
#     shows winners. Showing the rejects is what surfaces a miscalibrated
#     threshold early -- if a name you know to be sound is being killed every
#     day on the same check, you find out here, and the response is a
#     DECISIONS.md entry, never a quiet loosening of the number.
#
#   * "Dark checks" names any check that was inoperative for the WHOLE run
#     because its source was down. A dark check is not a passing check, and a
#     report that hides the difference is a report that quietly promotes
#     unmeasured assets.
#
#   * "Data quality" carries the completeness and lag figures. A screen run on
#     eight-hour-old derivatives data is a different instrument from one run on
#     fresh data, and the reader must be able to tell which they are holding.
#
#   * The upcoming-events table is the one forward-looking section, and it is
#     restricted to survivors: an unlock on an asset already disqualified is
#     not information the reader can act on.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

import statistics
from pathlib import Path
from typing import Any

from src.config import get_config
from src.db.connection import Database, get_db
from src.db.writes import json_load
from src.logging_setup import get_logger
from src.timeutil import add_days, age_hours, days_between, today_utc, utc_now_iso

log = get_logger("report.daily")

#: Blocks in the order they appear as columns. Abbreviated to keep the table
#: readable in a terminal; the header row spells them out.
BLOCK_COLUMNS = (
    ("score_fundamental", "Fund"),
    ("score_supply", "Supp"),
    ("score_sector", "Sect"),
    ("score_events", "Evnt"),
    ("score_attention", "Attn"),
    ("score_drawdown", "DD"),
)

#: Percentile keys that read as a flag when they sit at an extreme. Derived from
#: what Layer 2 already recorded, so every flag here is traceable to a stored
#: number rather than recomputed with different code.
FLAG_RULES = (
    ("events.overhang_cleared", 100.0, "overhang_cleared"),
    ("events.positive_catalyst", 100.0, "catalyst_30d"),
    ("events.monitoring_tag", 0.0, "monitoring_tag"),
)


def _num(value: Any, places: int = 1) -> str:
    """A number, or an em dash. Never a zero standing in for a missing value."""
    if value is None:
        return "—"
    try:
        return f"{float(value):.{places}f}"
    except (TypeError, ValueError):
        return "—"


def flags_for(percentiles: dict[str, Any]) -> list[str]:
    flags = []
    for key, trigger, label in FLAG_RULES:
        value = percentiles.get(key)
        if value is not None and abs(float(value) - trigger) < 1e-9:
            flags.append(label)
    return flags


def gather(db: Database, run_date: str) -> dict[str, Any]:
    """Everything the report needs, read once.

    Returned as a plain dict so the same payload can feed the markdown report,
    the Telegram summary and the dashboard JSON. Three renderers reading three
    slightly different queries is three chances for the numbers to disagree.
    """
    cfg = get_config()
    l1 = db.query(
        "SELECT base_asset, passed, failed_checks, check_values FROM layer1_result "
        "WHERE run_date = ?",
        (run_date,),
    )
    l2 = db.query(
        "SELECT base_asset, total_score, rank, universe_size, percentiles, "
        "       score_fundamental, score_supply, score_sector, score_events, "
        "       score_attention, score_drawdown "
        "FROM layer2_result WHERE run_date = ? ORDER BY rank",
        (run_date,),
    )

    l3 = db.query(
        "SELECT base_asset, setup_detected, setup_type, invalidation_price, "
        "       risk_flags, depth_2pct_usd, funding_pctile FROM layer3_result "
        "WHERE run_date = ?",
        (run_date,),
    )
    survivors = {r["base_asset"] for r in l1 if r["passed"]}
    failures = [r for r in l1 if not r["passed"]]

    # A check with no verdict for anyone is dark, not passing (D-007).
    dark: list[str] = []
    if l1:
        first = json_load(l1[0]["check_values"], {}) or {}
        for check_id in first:
            unavailable = sum(
                1
                for r in l1
                if (json_load(r["check_values"], {}) or {})
                .get(check_id, {})
                .get("source_unavailable")
            )
            if unavailable == len(l1):
                dark.append(check_id)

    return {
        "run_date": run_date,
        "generated_at_utc": utc_now_iso(),
        "universe": len(l1),
        "survivors": len(survivors),
        "survival_rate": len(survivors) / len(l1) if l1 else 0.0,
        "ranked": l2,
        "layer3": {r["base_asset"]: r for r in l3},
        "failures": failures,
        "dark_checks": sorted(dark),
        "events": _upcoming_events(db, run_date, survivors),
        "quality": data_quality(db, run_date),
        "regime": db.query_one(
            "SELECT regime, fear_greed_value, fear_greed_label, btc_return_30d "
            "FROM market_regime WHERE snapshot_date <= ? ORDER BY snapshot_date DESC LIMIT 1",
            (run_date,),
        ),
        "report_top_n": cfg.thresholds.journal.report_top_n,
    }


def _upcoming_events(db: Database, run_date: str, survivors: set[str]) -> list[dict[str, Any]]:
    """Events in the next 30 days, survivors only, knowable as of `run_date`.

    The `first_seen_utc` filter is not decoration: without it, tomorrow's
    regenerated report for today would include events that were not yet
    published today, and the report would stop being a record of what was
    knowable at the time.
    """
    rows = db.query(
        "SELECT base_asset, event_date_utc, event_type, recipient_type, "
        "       pct_of_circulating, magnitude_usd, confidence "
        "FROM scheduled_event "
        "WHERE event_date_utc BETWEEN ? AND ? AND first_seen_utc <= ? "
        "ORDER BY event_date_utc",
        (run_date, add_days(run_date, 30), f"{run_date}T23:59:59Z"),
    )
    return [r for r in rows if r["base_asset"] in survivors]


def data_quality(db: Database, run_date: str) -> dict[str, Any]:
    """Completeness, freshness and lag. The reader's warning label."""
    collectors = db.query(
        "SELECT collector_name, status, COUNT(*) AS n FROM collector_run "
        "WHERE started_at_utc >= ? GROUP BY collector_name, status",
        (f"{add_days(run_date, -7)}T00:00:00Z",),
    )
    # Three statuses, and conflating any two of them misleads the reader.
    # 'partial' means the collector RAN and degraded gracefully -- an optional
    # source was unconfigured or a page was dropped. Bucketing it with 'failed'
    # reported five working collectors at 0% success and would have sent the
    # reader chasing an outage that did not exist.
    tally: dict[str, dict[str, int]] = {}
    for row in collectors:
        entry = tally.setdefault(
            row["collector_name"], {"success": 0, "partial": 0, "failed": 0}
        )
        key = row["status"] if row["status"] in entry else "failed"
        entry[key] += row["n"]

    lags = [
        r["lag_seconds"]
        for r in db.query(
            "SELECT lag_seconds FROM news_item WHERE fetched_at_utc >= ? AND lag_seconds IS NOT NULL",
            (f"{add_days(run_date, -1)}T00:00:00Z",),
        )
    ]
    trigger = [
        r["lag_seconds"]
        for r in db.query(
            "SELECT lag_seconds FROM trigger_lag WHERE actual_start_utc >= ? "
            "AND lag_seconds IS NOT NULL",
            (f"{add_days(run_date, -7)}T00:00:00Z",),
        )
    ]

    return {
        "collectors": {
            name: {
                **counts,
                # Completion rate: did the collector run at all? A partial run
                # produced data. Reported alongside the partial count, never
                # instead of it, so degradation stays visible.
                "completion_rate": (
                    (counts["success"] + counts["partial"]) / sum(counts.values())
                    if sum(counts.values())
                    else None
                ),
            }
            for name, counts in sorted(tally.items())
        },
        # row_count is a cumulative WRITE tally, not a row census (D-069).
        # The Health page headers it "Writes" for that reason.
        "tables": db.query(
            "SELECT table_name, row_count, last_write_utc FROM table_stats ORDER BY table_name"
        ),
        "news_lag_seconds": _percentiles(lags),
        "trigger_lag_seconds": _percentiles(trigger),
        "coverage": _coverage(db, run_date),
        "blocks": _block_information(db, run_date),
    }


#: The six L2 blocks and the weight each carries, for the informativeness read.
_BLOCK_COLUMNS = (
    ("fundamental", "score_fundamental"),
    ("supply", "score_supply"),
    ("sector", "score_sector"),
    ("events", "score_events"),
    ("attention", "score_attention"),
    ("drawdown", "score_drawdown"),
)


def _block_information(db: Database, run_date: str) -> dict[str, dict[str, Any]]:
    """Did each L2 block actually SEPARATE today's assets, or just occupy weight?

    Coverage answers "was there a row"; this answers the harder question of
    whether the block distinguished one asset from another. The two come apart
    badly and quietly. Today's live run is the example: unlock coverage sits at
    0.4%, the exchange-announcement endpoint is geo-throttled, and the events
    block therefore scores an identical 50.0 for all 207 survivors -- a tenth
    of the total weight contributing a constant.

    A constant changes no ordering, so nothing looks wrong. But the reader
    believes six things were weighed when five were, and the score's apparent
    precision is borrowed from a block that said nothing. Reported rather than
    corrected: renormalising away a zero-variance block would be a threshold
    change, and those are decisions, not silent adjustments.
    """
    weights = get_config().thresholds.layer2_weights.as_dict()
    rows = db.query(
        "SELECT " + ", ".join(column for _, column in _BLOCK_COLUMNS)
        + " FROM layer2_result WHERE run_date = ?",
        (run_date,),
    )
    out: dict[str, dict[str, Any]] = {}
    for block, column in _BLOCK_COLUMNS:
        values = [r[column] for r in rows if r[column] is not None]
        # Rounded before counting: floating noise in the twelfth decimal is
        # not information, and treating it as such would hide exactly the
        # case this function exists to catch.
        distinct = len({round(float(v), 4) for v in values})
        out[block] = {
            "weight": weights.get(block),
            "scored": len(values),
            "of_ranked": len(rows),
            "distinct_values": distinct,
            # One distinct value across every asset separates nobody. Zero
            # means the block was not scored at all.
            "informative": distinct > 1,
        }
    return out


def _percentiles(values: list[float]) -> dict[str, float | None]:
    """p50 and p95. Reported as None when there is nothing to describe."""
    if not values:
        return {"n": 0, "p50": None, "p95": None}
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "p50": round(statistics.median(ordered), 1),
        "p95": round(ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))], 1),
    }


def _coverage(db: Database, run_date: str) -> dict[str, dict[str, Any]]:
    """Share of today's universe present in each input table.

    This is the number that tells the reader whether a check was measured or
    merely not contradicted.
    """
    universe = db.scalar(
        "SELECT COUNT(*) FROM layer1_result WHERE run_date = ?", (run_date,)
    ) or 0
    # The count must be INTERSECTED with today's universe. Counting a table's
    # own rows reported market_snapshot at 230% of the universe, because
    # CoinGecko covers 1,218 assets and only 528 of them have a perp. A
    # coverage figure above 100% is not a rounding artefact, it is the wrong
    # question: what matters is how much of what we SCREEN we can measure.
    scoped = (
        "SELECT COUNT(DISTINCT t.base_asset) FROM {table} t "
        "JOIN layer1_result l ON l.base_asset = t.base_asset AND l.run_date = ? "
        "WHERE {date_clause} AND ({data_clause})"
    )
    # A ROW is not DATA. The DefiLlama collector writes a row for every asset
    # in the universe, with has_fundamentals = 0 where no protocol is mapped:
    # 657 rows on 2026-09-09, of which 13 carried a fundamental. Counting rows
    # reported 100% coverage of a block that was fed for thirteen assets --
    # exactly the kind of number that makes an operator trust a block they
    # should be discounting. Each table therefore declares what "measured"
    # means for it.
    tables = (
        ("market_snapshot", "t.snapshot_date = ?", "t.market_cap_usd IS NOT NULL"),
        ("derivatives_snapshot", "t.ts_utc >= ?", "t.open_interest_usd IS NOT NULL"),
        ("fundamentals_snapshot", "t.snapshot_date = ?", "t.has_fundamentals = 1"),
        ("holder_snapshot", "t.snapshot_date = ?", "t.top10_share IS NOT NULL"),
        (
            "supply_metrics",
            "t.snapshot_date = ?",
            # Net issuance is the only supply metric with a writer (D-072).
            "t.emissions_annual IS NOT NULL",
        ),
        ("attention_snapshot", "t.snapshot_date = ?", "t.social_volume IS NOT NULL"),
    )
    from src.config import get_config
    from src.timeutil import add_days

    # Tables refreshed in rolling batches, with the age window the screen
    # accepts for them. MAX(snapshot_date) covers only the newest batch there.
    rolling = {"holder_snapshot": get_config().thresholds.layer1.holder_snapshot_max_age_days}

    out: dict[str, dict[str, Any]] = {}
    for table, date_clause, data_clause in tables:
        # Count against the snapshot the SCREEN actually used, not against
        # today's calendar date.
        #
        # load_snapshots and load_scoring_frame both resolve
        # MAX(snapshot_date) <= run_date, so a day where collection has not
        # run yet is screened on yesterday's rows -- deliberately. Coverage
        # pinned to `= run_date` then reported 0% for every table while the
        # screen had just ranked 207 assets, which reads as a total collection
        # outage and is the exact "two different zeroes" confusion the
        # dashboard already guards against on the funnel.
        #
        # So: resolve the same date the screen resolved, report it, and say
        # how old it is. A reader can then tell "nothing was collected" from
        # "today's screen ran on yesterday's data", which are different
        # problems with different responses.
        if "ts_utc" in date_clause:
            effective = db.scalar(
                f"SELECT MAX(ts_utc) FROM {table} WHERE ts_utc <= ?",
                (f"{run_date}T23:59:59Z",),
            )
            as_of = (effective or "")[:10] or None
            bound = f"{as_of}T00:00:00Z" if as_of else f"{run_date}T00:00:00Z"
        else:
            as_of = db.scalar(
                f"SELECT MAX(snapshot_date) FROM {table} WHERE snapshot_date <= ?",
                (run_date,),
            )
            bound = as_of or run_date
        params: tuple[Any, ...] = (run_date, bound)
        if table in rolling:
            # Count every asset with a row inside the window the screen itself
            # accepts (pipeline._latest_holders), not only the newest batch.
            date_clause = "t.snapshot_date BETWEEN ? AND ?"
            params = (run_date, add_days(run_date, -rolling[table]), run_date)
        measured = (
            db.scalar(
                scoped.format(table=table, date_clause=date_clause, data_clause=data_clause),
                params,
            )
            or 0
        )
        present = (
            db.scalar(
                scoped.format(table=table, date_clause=date_clause, data_clause="1=1"),
                params,
            )
            or 0
        )
        out[table] = {
            "assets": measured,
            "rows_present": present,
            "of_universe": round(measured / universe, 4) if universe else None,
            #: The snapshot date these counts describe. None when the table
            #: has no row at or before run_date at all.
            "as_of": as_of,
            #: How stale that snapshot is relative to the run. 0 means today.
            "age_days": days_between(as_of, run_date) if as_of else None,
        }
    return out


def render(payload: dict[str, Any]) -> str:
    """The markdown report. Section order follows spec 14."""
    p = payload
    lines: list[str] = [f"# Daily Screen — {p['run_date']}", ""]

    if p["universe"] == 0:
        lines += [
            "No universe rows for this date. Nothing was screened, and this "
            "report is a record of that fact rather than an empty template.",
            "",
        ]
        return "\n".join(lines)

    # -- universe -----------------------------------------------------------
    lines += [
        "## Universe",
        "",
        f"{p['universe']} perps → {p['survivors']} survived L1 "
        f"({p['survival_rate']:.1%})",
        "",
    ]
    regime = p.get("regime")
    if regime:
        lines += [
            f"Regime: **{regime.get('regime') or 'unknown'}** "
            f"(BTC 30d {_num((regime.get('btc_return_30d') or 0) * 100)}%, "
            f"fear/greed {regime.get('fear_greed_value') or '—'} "
            f"{regime.get('fear_greed_label') or ''})".strip(),
            "",
        ]

    if p["dark_checks"]:
        lines += [
            "> **Checks dark this run:** " + ", ".join(p["dark_checks"]) + ".",
            "> Their source returned nothing for the entire universe, so they "
            "did not contribute to any verdict. A dark check is not a passing "
            "check: every survivor below is unmeasured on these.",
            "",
        ]

    # -- ranked table -------------------------------------------------------
    top_n = p["report_top_n"]
    ranked = p["ranked"][:top_n]
    lines += [f"## Top {len(ranked)}", ""]
    if not ranked:
        lines += ["Nothing survived to rank.", ""]
    else:
        header = "| # | Asset | Score | " + " | ".join(label for _, label in BLOCK_COLUMNS)
        lines += [
            header + " | Flags |",
            "|---:|---|---:|" + "---:|" * len(BLOCK_COLUMNS) + "---|",
        ]
        for row in ranked:
            percentiles = json_load(row["percentiles"], {}) or {}
            cells = " | ".join(_num(row[key], 0) for key, _ in BLOCK_COLUMNS)
            lines.append(
                f"| {row['rank']} | {row['base_asset']} | {_num(row['total_score'])} | "
                f"{cells} | {', '.join(flags_for(percentiles)) or '—'} |"
            )
        lines.append("")
        lines += [
            "_A blank block score means that block had no data for the asset. "
            "Its weight was redistributed across the blocks that did, never "
            "counted as zero._",
            "",
        ]

    # -- layer 3 ------------------------------------------------------------
    lines += _layer3_section(p)

    # -- notable disqualifications -----------------------------------------
    lines += ["## Notable disqualifications", ""]
    notable = _notable_failures(p["failures"])
    if not notable:
        lines += ["Nothing was disqualified. Verify the checks are running.", ""]
    else:
        lines += [
            "| Asset | Mcap | Failed | Value | Threshold | Checks failed |",
            "|---|---:|---|---:|---:|---:|",
        ]
        for row in notable:
            mcap = row["market_cap_usd"]
            lines.append(
                f"| {row['base_asset']} | "
                f"{'—' if not mcap else f'${mcap / 1e6:,.0f}M'} | "
                f"{row['check_id']} | {row['value_display']} | "
                f"{_num(row['threshold'], 4)} | {row['failed_count']} |"
            )
        lines.append("")
        counts = _failure_counts(p["failures"])
        lines.append(
            "Disqualifications by check: "
            + ", ".join(f"{check} {n}" for check, n in counts)
            + "."
        )
        lines.append("")

    # -- events -------------------------------------------------------------
    lines += ["## Upcoming events (next 30d, survivors only)", ""]
    if not p["events"]:
        lines += ["No recorded events for survivors in the next 30 days.", ""]
        lines += [
            "_Absence of a recorded event is not absence of an event: the "
            "unlock calendar is a manual fallback and is only as complete as "
            "its last update._",
            "",
        ]
    else:
        lines += [
            "| Asset | Date | Type | Recipient | % Circ | Confidence |",
            "|---|---|---|---|---:|---|",
        ]
        for e in p["events"]:
            pct = e["pct_of_circulating"]
            lines.append(
                f"| {e['base_asset']} | {e['event_date_utc']} | {e['event_type']} | "
                f"{e['recipient_type'] or 'unknown'} | "
                f"{_num(pct * 100 if pct is not None else None)} | {e['confidence']} |"
            )
        lines.append("")

    lines += _quality_section(p["quality"])
    lines += [
        "---",
        "",
        f"_Generated {p['generated_at_utc']}. Every figure above is read from a "
        "stored row; none is recomputed here._",
        "",
    ]
    return "\n".join(lines)


def _layer3_section(payload: dict[str, Any]) -> list[str]:
    """Structure and positioning risk on the ranked head.

    Deliberately placed BELOW the ranking and labelled advisory. Layer 3 cannot
    promote anything, and a section that appears above the ranking reads like
    a recommendation list -- which is exactly the TRB failure mode, where a
    derivatives reading was treated as a reason to buy.
    """
    l3 = payload.get("layer3") or {}
    if not l3:
        return []

    ranked = payload["ranked"][: payload["report_top_n"]]
    rows = [(r["base_asset"], l3[r["base_asset"]]) for r in ranked if r["base_asset"] in l3]
    if not rows:
        return []

    lines = [
        "## Layer 3 — structure and positioning risk (advisory)",
        "",
        "Layer 3 never promotes an asset. It says where a thesis would be wrong "
        "and how fragile the positioning is. A derivatives reading is a RISK "
        "check, never a buy trigger.",
        "",
        "| Asset | Setup | Invalidation | Bid depth ±2% | Risk flags |",
        "|---|---|---:|---:|---|",
    ]
    for asset, row in rows:
        setup = row["setup_type"] or "—"
        if not row["setup_detected"] and row["setup_type"]:
            setup = f"{setup} (not live)"
        invalidation = row["invalidation_price"]
        depth = row["depth_2pct_usd"]
        flags = json_load(row["risk_flags"], []) or []
        lines.append(
            "| {asset} | {setup} | {invalidation} | {depth} | {flags} |".format(
                asset=asset,
                setup=setup,
                invalidation="—" if invalidation is None else f"{invalidation:,.6g}",
                depth="—" if depth is None else f"${depth:,.0f}",
                flags=", ".join(flags) or "—",
            )
        )
    live = sum(1 for _, r in rows if r["setup_detected"])
    lines.append("")
    lines.append(
        f"{live} of {len(rows)} reported assets carry a live setup with an explicit "
        "invalidation level. The rest are shown so the absence is visible: no "
        "level, no candidate."
    )
    lines.append("")
    return lines


def _notable_failures(failures: list[dict[str, Any]], limit: int = 15) -> list[dict[str, Any]]:
    """The disqualifications worth showing: the ones that failed on ONE check.

    Spec 14 asks for these ordered by "highest L2 potential", which cannot be
    computed here -- L2 never sees a disqualified asset, and scoring one to
    order this table would mean building the very bypass the architecture
    forbids. Failing a single check is the honest proxy: it is the asset that
    came closest to surviving, which is exactly the case where a
    mis-calibrated threshold would show up first. See docs/DECISIONS.md D-011.
    """
    scored: list[tuple[int, float, dict[str, Any]]] = []
    for row in failures:
        failed = json_load(row["failed_checks"], []) or []
        if not failed:
            continue
        values = json_load(row["check_values"], {}) or {}
        first = values.get(failed[0], {})
        # Market cap is already stored as the L1_MCAP check's own value, so
        # this reads a recorded number rather than re-deriving one.
        mcap = (values.get("L1_MCAP") or {}).get("value") or 0.0
        scored.append(
            (
                len(failed),
                -float(mcap),
                {
                    "base_asset": row["base_asset"],
                    "check_id": failed[0],
                    "failed_count": len(failed),
                    "market_cap_usd": float(mcap) or None,
                    "value_display": first.get("value_display", "n/a"),
                    "threshold": first.get("threshold"),
                    "reason": first.get("reason", ""),
                },
            )
        )
    # Fewest checks failed first, then largest market cap. Alphabetical order
    # (the previous fallback) buried the interesting rows: the table's purpose
    # is to show the BIGGEST thing the filter removed, because that is the one
    # the reader is most likely to think it got wrong.
    scored.sort(key=lambda triple: (triple[0], triple[1]))
    return [row for _, _, row in scored[:limit]]


def _failure_counts(failures: list[dict[str, Any]]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for row in failures:
        for check in json_load(row["failed_checks"], []) or []:
            counts[check] = counts.get(check, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def _quality_section(quality: dict[str, Any]) -> list[str]:
    lines = ["## Data quality", ""]

    coverage = quality.get("coverage") or {}
    if coverage:
        lines += [
            "| Input table | Snapshot | Age | Measured | Rows | Share of universe |",
            "|---|---|---:|---:|---:|---:|",
        ]
        for table, stats in coverage.items():
            share = stats["of_universe"]
            age = stats.get("age_days")
            lines.append(
                f"| {table} | {stats.get('as_of') or '—'} | "
                f"{'—' if age is None else f'{age}d'} | "
                f"{stats['assets']} | {stats['rows_present']} | "
                f"{'—' if share is None else f'{share:.1%}'} |"
            )
        lines.append("")
        lines += [
            "_\"Measured\" counts assets with an actual value; \"Rows\" counts "
            "rows written. Where the two differ, a row exists with nothing in "
            "it, and the block was scored on far less than the row count "
            "suggests. \"Snapshot\" is the date these counts describe -- the "
            "same latest-at-or-before-today row the screen itself used, so a "
            "non-zero age means today's ranking was built on older data rather "
            "than that nothing was collected._",
            "",
        ]

    blocks = quality.get("blocks") or {}
    uninformative = [
        (name, stats) for name, stats in blocks.items() if not stats["informative"]
    ]
    if uninformative:
        dead_weight = sum(stats["weight"] or 0 for _, stats in uninformative)
        total_weight = sum(stats["weight"] or 0 for stats in blocks.values())
        lines += [
            "**Blocks that separated nothing today.** A block scoring the same "
            "value for every asset changes no ordering, so it raises no error "
            "-- but the score looks more informed than it is.",
            "",
            "| Block | Weight | Scored | Distinct values |",
            "|---|---:|---:|---:|",
        ]
        for name, stats in uninformative:
            lines.append(
                f"| {name} | {stats['weight']:g} | {stats['scored']}/"
                f"{stats['of_ranked']} | {stats['distinct_values']} |"
            )
        share = dead_weight / total_weight if total_weight else 0.0
        lines += [
            "",
            f"_{dead_weight:g} of {total_weight:g} weight ({share:.0%}) carried no "
            "information. The ranking is effectively built from the remaining "
            "blocks; it is not wrong, but it rests on fewer inputs than the six "
            "the weights imply._",
            "",
        ]

    collectors = quality.get("collectors") or {}
    if collectors:
        failed = [
            f"{name} {stats['failed']}x"
            for name, stats in collectors.items()
            if stats["failed"]
        ]
        partial = [name for name, stats in collectors.items() if stats["partial"]]
        lines.append(f"Collectors over the last 7 days: {len(collectors)} ran.")
        lines.append("")
        if failed:
            lines.append("Failed runs: " + ", ".join(failed) + ".")
        else:
            lines.append("No failed runs.")
        if partial:
            lines.append(
                "Degraded (ran, but an optional source was missing or truncated): "
                + ", ".join(sorted(partial))
                + ". These produced data; they are not outages."
            )
        lines.append("")

    news = quality.get("news_lag_seconds") or {}
    if news.get("n"):
        lines.append(
            f"News lag (n={news['n']}): p50 {news['p50']}s, p95 {news['p95']}s."
        )
    else:
        lines.append("News lag: no measured items in the last 24h.")

    trigger = quality.get("trigger_lag_seconds") or {}
    if trigger.get("n"):
        lines.append(
            f"Trigger lag (n={trigger['n']}): p50 {trigger['p50']}s, "
            f"p95 {trigger['p95']}s."
        )
    lines.append("")
    return lines


def latest_screen_date(db: Database) -> str | None:
    """The newest day a screen actually ran, or None if none ever has.

    The report, the Telegram summary and the publisher all default to this
    rather than to today (D-064, D-067). Today is a guess about what happened;
    this is a reading of what did.
    """
    return db.scalar("SELECT MAX(run_date) FROM layer1_result")


class NothingToReport(RuntimeError):
    """The date has no screen results, so a report of it would read as a day
    on which nothing survived (D-067)."""


def write_report(run_date: str | None = None) -> Path:
    """Render the report to reports/YYYY-MM-DD.md and return its path."""
    cfg = get_config()
    with get_db() as db:
        date = run_date or latest_screen_date(db)
        if not date or not db.scalar(
            "SELECT COUNT(*) FROM layer1_result WHERE run_date = ?", (date,)
        ):
            raise NothingToReport(
                f"no screen results for {date or 'any date'}: a report of an unscreened "
                "day reads as a day on which nothing survived"
            )
        payload = gather(db, date)
    text = render(payload)

    out_dir = cfg.path(cfg.settings.reporting.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{date}.md"
    path.write_text(text, encoding="utf-8")
    log.info(
        "report_written",
        path=str(path),
        universe=payload["universe"],
        survivors=payload["survivors"],
        dark_checks=payload["dark_checks"],
    )
    return path


def staleness_hours(run_date: str, generated_at: str | None = None) -> float:
    """Age of a run in hours. Used by the dashboard's stale-data banner."""
    return age_hours(generated_at or f"{run_date}T00:00:00Z")


__all__ = [
    "NothingToReport",
    "data_quality",
    "flags_for",
    "gather",
    "latest_screen_date",
    "render",
    "staleness_hours",
    "write_report",
]
