"""
# WHY: ------------------------------------------------------------------------
# Bakes data/public/pulse.json (contract.PULSE_JSON_EXAMPLE) from pulse_result.
#
# The file is NEVER committed (.gitignore): it is baked by build-site inside
# every deploy, hourly and daily, so the site refreshes without a git commit
# (PLAN section 6). It is written through src/report/publish._write, i.e. the
# same NaN handling and the same key-shape guard as every other public file.
#
# Honest failure: when the newest scored hour is older than STALE_AFTER, or
# there is none, or the database cannot be read, the file still has the full
# shape with status "unavailable" and no assets. A page that shows "Pulse
# unavailable" is correct; a page that shows a three-hour-old ranking with a
# fresh generated_at is not.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src.config import get_config
from src.db.connection import Database
from src.db.writes import json_load
from src.logging_setup import get_logger
from src.pulse import journal
from src.pulse.contract import EXCLUDING_FLAGS, PULSE_JSON_SCHEMA_VERSION
from src.timeutil import format_instant, parse_instant, utc_now, utc_now_iso

log = get_logger("pulse.publish")

FILE_NAME = "pulse.json"
STALE_AFTER = timedelta(hours=3)
RISERS_N = 10

#: The feature subset the page shows, exactly the example's keys.
PUBLIC_FEATURES = (
    "flow_24h", "flow_4h", "ret_4h", "ret_24h", "oi_chg_24h", "thrust_4h",
    "quote_volume_24h", "invalidation_4h",
)
COMPONENT_KEYS = ("flow", "structure_4h", "momentum", "thrust", "oi_confirm", "structure_1h")


def _iso_z(ts: str | None) -> str | None:
    return format_instant(parse_instant(ts)) if ts else None


def unavailable_payload(as_of: str | None = None, reason: str | None = None) -> dict[str, Any]:
    """The full shape, empty. `reason` is logged, not published."""
    if reason:
        log.warning("pulse_unavailable", reason=reason, as_of=as_of)
    return {
        "schema_version": PULSE_JSON_SCHEMA_VERSION,
        "status": "unavailable",
        "as_of_utc": _iso_z(as_of),
        "generated_at_utc": utc_now_iso(),
        "score_version": None,
        "universe_size": 0,
        "scored": 0,
        "aligned": [],
        "risers": [],
        "assets": [],
        "excluded": [],
    }


def build_pulse(db: Database, now: datetime | None = None) -> dict[str, Any]:
    """The pulse.json payload from the newest scored hour (+ the one before)."""
    from src.report.publish import _safe_name

    stamp = now or utc_now()
    newest = db.scalar("SELECT MAX(ts_utc) FROM pulse_result")
    if not newest:
        return unavailable_payload(None, "no pulse_result rows")
    if stamp - parse_instant(newest) > STALE_AFTER:
        return unavailable_payload(newest, "newest pulse_result hour is stale")

    rows = journal.read_hour(db, newest)
    prev_ts = journal.previous_scored_hour(db, newest)
    prev = {r["base_asset"]: r for r in (journal.read_hour(db, prev_ts) if prev_ts else [])}

    scored = sorted(
        (r for r in rows if r["score"] is not None),
        key=lambda r: (r["rank"] if r["rank"] is not None else 10**6, r["base_asset"]),
    )
    assets: list[dict[str, Any]] = []
    for r in scored:
        feats = json_load(r["features"], {}) or {}
        comps = json_load(r["components"], {}) or {}
        before = prev.get(r["base_asset"])
        prev_scored = before is not None and before["score"] is not None
        assets.append(
            {
                "asset": r["base_asset"],
                "score": round(float(r["score"]), 2),
                "rank": r["rank"],
                "prev_rank": before["rank"] if prev_scored else None,
                "delta": round(float(r["score"]) - float(before["score"]), 2) if prev_scored else None,
                "gem_rank": r["gem_rank"],
                "aligned": bool(r["aligned"]),
                "state_4h": r["state_4h"],
                "bars_since_4h": feats.get("bars_since_4h"),
                "state_1h": r["state_1h"],
                "oi_quadrant": r["oi_quadrant"],
                "flags": json_load(r["flags"], []) or [],
                "coverage": feats.get("coverage"),
                "components": {k: comps.get(k) for k in COMPONENT_KEYS},
                "features": {k: feats.get(k) for k in PUBLIC_FEATURES},
                "spark_1h": feats.get("spark_1h") or [],
                "flow_1h": feats.get("flow_1h") or [],
                "spark_4h": feats.get("spark_4h") or [],
                "file": f"assets/{_safe_name(r['base_asset'])}.json",
            }
        )

    excluded = []
    for r in rows:
        if r["score"] is not None:
            continue
        flags = json_load(r["flags"], []) or []
        reason = next((f for f in flags if f in EXCLUDING_FLAGS), None) or (flags[0] if flags else "unscored")
        excluded.append({"asset": r["base_asset"], "reason": reason})

    risers = sorted(
        (a for a in assets if a["delta"] is not None and a["delta"] > 0),
        key=lambda a: (-a["delta"], a["asset"]),
    )[:RISERS_N]

    return {
        "schema_version": PULSE_JSON_SCHEMA_VERSION,
        "status": "ok",
        "as_of_utc": _iso_z(newest),
        "generated_at_utc": utc_now_iso(),
        "score_version": next((r["score_version"] for r in rows if r["score_version"]), None),
        "universe_size": len(rows),
        "scored": len(assets),
        "aligned": [a["asset"] for a in assets if a["aligned"]],
        "risers": [
            {"asset": a["asset"], "delta": a["delta"], "rank": a["rank"], "prev_rank": a["prev_rank"]}
            for a in risers
        ],
        "assets": assets,
        "excluded": excluded,
    }


JOURNAL_FILE_NAME = "pulse_journal.json"

#: Entries carried into the page, newest first. The journal is append-only and
#: grows by ~24 entries a day, so the FILE is bounded while the TABLE is not:
#: every statistic above is computed over all of it, and only the visible list
#: is trimmed. `entries_total` always says how many exist.
JOURNAL_ENTRIES_SHOWN = 500

#: Completed returns before the page draws any conclusion, matching the daily
#: journal's gate. Published so the page cannot quietly use a smaller one.
JOURNAL_MIN_FOR_CONCLUSION = 30


def journal_unavailable_payload(reason: str | None = None) -> dict[str, Any]:
    """The full shape, empty, when the journal cannot be read."""
    if reason:
        log.warning("pulse_journal_unavailable", reason=reason)
    return {
        "schema_version": PULSE_JSON_SCHEMA_VERSION,
        "status": "unavailable",
        "generated_at_utc": utc_now_iso(),
        "score_version": None,
        "horizons": [],
        "min_for_conclusion": JOURNAL_MIN_FOR_CONCLUSION,
        "entries_total": 0,
        "entries_with_returns": 0,
        "entries_shown": 0,
        "first_entry_utc": None,
        "coverage_note": "The Pulse journal could not be read.",
        "statistics": {},
        "entries": [],
    }


def _journal_coverage_note(total: int, with_returns: int, horizons: list[str]) -> str:
    """Says how far short it is, rather than shrugging (the daily journal's rule)."""
    if total == 0:
        return (
            "No Pulse entries yet. An entry is written when an asset ENTERS the "
            "Pulse top 10 or ALIGNED, so the first scored hour writes none."
        )
    longest = horizons[-1] if horizons else "72h"
    return (
        f"{total} entries recorded, {with_returns} with at least one completed "
        f"return. A return is filled only once every bar in its window exists, "
        f"so the newest entries wait up to {longest} for their last horizon."
    )


def build_pulse_journal(db: Database) -> dict[str, Any]:
    """The pulse_journal.json payload: statistics, then every entry with its returns.

    Includes the losers and the control, for the same reason the daily journal
    does: a forward-return list with the bad entries removed is not a record,
    it is marketing. `pulse_journal` is append-only in the database, so this
    file cannot show a curated subset of it -- only a bounded, newest-first
    window of a list nothing can delete from.
    """
    from src.report.publish import _safe_name

    horizons = [
        journal.horizon_label(h)
        for h in get_config().thresholds.pulse.journal_horizons_hours
    ]
    entries = db.query(
        "SELECT entry_id, ts_signal_utc, base_asset, trigger, is_control, pulse_score, "
        "pulse_rank, gem_rank, state_4h, score_version FROM pulse_journal "
        "ORDER BY ts_signal_utc DESC, is_control, pulse_rank"
    )
    returns = db.query(
        "SELECT entry_id, horizon, return_raw, return_vs_btc, max_favourable, max_adverse "
        "FROM pulse_forward_return"
    )
    by_entry: dict[str, dict[str, Any]] = {}
    for row in returns:
        by_entry.setdefault(row["entry_id"], {})[row["horizon"]] = {
            "return_raw": row["return_raw"],
            "return_vs_btc": row["return_vs_btc"],
            "max_favourable": row["max_favourable"],
            "max_adverse": row["max_adverse"],
        }

    shown = [
        {
            "entry_id": e["entry_id"],
            "ts_signal_utc": _iso_z(e["ts_signal_utc"]),
            "asset": e["base_asset"],
            "trigger": e["trigger"],
            "is_control": bool(e["is_control"]),
            "pulse_score": e["pulse_score"],
            "pulse_rank": e["pulse_rank"],
            "gem_rank": e["gem_rank"],
            "state_4h": e["state_4h"],
            "score_version": e["score_version"],
            "returns": by_entry.get(e["entry_id"], {}),
            "file": f"assets/{_safe_name(e['base_asset'])}.json",
        }
        for e in entries[:JOURNAL_ENTRIES_SHOWN]
    ]
    with_returns = sum(1 for e in entries if e["entry_id"] in by_entry)
    return {
        "schema_version": PULSE_JSON_SCHEMA_VERSION,
        "status": "ok",
        "generated_at_utc": utc_now_iso(),
        "score_version": get_config().thresholds.pulse.score_version,
        "horizons": horizons,
        "min_for_conclusion": JOURNAL_MIN_FOR_CONCLUSION,
        "entries_total": len(entries),
        "entries_with_returns": with_returns,
        "entries_shown": len(shown),
        "first_entry_utc": _iso_z(entries[-1]["ts_signal_utc"]) if entries else None,
        "coverage_note": _journal_coverage_note(len(entries), with_returns, horizons),
        # {score_version: {trigger: {horizon: block}}}, exactly `pulse report`.
        "statistics": journal.pulse_report(db)["versions"],
        "entries": shown,
    }


def bake_pulse_journal_json(db: Database | None, out_dir: Path) -> Path:
    """Write out_dir/pulse_journal.json. A database error still writes 'unavailable'."""
    from src.report.publish import _write

    error: Exception | None = None
    try:
        if db is None:
            raise RuntimeError("no database")
        payload = build_pulse_journal(db)
    except Exception as exc:  # noqa: BLE001 - the page must still say "unavailable"
        error = exc
        payload = journal_unavailable_payload(f"{type(exc).__name__}")
    out_dir.mkdir(parents=True, exist_ok=True)
    _write(out_dir, JOURNAL_FILE_NAME, payload)
    log.info(
        "pulse_journal_baked",
        status=payload["status"],
        entries=payload["entries_total"],
        with_returns=payload["entries_with_returns"],
    )
    if error is not None:
        raise error
    return out_dir / JOURNAL_FILE_NAME


def bake_pulse_json(db: Database | None, out_dir: Path, now: datetime | None = None) -> Path:
    """Write out_dir/pulse.json. A database error still writes 'unavailable'.

    Re-raises after writing the unavailable file, so the caller can exit
    non-zero while the site still ships an honest status.
    """
    from src.report.publish import _write

    error: Exception | None = None
    try:
        if db is None:
            raise RuntimeError("no database")
        payload = build_pulse(db, now)
    except Exception as exc:  # noqa: BLE001 - the page must still say "unavailable"
        error = exc
        payload = unavailable_payload(None, f"{type(exc).__name__}")
    out_dir.mkdir(parents=True, exist_ok=True)
    _write(out_dir, FILE_NAME, payload)
    log.info("pulse_baked", status=payload["status"], assets=len(payload["assets"]))
    if error is not None:
        raise error
    return out_dir / FILE_NAME


__all__ = [
    "FILE_NAME",
    "JOURNAL_FILE_NAME",
    "JOURNAL_MIN_FOR_CONCLUSION",
    "STALE_AFTER",
    "bake_pulse_journal_json",
    "bake_pulse_json",
    "build_pulse",
    "build_pulse_journal",
    "journal_unavailable_payload",
    "unavailable_payload",
]
