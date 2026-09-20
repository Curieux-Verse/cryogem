"""
# WHY: ------------------------------------------------------------------------
# The Pulse journal (D-081). Same discipline as src/journal/forward_returns.py,
# on the hourly clock:
#
#   1. APPEND ONLY. pulse_journal and pulse_forward_return are guarded by DB
#      triggers and written through writes.upsert, which turns an APPEND_ONLY
#      table into INSERT OR IGNORE. Ids are deterministic, so re-running an
#      hour adds nothing and overwrites nothing.
#
#   2. AN EVENT, NOT A STATE. An entry is written when an asset ENTERS the
#      Pulse top N or ALIGNED compared with the previous scored hour -- never
#      "still in the top 10". The previous hour must be recent (PREV_MAX_GAP):
#      after an outage, "entered" would mean "entered at some point during the
#      gap AND was still there", which selects on persistence.
#
#   3. A RANDOM CONTROL per entry-hour, drawn from that hour's scored
#      survivors that did not trigger, seeded from the hour. Its id is per
#      HOUR, not per asset, so a re-run with a different pool cannot add a
#      second control beside the first (the D-062 lesson).
#
#   4. ENTRY AT THE NEXT BAR'S OPEN. The signal hour ts is the hour the score
#      describes (bars closed by ts). The run lands some minutes later -- :25
#      today -- so the bar opening AT ts has already printed its open before
#      the signal exists. The entry is therefore the open of the bar opening
#      at ts + 1h: the first price anyone could trade after the signal,
#      whatever minute the cron fires. The exit is the close of the bar ending
#      at entry + h; excursions use every bar in between. A return is written
#      only when every one of those bars (and BTC's entry/exit bars) exists.
#
# No kill logic lives here. pulse_report() shows results; it never zeroes a
# weight or relabels anything.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

import random
import statistics
from datetime import datetime, timedelta
from typing import Any

from src.config import get_config
from src.db.connection import Database
from src.db.writes import deterministic_id, json_dump, json_load, upsert
from src.logging_setup import get_logger
from src.timeutil import format_instant, parse_instant, utc_now, utc_now_iso

log = get_logger("pulse.journal")

BTC = "BTC"
TRIGGER_TOP = "top10"
TRIGGER_ALIGNED = "aligned"
TRIGGER_CONTROL = "control"

#: The previous scored hour must be at most this old to count as a baseline.
PREV_MAX_GAP = timedelta(hours=3)

#: How long after a horizon falls due an entry is still retried. Bounds the
#: hourly read: without it the pending scan grows with the whole journal.
BACKFILL_GRACE = timedelta(hours=72)

#: Features copied into a journal row (small; sparklines are not journalled).
JOURNAL_FEATURES = (
    "flow_24h", "flow_4h", "ret_4h", "ret_24h", "oi_chg_24h", "thrust_4h",
    "quote_volume_24h", "vamom_24h", "bars_since_4h", "coverage", "penalty",
)


# ------------------------------------------------------------------------------
# hour helpers, shared by alerts.py and publish.py
# ------------------------------------------------------------------------------
def previous_scored_hour(db: Database, ts: str, max_gap: timedelta = PREV_MAX_GAP) -> str | None:
    """Newest pulse_result hour strictly before `ts`, if within `max_gap`."""
    prev = db.scalar("SELECT MAX(ts_utc) FROM pulse_result WHERE ts_utc < ?", (ts,))
    if not prev:
        return None
    if parse_instant(ts) - parse_instant(prev) > max_gap:
        return None
    return prev


def read_hour(db: Database, ts: str) -> list[dict[str, Any]]:
    """Every pulse_result row of one hour (~160 rows)."""
    return db.query(
        "SELECT ts_utc, base_asset, score, rank, universe_size, state_4h, state_1h, "
        "oi_quadrant, flags, components, features, aligned, gem_rank, score_version "
        "FROM pulse_result WHERE ts_utc = ? ORDER BY base_asset",
        (ts,),
    )


def _in_top(row: dict[str, Any], top_n: int) -> bool:
    return row.get("score") is not None and row.get("rank") is not None and int(row["rank"]) <= top_n


# ------------------------------------------------------------------------------
# entries
# ------------------------------------------------------------------------------
def _entry_row(row: dict[str, Any], ts: str, trigger: str, now: str) -> dict[str, Any]:
    feats = json_load(row.get("features"), {}) or {}
    kept = {k: feats.get(k) for k in JOURNAL_FEATURES if k in feats}
    kept["components"] = json_load(row.get("components"), {}) or {}
    is_control = trigger == TRIGGER_CONTROL
    return {
        # Per hour for the control (one control per hour, whatever the pool);
        # per asset and trigger for a signal.
        "entry_id": (
            deterministic_id("pulse", ts, TRIGGER_CONTROL)
            if is_control
            else deterministic_id("pulse", ts, row["base_asset"], trigger)
        ),
        "ts_signal_utc": ts,
        "base_asset": row["base_asset"],
        "trigger": trigger,
        "is_control": 1 if is_control else 0,
        "pulse_score": row.get("score"),
        "pulse_rank": row.get("rank"),
        "gem_rank": row.get("gem_rank"),
        "state_4h": row.get("state_4h"),
        "features": json_dump(kept),
        "score_version": row.get("score_version"),
        "created_at_utc": now,
    }


def detect_entries(
    current: list[dict[str, Any]], previous: list[dict[str, Any]], top_n: int
) -> list[tuple[dict[str, Any], str]]:
    """(row, trigger) for every asset that ENTERED top N or ALIGNED."""
    prev = {r["base_asset"]: r for r in previous}
    out: list[tuple[dict[str, Any], str]] = []
    for row in sorted(current, key=lambda r: (r.get("rank") is None, r.get("rank") or 0, r["base_asset"])):
        before = prev.get(row["base_asset"])
        if _in_top(row, top_n) and not (before and _in_top(before, top_n)):
            out.append((row, TRIGGER_TOP))
        if row.get("aligned") and not (before and before.get("aligned")):
            out.append((row, TRIGGER_ALIGNED))
    return out


def draw_control(ts: str, current: list[dict[str, Any]], triggered: set[str]) -> dict[str, Any] | None:
    """One scored survivor that did not trigger, seeded from the hour."""
    pool = sorted(
        (r for r in current if r.get("score") is not None and r["base_asset"] not in triggered),
        key=lambda r: r["base_asset"],
    )
    if not pool:
        return None
    return random.Random(f"pulse-control|{ts}").choice(pool)


def write_entries(
    db: Database,
    ts: str,
    current: list[dict[str, Any]] | None = None,
    previous: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Journal the hour's entries plus one control. Idempotent."""
    cfg = get_config().thresholds.pulse
    if current is None:
        current = read_hour(db, ts)
    if previous is None:
        prev_ts = previous_scored_hour(db, ts)
        if prev_ts is None:
            log.info("pulse_journal_no_baseline", ts=ts)
            return {"entries": 0, "controls": 0, "baseline": None}
        previous = read_hour(db, prev_ts)
    if not current or not previous:
        return {"entries": 0, "controls": 0, "baseline": None}

    found = detect_entries(current, previous, cfg.journal_top_n)
    if not found:
        return {"entries": 0, "controls": 0, "baseline": True}

    now = utc_now_iso()
    rows = [_entry_row(row, ts, trigger, now) for row, trigger in found]
    control = draw_control(ts, current, {row["base_asset"] for row, _ in found})
    if control is not None:
        rows.append(_entry_row(control, ts, TRIGGER_CONTROL, now))
    upsert(db, "pulse_journal", rows)
    log.info("pulse_journal_written", ts=ts, entries=len(found), control=control is not None)
    return {"entries": len(found), "controls": 1 if control is not None else 0, "baseline": True}


# ------------------------------------------------------------------------------
# forward returns
# ------------------------------------------------------------------------------
def horizon_label(hours: int) -> str:
    return f"{int(hours)}h"


def _bars(db: Database, asset: str, first_open: str, last_open: str) -> list[dict[str, Any]]:
    return db.query(
        "SELECT ts_open_utc, open, high, low, close FROM bar_1h WHERE base_asset = ? "
        "AND ts_open_utc >= ? AND ts_open_utc <= ? ORDER BY ts_open_utc",
        (asset, first_open, last_open),
    )


def compute_forward_return(
    db: Database, asset: str, ts_signal: str, hours: int
) -> dict[str, Any] | None:
    """The return of a position opened at the next bar's open, or None while pending."""
    entry_dt = parse_instant(ts_signal) + timedelta(hours=1)
    last_open_dt = entry_dt + timedelta(hours=hours - 1)
    entry_ts, last_ts = format_instant(entry_dt), format_instant(last_open_dt)

    bars = _bars(db, asset, entry_ts, last_ts)
    if len(bars) != hours or bars[0]["ts_open_utc"] != entry_ts or bars[-1]["ts_open_utc"] != last_ts:
        return None
    entry_price = bars[0]["open"]
    exit_price = bars[-1]["close"]
    if not entry_price or exit_price is None:
        return None

    btc = {
        r["ts_open_utc"]: r
        for r in db.query(
            "SELECT ts_open_utc, open, close FROM bar_1h WHERE base_asset = ? "
            "AND ts_open_utc IN (?, ?)",
            (BTC, entry_ts, last_ts),
        )
    }
    btc_entry = (btc.get(entry_ts) or {}).get("open")
    btc_exit = (btc.get(last_ts) or {}).get("close")
    if not btc_entry or btc_exit is None:
        return None

    raw = (exit_price - entry_price) / entry_price
    btc_raw = (btc_exit - btc_entry) / btc_entry
    # A missing high/low falls back to the close, which understates both
    # excursions -- the honest direction (as the daily journal does).
    highs = [b["high"] if b["high"] is not None else b["close"] for b in bars]
    lows = [b["low"] if b["low"] is not None else b["close"] for b in bars]
    return {
        "entry_ts_utc": entry_ts,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "return_raw": round(raw, 6),
        "return_vs_btc": round(raw - btc_raw, 6),
        "max_favourable": round((max(highs) - entry_price) / entry_price, 6),
        "max_adverse": round((min(lows) - entry_price) / entry_price, 6),
    }


def backfill_returns(
    db: Database, now: datetime | None = None, grace: timedelta = BACKFILL_GRACE
) -> int:
    """Fill every horizon that has elapsed, within a bounded window."""
    stamp = now or utc_now()
    horizons = get_config().thresholds.pulse.journal_horizons_hours
    filled = 0
    for hours in horizons:
        label = horizon_label(hours)
        # Due once the exit bar (opening at ts + h) has closed: ts + h + 1h.
        due_by = stamp - timedelta(hours=hours + 1)
        pending = db.query(
            "SELECT j.entry_id, j.ts_signal_utc, j.base_asset FROM pulse_journal j "
            "LEFT JOIN pulse_forward_return f ON f.entry_id = j.entry_id AND f.horizon = ? "
            "WHERE f.entry_id IS NULL AND j.ts_signal_utc <= ? AND j.ts_signal_utc >= ?",
            (label, format_instant(due_by), format_instant(due_by - grace)),
        )
        rows: list[dict[str, Any]] = []
        for entry in pending:
            result = compute_forward_return(db, entry["base_asset"], entry["ts_signal_utc"], hours)
            if result is None:
                # A bar is missing: stay pending rather than write a number
                # that could never be corrected.
                continue
            rows.append(
                {"entry_id": entry["entry_id"], "horizon": label, **result,
                 "computed_at_utc": utc_now_iso()}
            )
        filled += upsert(db, "pulse_forward_return", rows)
    log.info("pulse_forward_returns_backfilled", filled=filled)
    return filled


# ------------------------------------------------------------------------------
# evaluation report (observational only: no kill logic)
# ------------------------------------------------------------------------------
def _stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"median": None, "mean": None}
    return {
        "median": round(statistics.median(values), 6),
        "mean": round(statistics.fmean(values), 6),
    }


def pulse_report(db: Database) -> dict[str, Any]:
    """By score_version, trigger and horizon: n, median/mean, hit rate, vs control."""
    rows = db.query(
        "SELECT j.trigger, j.is_control, j.score_version, f.horizon, f.return_raw, "
        "f.return_vs_btc, f.max_favourable, f.max_adverse "
        "FROM pulse_journal j JOIN pulse_forward_return f ON f.entry_id = j.entry_id"
    )
    grouped: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {}
    for r in rows:
        version = r["score_version"] or "unversioned"
        grouped.setdefault(version, {}).setdefault(r["trigger"], {}).setdefault(
            r["horizon"], []
        ).append(r)

    out: dict[str, Any] = {"generated_at_utc": utc_now_iso(), "versions": {}}
    for version, by_trigger in sorted(grouped.items()):
        controls = by_trigger.get(TRIGGER_CONTROL, {})
        vblock: dict[str, Any] = {}
        for trigger, by_horizon in sorted(by_trigger.items()):
            tblock: dict[str, Any] = {}
            for horizon, items in sorted(by_horizon.items(), key=lambda kv: int(kv[0][:-1])):
                raw = [i["return_raw"] for i in items if i["return_raw"] is not None]
                vs = [i["return_vs_btc"] for i in items if i["return_vs_btc"] is not None]
                ctl = [
                    i["return_vs_btc"] for i in controls.get(horizon, [])
                    if i["return_vs_btc"] is not None
                ]
                block = {
                    "n": len(items),
                    "raw": _stats(raw),
                    "vs_btc": _stats(vs),
                    "hit_rate_vs_btc": round(sum(v > 0 for v in vs) / len(vs), 4) if vs else None,
                    "max_adverse": _stats([i["max_adverse"] for i in items if i["max_adverse"] is not None]),
                    "max_favourable": _stats(
                        [i["max_favourable"] for i in items if i["max_favourable"] is not None]
                    ),
                }
                if trigger != TRIGGER_CONTROL:
                    block["vs_control"] = (
                        {
                            "control_n": len(ctl),
                            "median_difference": round(statistics.median(vs) - statistics.median(ctl), 6),
                            "mean_difference": round(statistics.fmean(vs) - statistics.fmean(ctl), 6),
                        }
                        if vs and ctl
                        else None
                    )
                tblock[horizon] = block
            vblock[trigger] = tblock
        out["versions"][version] = vblock
    return out


def render_report(report: dict[str, Any]) -> str:
    """Plain-text table for the CLI. States n; draws no conclusion."""
    lines = ["# Pulse journal (observational: no weights are changed from here)", ""]
    if not report["versions"]:
        lines.append("No completed forward returns yet. No conclusion can be drawn.")
        return "\n".join(lines)

    def pct(v: float | None) -> str:
        return "-" if v is None else f"{v * 100:+.2f}%"

    for version, by_trigger in report["versions"].items():
        lines.append(f"## score_version {version}")
        lines.append("| trigger | horizon | n | median vs BTC | mean vs BTC | hit rate | median - control |")
        lines.append("|---|---|---:|---:|---:|---:|---:|")
        for trigger, by_h in by_trigger.items():
            for horizon, b in by_h.items():
                ctl = b.get("vs_control") or {}
                hit = "-" if b["hit_rate_vs_btc"] is None else f"{b['hit_rate_vs_btc'] * 100:.0f}%"
                lines.append(
                    f"| {trigger} | {horizon} | {b['n']} | {pct(b['vs_btc']['median'])} | "
                    f"{pct(b['vs_btc']['mean'])} | {hit} | {pct(ctl.get('median_difference'))} |"
                )
        lines.append("")
    return "\n".join(lines)


__all__ = [
    "BACKFILL_GRACE",
    "PREV_MAX_GAP",
    "TRIGGER_ALIGNED",
    "TRIGGER_CONTROL",
    "TRIGGER_TOP",
    "backfill_returns",
    "compute_forward_return",
    "detect_entries",
    "draw_control",
    "horizon_label",
    "previous_scored_hour",
    "pulse_report",
    "read_hour",
    "render_report",
    "write_entries",
]
