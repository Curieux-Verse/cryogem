"""
# WHY: ------------------------------------------------------------------------
# Pulse Telegram alerts (D-080). STATE CHANGES ONLY:
#
#   (a) an asset newly ALIGNED this hour (not ALIGNED at the previous scored
#       hour);
#   (b) state_4h newly bear_break on an asset in the Gem top
#       `alert_bear_break_gem_top`.
#
# Never "still bullish". A change is detected against the previous scored
# hour, and only when that hour is recent (journal.PREV_MAX_GAP): with no
# baseline there is no change to announce, so a cold start or the first hour
# after an outage sends nothing.
#
# Dedup: pulse_alert remembers every alert attempted. The same asset+kind is
# never announced twice within 24h, and at most alert_max_per_day rows are
# written per UTC day -- counting failed deliveries too, so a broken bot cannot
# turn into a retry storm once it recovers. One batched message per hour.
#
# Delivery reuses src/report/telegram.py, which never raises. Unset secrets
# skip quietly and record NOTHING (otherwise the first configured hour would
# be deduplicated against alerts nobody received). A delivery failure records
# delivered=0 and never fails the Pulse run.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from src.config import get_config
from src.db.connection import Database
from src.db.writes import upsert
from src.logging_setup import get_logger
from src.pulse import journal
from src.timeutil import format_instant, parse_instant, utc_now_iso

log = get_logger("pulse.alerts")

KIND_ALIGNED = "aligned"
KIND_BEAR_BREAK = "bear_break"
DEDUP_WINDOW = timedelta(hours=24)


def detect_changes(
    current: list[dict[str, Any]], previous: list[dict[str, Any]], bear_gem_top: int
) -> list[tuple[dict[str, Any], str]]:
    """(row, kind) for every state change worth a message.

    bear_break first, by Gem rank: a risk warning is the alert a reader can
    lose money by missing, so the daily cap drops the new-ALIGNED names first.
    """
    prev = {r["base_asset"]: r for r in previous}
    bears: list[tuple[dict[str, Any], str]] = []
    aligned: list[tuple[dict[str, Any], str]] = []
    for row in current:
        before = prev.get(row["base_asset"])
        if before is None:
            # Not measured an hour ago: a first reading is not a change.
            continue
        gem = row.get("gem_rank")
        if (
            row.get("state_4h") == KIND_BEAR_BREAK
            and before.get("state_4h") != KIND_BEAR_BREAK
            and gem is not None
            and int(gem) <= bear_gem_top
        ):
            bears.append((row, KIND_BEAR_BREAK))
        if row.get("aligned") and not before.get("aligned"):
            aligned.append((row, KIND_ALIGNED))
    bears.sort(key=lambda x: (int(x[0]["gem_rank"]), x[0]["base_asset"]))
    aligned.sort(key=lambda x: (x[0].get("rank") or 10**6, x[0]["base_asset"]))
    return bears + aligned


def _line(row: dict[str, Any], kind: str) -> str:
    from src.report.telegram import _md

    asset = _md(row["base_asset"])
    score = row.get("score")
    score_txt = f"{score:.1f}" if isinstance(score, (int, float)) else "-"
    if kind == KIND_ALIGNED:
        return (
            f"ALIGNED `{asset}`: Pulse {score_txt} (rank {row.get('rank')}), "
            f"Gem rank {row.get('gem_rank')}, 4H {_md(row.get('state_4h'))}"
        )
    return (
        f"BEAR BREAK `{asset}`: 4H closed below rising support, Gem rank "
        f"{row.get('gem_rank')}, Pulse {score_txt}"
    )


def format_message(ts: str, changes: list[tuple[dict[str, Any], str]]) -> str:
    lines = [f"*Pulse {ts[:13].replace('T', ' ')}:00 UTC*"]
    lines += [_line(row, kind) for row, kind in changes]
    lines.append("")
    lines.append("State changes only. Not a buy or sell signal.")
    return "\n".join(lines)


def send_alerts(
    db: Database,
    ts: str,
    current: list[dict[str, Any]] | None = None,
    previous: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Detect, dedup, cap, send one message, record. Never raises on delivery."""
    from src.report import telegram

    cfg = get_config().thresholds.pulse
    if current is None:
        current = journal.read_hour(db, ts)
    if previous is None:
        prev_ts = journal.previous_scored_hour(db, ts)
        previous = journal.read_hour(db, prev_ts) if prev_ts else []
    if not previous:
        return {"candidates": 0, "sent": 0, "delivered": False, "reason": "no_baseline"}

    changes = detect_changes(current, previous, cfg.alert_bear_break_gem_top)
    if not changes:
        return {"candidates": 0, "sent": 0, "delivered": False, "reason": "no_change"}

    if not telegram.is_configured():
        log.info("pulse_alerts_skipped", reason="telegram_not_configured", candidates=len(changes))
        return {"candidates": len(changes), "sent": 0, "delivered": False, "reason": "unconfigured"}

    since = format_instant(parse_instant(ts) - DEDUP_WINDOW)
    recent = db.query(
        "SELECT ts_utc, base_asset, kind FROM pulse_alert WHERE ts_utc >= ?", (since,)
    )
    seen = {(r["base_asset"], r["kind"]) for r in recent}
    day = ts[:10]
    used_today = sum(1 for r in recent if r["ts_utc"][:10] == day)
    room = max(0, cfg.alert_max_per_day - used_today)

    fresh = [(row, kind) for row, kind in changes if (row["base_asset"], kind) not in seen]
    batch = fresh[:room]
    if not batch:
        reason = "deduplicated" if not fresh else "daily_cap"
        return {"candidates": len(changes), "sent": 0, "delivered": False, "reason": reason}

    message = format_message(ts, batch)
    delivered = telegram.send_message(message)
    now = utc_now_iso()
    upsert(
        db,
        "pulse_alert",
        [
            {
                "ts_utc": ts,
                "base_asset": row["base_asset"],
                "kind": kind,
                "message": _line(row, kind),
                "delivered": 1 if delivered else 0,
                "created_at_utc": now,
            }
            for row, kind in batch
        ],
    )
    log.info("pulse_alerts", sent=len(batch), delivered=delivered, capped=len(fresh) - len(batch))
    return {
        "candidates": len(changes),
        "sent": len(batch),
        "delivered": bool(delivered),
        "reason": "sent" if delivered else "delivery_failed",
    }


__all__ = ["KIND_ALIGNED", "KIND_BEAR_BREAK", "detect_changes", "format_message", "send_alerts"]
