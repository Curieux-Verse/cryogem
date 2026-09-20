"""
# WHY: ------------------------------------------------------------------------
# One Pulse hour, end to end (PLAN section 6):
#
#     ensure tables -> collect (data.py) -> features -> gem ranks -> score
#       -> pulse_result -> journal (D-081) -> alerts (D-080)
#
# TIMING-AGNOSTIC. The hour scored is hour_floor(now): the newest hour whose
# 1H bar has closed. Whether cron-job.org fires at :03 or :25 changes only how
# stale the bars are, never which hour is scored, so a late trigger re-scores
# the same hour and the upsert makes that a no-op.
#
# FAILURE ORDER. If the collector failed, NOTHING is written to pulse_result:
# a partial or stale hour stamped as current is worse than a missing hour,
# which the page reports as "unavailable". The journal runs after the result
# is written and its failure is reported (the CLI exits non-zero -- a skipped
# journal hour can be healed by re-running the hour, since ids are
# deterministic). Alerts can never fail the run.
#
# collect-hourly never runs init-db, so the Pulse tables are created here on
# first use -- checked once with one sqlite_master read, applied only when a
# Pulse table is missing.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from src.config import get_config
from src.db.connection import Database, get_db
from src.db.writes import json_dump, upsert
from src.logging_setup import get_logger
from src.timeutil import format_instant, utc_now, utc_now_iso

log = get_logger("pulse.run")

PULSE_TABLES = frozenset(
    {"bar_1h", "oi_1h", "series_cursor", "pulse_result", "pulse_journal",
     "pulse_forward_return", "pulse_alert"}
)
SPARK_1H_BARS = 48
SPARK_4H_BARS = 42


def ensure_pulse_tables(db: Database) -> bool:
    """Apply the schema only if a Pulse table is missing. True if it applied."""
    missing = PULSE_TABLES - set(db.table_names())
    if not missing:
        return False
    log.info("pulse_schema_apply", missing=sorted(missing))
    db.apply_schema()
    return True


def read_gem_ranks(db: Database) -> dict[str, int]:
    """base_asset -> rank from the newest Layer 2 run. One query."""
    rows = db.query(
        "SELECT base_asset, rank FROM layer2_result "
        "WHERE run_date = (SELECT MAX(run_date) FROM layer2_result) AND rank IS NOT NULL"
    )
    return {r["base_asset"]: int(r["rank"]) for r in rows}


def _py(value: Any) -> Any:
    """numpy/pandas scalars -> plain Python; NaN/inf -> None."""
    if value is None:
        return None
    if hasattr(value, "item") and not isinstance(value, (list, dict, str)):
        try:
            value = value.item()
        except (ValueError, AttributeError):
            pass
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (list, tuple)):
        return [_py(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _py(v) for k, v in value.items()}
    try:
        import pandas as pd

        if value is pd.NA or value is pd.NaT:
            return None
    except ImportError:  # pragma: no cover
        pass
    return value


def _sig(value: Any, digits: int = 6) -> float | None:
    value = _py(value)
    if value is None:
        return None
    return float(f"{float(value):.{digits}g}")


def sparklines(bars_1h: Any) -> dict[str, list[float | None]]:
    """spark_1h, flow_1h (same 48 bars) and spark_4h (42 complete 4H closes)."""
    from src.pulse.features import resample_4h

    out: dict[str, list[float | None]] = {"spark_1h": [], "flow_1h": [], "spark_4h": []}
    if bars_1h is None or len(bars_1h) == 0:
        return out
    tail = bars_1h.tail(SPARK_1H_BARS)
    out["spark_1h"] = [_sig(v) for v in tail["close"].tolist()]
    flows: list[float | None] = []
    for vol, buy in zip(tail["quote_volume"].tolist(), tail["taker_buy_quote"].tolist()):
        vol, buy = _py(vol), _py(buy)
        flows.append(round((2 * buy - vol) / vol, 4) if vol and buy is not None else None)
    out["flow_1h"] = flows
    try:
        bars_4h = resample_4h(bars_1h)
        out["spark_4h"] = [_sig(v) for v in bars_4h["close"].tail(SPARK_4H_BARS).tolist()]
    except Exception as exc:  # noqa: BLE001 - a chart is not worth an hour
        log.warning("pulse_spark_4h_failed", error=type(exc).__name__)
    return out


def build_rows(
    ts: str,
    features: Any,
    scored: Any,
    window: Any,
    gem_ranks: dict[str, int],
    score_version: str,
) -> list[dict[str, Any]]:
    """One pulse_result row per survivor, excluded ones included (score NULL)."""
    now = utc_now_iso()
    rows: list[dict[str, Any]] = []
    universe = len(scored)
    bars = getattr(window, "bars", {}) or {}
    for asset in scored.index:
        s = scored.loc[asset]
        f = features.loc[asset] if asset in features.index else None
        score = _py(s.get("score"))
        rank = _py(s.get("rank"))
        feats: dict[str, Any] = {}
        if f is not None:
            for col in features.columns:
                if col == "flags":
                    continue
                feats[col] = _py(f[col])
        feats["coverage"] = _py(s.get("coverage"))
        feats["penalty"] = _py(s.get("penalty"))
        if score is not None:
            feats.update(sparklines(bars.get(asset)))
        gem = _py(s.get("gem_rank"))
        if gem is None:
            gem = gem_ranks.get(asset)
        rows.append(
            {
                "ts_utc": ts,
                "base_asset": str(asset),
                "score": round(float(score), 4) if score is not None else None,
                "rank": int(rank) if rank is not None else None,
                "universe_size": universe,
                "state_4h": _py(s.get("state_4h")),
                "state_1h": _py(s.get("state_1h")),
                "oi_quadrant": _py(s.get("oi_quadrant")),
                "flags": json_dump(_py(list(s.get("flags") or []))),
                "components": json_dump(_py(dict(s.get("components") or {}))),
                "features": json_dump(feats),
                "aligned": 1 if bool(_py(s.get("aligned"))) else 0,
                "gem_rank": int(gem) if gem is not None else None,
                "score_version": score_version,
                "fetched_at_utc": now,
            }
        )
    return rows


#: Sparklines are drawn only for the newest hour and read for the one before it
#: (the bake). Kept on every row they cost ~1.8KB x ~160 rows an hour -- about
#: 2.5GB of Turso storage a year for pictures nobody can see again. Hours in
#: this band have them stripped; bar_1h still holds every price they came from.
#: The band is a week, not a couple of hours: hourly runs then touch ~160 rows,
#: while a gap (a day of failed runs) is still cleaned up when they resume.
SPARK_KEEP_HOURS = 2
SPARK_PRUNE_BAND_HOURS = 168


def prune_sparklines(db: Database, stamp: datetime) -> int:
    """Strip spark arrays from pulse_result rows 2..12 hours old. Returns rows touched."""
    newest_old = format_instant(stamp - timedelta(hours=SPARK_KEEP_HOURS))
    oldest = format_instant(stamp - timedelta(hours=SPARK_PRUNE_BAND_HOURS))
    return db.execute(
        "UPDATE pulse_result SET features = "
        "json_remove(features, '$.spark_1h', '$.flow_1h', '$.spark_4h') "
        "WHERE ts_utc < ? AND ts_utc >= ? AND features IS NOT NULL "
        "AND json_extract(features, '$.spark_1h') IS NOT NULL",
        (newest_old, oldest),
    )


async def run_pulse(as_of: datetime | None = None) -> dict[str, Any]:
    """Score one closed hour. Returns a summary; summary['ok'] drives the exit code."""
    from src.pulse.data import BinancePulseCollector, hour_floor
    from src.pulse.features import compute_features
    from src.pulse.score import score_pulse

    cfg = get_config().thresholds.pulse
    stamp = hour_floor(as_of or utc_now())
    ts = format_instant(stamp)
    summary: dict[str, Any] = {"ok": False, "ts_utc": ts, "score_version": cfg.score_version}

    with get_db() as db:
        summary["schema_applied"] = ensure_pulse_tables(db)

    collector = BinancePulseCollector()
    result = await collector.run(stamp)
    summary["collector_status"] = result.status
    if not result.ok:
        summary["error"] = f"collector {result.status}: {result.error_message or ''}".strip()
        log.error("pulse_collector_failed", ts=ts, status=result.status)
        return summary

    window = getattr(collector, "window", None)
    if window is None or not getattr(window, "survivors", None):
        summary["error"] = "no survivors in the market window (no Layer 1 run on file?)"
        log.error("pulse_no_survivors", ts=ts)
        return summary

    features = compute_features(window)
    with get_db() as db:
        gem_ranks = read_gem_ranks(db)
    scored = score_pulse(features, gem_ranks)
    rows = build_rows(ts, features, scored, window, gem_ranks, cfg.score_version)
    if not rows:
        summary["error"] = "scoring produced no rows"
        return summary

    with get_db() as db:
        upsert(db, "pulse_result", rows)
        summary["sparks_pruned"] = prune_sparklines(db, stamp)
    summary.update(
        ok=True,
        universe=len(rows),
        scored=sum(1 for r in rows if r["score"] is not None),
        aligned=[r["base_asset"] for r in rows if r["aligned"]],
    )

    # Journal and alerts share one read of the previous hour.
    from src.pulse import alerts, journal

    current = [{**r} for r in rows]
    try:
        with get_db() as db:
            prev_ts = journal.previous_scored_hour(db, ts)
            previous = journal.read_hour(db, prev_ts) if prev_ts else []
            summary["previous_hour"] = prev_ts
            if previous:
                summary["journal"] = journal.write_entries(db, ts, current, previous)
            else:
                summary["journal"] = {"entries": 0, "controls": 0, "baseline": None}
            summary["returns_filled"] = journal.backfill_returns(db, now=stamp)
    except Exception as exc:  # noqa: BLE001 - reported; the result is already written
        log.error("pulse_journal_failed", ts=ts, error=f"{type(exc).__name__}: {exc}"[:300])
        summary["journal_error"] = type(exc).__name__
        previous = None

    try:
        with get_db() as db:
            summary["alerts"] = alerts.send_alerts(db, ts, current, previous)
    except Exception as exc:  # noqa: BLE001 - an alert must never fail the run
        log.error("pulse_alerts_failed", ts=ts, error=type(exc).__name__)
        summary["alerts"] = {"error": type(exc).__name__}

    log.info("pulse_done", **{k: v for k, v in summary.items() if k not in {"aligned"}})
    return summary


__all__ = [
    "PULSE_TABLES",
    "build_rows",
    "ensure_pulse_tables",
    "prune_sparklines",
    "read_gem_ranks",
    "run_pulse",
    "sparklines",
]
