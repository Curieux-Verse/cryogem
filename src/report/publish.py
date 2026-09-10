"""
# WHY: ------------------------------------------------------------------------
# Serialises the database into data/public/*.json for the static dashboard
# (spec 16.4).
#
# The dashboard is a public GitHub Pages bundle. It therefore has NO API client
# and NO key: everything it displays is baked here, inside a job that already
# holds the secrets, and only the derived numbers cross the boundary. If a page
# ever needs something this module does not emit, the fix is to emit it here --
# never to fetch it from the browser (spec 16.4.1).
#
# Two invariants this module enforces in code rather than trusting to review:
#
#   1. NO SECRET LEAVES. Every payload is scanned for key-shaped strings before
#      it is written, and the write is refused on a match. CI greps the built
#      bundle too, but by then the value is already in a commit, and a public
#      repo makes that permanent. The cheap check belongs at the earliest point.
#
#   2. EVERY NUMBER IS TRACEABLE to a stored row (spec 16.4.4). This module
#      reshapes; it does not compute. The one exception is documented at
#      _shadow_ranking, and it can never promote a disqualified asset.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from src.config import get_config
from src.db.connection import Database, get_db
from src.db.writes import json_load
from src.logging_setup import get_logger
from src.report import daily
from src.timeutil import add_days, today_utc, utc_now_iso

log = get_logger("report.publish")

#: Bumped when a payload's shape changes incompatibly. The dashboard reads it
#: and refuses to render a version it does not understand, rather than
#: rendering blanks and looking like a data outage.
SCHEMA_VERSION = 1

#: Patterns that must never appear in published JSON. Deliberately broad: a
#: false positive costs one investigation, a false negative costs a rotated
#: credential and a rewritten history.
SECRET_PATTERNS = (
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"hc-ping\.com/[0-9a-f-]{36}"),
    re.compile(r"libsql://[^\"\s]+\?authToken=", re.I),
    re.compile(r"\bey[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"(?:api[_-]?key|auth[_-]?token|bot_token)\"?\s*[:=]\s*\"?[A-Za-z0-9_\-]{16,}", re.I),
    re.compile(r"\bbot\d{8,}:[A-Za-z0-9_-]{30,}"),
)


class SecretLeak(RuntimeError):
    """Raised instead of writing a payload that contains a key-shaped string."""


def _assert_no_secrets(name: str, text: str) -> None:
    for pattern in SECRET_PATTERNS:
        match = pattern.search(text)
        if match:
            # The matched text is NOT logged. Logging it would move the secret
            # from a file that was never written into a log that will be.
            raise SecretLeak(
                f"{name}: refusing to publish -- content matches {pattern.pattern!r} "
                f"at offset {match.start()}. Nothing was written."
            )


NON_FINITE = {float("inf"): "Infinity", float("-inf"): "-Infinity"}


def _json_safe(value: Any) -> Any:
    """Replace non-finite floats with strings, recursively.

    `json.dumps` emits a BARE `Infinity` by default, which is not JSON:
    `JSON.parse` throws on it and the page fails to load with no clue why.
    This surfaced on every orphan perp, because an orphan's perp/spot ratio is
    deliberately `float("inf")` rather than `None` -- a perp with no spot pair
    has an infinite ratio, and recording that as "missing" would let it pass a
    check it should fail. So the value is real and must survive the boundary:
    it crosses as the STRING "Infinity", which is valid JSON and forces the
    consumer to handle it rather than silently comparing against null.

    `allow_nan=False` in the writer then makes any case this misses raise,
    instead of shipping a file no browser can read.
    """
    if isinstance(value, float):
        if value != value:  # NaN
            return None
        return NON_FINITE.get(value, value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _write(out_dir: Path, name: str, payload: Any) -> tuple[str, int]:
    text = json.dumps(
        _json_safe(payload),
        indent=None,
        separators=(",", ":"),
        sort_keys=False,
        allow_nan=False,
    )
    _assert_no_secrets(name, text)
    path = out_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return name, len(text.encode("utf-8"))


# ==============================================================================
# latest.json -- the Screen page
# ==============================================================================
def build_latest(db: Database, run_date: str, payload: dict[str, Any]) -> dict[str, Any]:
    """The funnel and the ranked table.

    `generated_at_utc` is what the dashboard's staleness banner reads. It is
    the publish time, not the run date: a run that succeeded at 03:10 and a
    publish that failed until 19:00 are different situations, and the reader
    needs to see the second one.
    """
    ranked = []
    for row in payload["ranked"]:
        percentiles = json_load(row["percentiles"], {}) or {}
        ranked.append(
            {
                "rank": row["rank"],
                "asset": row["base_asset"],
                "score": row["total_score"],
                "blocks": {
                    "fundamental": row["score_fundamental"],
                    "supply": row["score_supply"],
                    "sector": row["score_sector"],
                    "events": row["score_events"],
                    "attention": row["score_attention"],
                    "drawdown": row["score_drawdown"],
                },
                "flags": daily.flags_for(percentiles),
                "sector": get_config().sectors.sector_of(row["base_asset"]),
                # The detail file this row links to. Not derivable in the
                # browser: see _safe_name.
                "file": f"assets/{_safe_name(row['base_asset'])}.json",
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "run_date": run_date,
        "generated_at_utc": utc_now_iso(),
        "funnel": {
            "universe": payload["universe"],
            "disqualified": payload["universe"] - payload["survivors"],
            "survivors": payload["survivors"],
            "ranked": len(payload["ranked"]),
            "reported": min(payload["report_top_n"], len(payload["ranked"])),
            "survival_rate": round(payload["survival_rate"], 4),
        },
        "regime": payload["regime"],
        # Named explicitly so the dashboard can show, per check, that it was
        # inoperative. A survivor is not "clean" on a dark check.
        "dark_checks": payload["dark_checks"],
        "ranked": ranked,
        "report_top_n": payload["report_top_n"],
    }


# ==============================================================================
# rejected.json -- the disqualification wall
# ==============================================================================
def build_rejected(payload: dict[str, Any]) -> dict[str, Any]:
    """Everything that failed L1, grouped by the check that killed it.

    Spec 16.3 asks for this sorted by "what its L2 score would have been". That
    number does not exist and this module will not invent it: Layer 2 is a
    CROSS-SECTIONAL ranking of survivors, so producing a score for a
    disqualified asset means either scoring it against a universe it is not in,
    or admitting it to the cross-section and thereby changing every survivor's
    percentile. Both are worse than not answering.
    ,
    Ordering is by market cap instead, which serves the page's actual purpose:
    the biggest name the filter removed is the one the reader is most likely to
    believe it got wrong, and therefore the row that most needs to show its
    number. Recorded as D-011.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in payload["failures"]:
        failed = json_load(row["failed_checks"], []) or []
        values = json_load(row["check_values"], {}) or {}
        mcap = (values.get("L1_MCAP") or {}).get("value")
        entry = {
            "asset": row["base_asset"],
            "file": f"assets/{_safe_name(row['base_asset'])}.json",
            "market_cap_usd": mcap,
            "failed_checks": failed,
            "checks": {
                check: {
                    "value": values.get(check, {}).get("value"),
                    "value_display": values.get(check, {}).get("value_display"),
                    "threshold": values.get(check, {}).get("threshold"),
                    "reason": values.get(check, {}).get("reason"),
                }
                for check in failed
            },
        }
        for check in failed:
            groups.setdefault(check, []).append(entry)

    for check, rows in groups.items():
        rows.sort(key=lambda r: -(r["market_cap_usd"] or 0.0))

    return {
        "schema_version": SCHEMA_VERSION,
        "run_date": payload["run_date"],
        "generated_at_utc": utc_now_iso(),
        "total_disqualified": len(payload["failures"]),
        "ordering": "market_cap_desc",
        "groups": [
            {
                "check_id": check,
                "count": len(rows),
                "description": _check_description(check),
                "assets": rows,
            }
            for check, rows in sorted(groups.items(), key=lambda kv: -len(kv[1]))
        ],
    }


def _check_description(check_id: str) -> str:
    from src.screening.layer1_kill import CHECK_DESCRIPTIONS

    return CHECK_DESCRIPTIONS.get(check_id, "")


# ==============================================================================
# assets/<TICKER>.json -- the detail page
# ==============================================================================
def build_asset(
    db: Database, run_date: str, base_asset: str, l1_row: dict[str, Any]
) -> dict[str, Any]:
    """One asset's full evidence: nine checks, six blocks, events, news.

    Written for every asset SCREENED, not only survivors. A disqualified asset
    needs its detail page most: the rejection wall links here, and the argument
    for a rejection is the number, not the red mark.
    """
    cfg = get_config()
    checks = json_load(l1_row["check_values"], {}) or {}
    l2 = db.query_one(
        "SELECT total_score, rank, universe_size, percentiles, score_fundamental, "
        "score_supply, score_sector, score_events, score_attention, score_drawdown "
        "FROM layer2_result WHERE run_date = ? AND base_asset = ?",
        (run_date, base_asset),
    )
    l3 = db.query_one(
        "SELECT setup_detected, setup_type, invalidation_price, trendline_touches, "
        "break_confirmed, retest_confirmed, fvg_count, order_block_count, "
        "risk_flags, depth_2pct_usd, funding_pctile, oi_change_pctile "
        "FROM layer3_result WHERE run_date = ? AND base_asset = ?",
        (run_date, base_asset),
    )
    market = db.query_one(
        "SELECT price_usd, market_cap_usd, fdv_usd, spot_volume_24h_usd, "
        "circulating_supply, total_supply, max_supply, ath_usd, ath_date, "
        "pct_below_ath, price_change_24h_pct "
        "FROM market_snapshot WHERE base_asset = ? AND snapshot_date <= ? "
        "ORDER BY snapshot_date DESC LIMIT 1",
        (base_asset, run_date),
    )
    price_rows = db.query(
        "SELECT snapshot_date, open_usd, high_usd, low_usd, close_usd "
        "FROM price_daily WHERE base_asset = ? AND snapshot_date BETWEEN ? AND ? "
        "ORDER BY snapshot_date",
        (base_asset, add_days(run_date, -cfg.settings.reporting.history_window_days), run_date),
    )
    # Columnar, not one object per day. These files are COMMITTED daily and the
    # repo keeps every version forever: at 90 days of history across ~530
    # assets, repeating five key names per bar costs roughly 2 MB per day of
    # permanent git history for no added information.
    prices = {
        "columns": ["date", "o", "h", "l", "c"],
        "rows": [
            [r["snapshot_date"], r["open_usd"], r["high_usd"], r["low_usd"], r["close_usd"]]
            for r in price_rows
        ],
    }
    events = db.query(
        "SELECT event_date_utc, event_type, recipient_type, pct_of_circulating, "
        "magnitude_usd, confidence, description FROM scheduled_event "
        "WHERE base_asset = ? AND event_date_utc BETWEEN ? AND ? AND first_seen_utc <= ? "
        "ORDER BY event_date_utc",
        (
            base_asset,
            add_days(run_date, -90),
            add_days(run_date, 90),
            f"{run_date}T23:59:59Z",
        ),
    )
    news = db.query(
        "SELECT news_id, published_at_utc, title, url, source_name, sentiment_label, "
        "sentiment_score, event_type_guess FROM news_item "
        "WHERE published_at_utc >= ? AND published_at_utc <= ? AND assets LIKE ? "
        "ORDER BY published_at_utc DESC LIMIT 30",
        (
            f"{add_days(run_date, -7)}T00:00:00Z",
            f"{run_date}T23:59:59Z",
            f'%"{base_asset}"%',
        ),
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "asset": base_asset,
        "file": f"assets/{_safe_name(base_asset)}.json",
        "run_date": run_date,
        "generated_at_utc": utc_now_iso(),
        "sector": cfg.sectors.sector_of(base_asset),
        "verdict": {
            "passed": bool(l1_row["passed"]),
            "failed_checks": json_load(l1_row["failed_checks"], []) or [],
            # A dark check did not pass; it was not asked. The dashboard shows
            # these differently, which is only possible if we say which.
            "dark_checks": [
                cid for cid, c in checks.items() if c.get("source_unavailable")
            ],
        },
        "checks": checks,
        "layer2": (
            {
                "total_score": l2["total_score"],
                "rank": l2["rank"],
                "universe_size": l2["universe_size"],
                "blocks": {
                    "fundamental": l2["score_fundamental"],
                    "supply": l2["score_supply"],
                    "sector": l2["score_sector"],
                    "events": l2["score_events"],
                    "attention": l2["score_attention"],
                    "drawdown": l2["score_drawdown"],
                },
                "percentiles": json_load(l2["percentiles"], {}) or {},
            }
            if l2
            else None
        ),
        "layer3": (
            {
                **{k: v for k, v in l3.items() if k != "risk_flags"},
                "risk_flags": json_load(l3["risk_flags"], []) or [],
            }
            if l3
            else None
        ),
        "market": market,
        "prices": prices,
        "events": events,
        # Labelled at the boundary, not in the UI: news is context, and a field
        # named "news" next to a field named "score" invites a reader to treat
        # the two as the same kind of thing.
        "news_context": news,
    }


# ==============================================================================
# journal.json -- the receipts
# ==============================================================================
def build_journal(db: Database) -> dict[str, Any]:
    """Statistics per horizon, plus every entry with its outcome.

    Includes the losers. That is the entire point of the page: a forward-return
    distribution with the bad entries removed is not a distribution, it is
    marketing. `forward_return` is append-only precisely so that this list
    cannot be curated after the fact.
    """
    from src.journal import forward_returns as fr

    cfg = get_config()
    horizons = cfg.thresholds.journal.horizons
    stats = {h: fr.compute_statistics(h) for h in horizons}

    entries = db.query(
        "SELECT entry_id, run_date, base_asset, rank, total_score, is_control, "
        "       price_at_signal, btc_price_at_signal FROM journal_entry "
        "ORDER BY run_date DESC, is_control, rank"
    )
    returns = db.query(
        "SELECT entry_id, horizon, return_raw, return_vs_btc, max_favourable, max_adverse "
        "FROM forward_return"
    )
    by_entry: dict[str, dict[str, Any]] = {}
    for row in returns:
        by_entry.setdefault(row["entry_id"], {})[row["horizon"]] = {
            "return_raw": row["return_raw"],
            "return_vs_btc": row["return_vs_btc"],
            "max_favourable": row["max_favourable"],
            "max_adverse": row["max_adverse"],
        }

    first_run = db.scalar("SELECT MIN(run_date) FROM journal_entry")
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": utc_now_iso(),
        "horizons": horizons,
        "statistics": stats,
        "first_entry_date": first_run,
        # The empty state needs a number, not a shrug: "not enough data" is
        # only useful if it says how much is missing.
        "coverage_note": _coverage_note(stats, horizons),
        "entries": [
            {
                **entry,
                "is_control": bool(entry["is_control"]),
                "returns": by_entry.get(entry["entry_id"], {}),
            }
            for entry in entries
        ],
    }


def _coverage_note(stats: dict[str, Any], horizons: list[str]) -> str:
    total = next(iter(stats.values()), {}).get("entries_total", 0) if stats else 0
    if not total:
        return "No journal entries yet. Nothing can be concluded, and nothing is claimed."
    parts = [
        f"{stats[h]['entries_with_returns']} with complete {h} returns" for h in horizons
    ]
    return f"{total} entries, " + ", ".join(parts) + "."


# ==============================================================================
# events.json -- the calendar
# ==============================================================================
def build_events(db: Database, run_date: str, survivors: set[str]) -> dict[str, Any]:
    """90-day forward calendar across survivors, coloured by recipient type.

    Recipient type is the field the unlock research identified as most
    predictive, so it is carried through verbatim -- including the honest
    `null` where no source stated it. A guessed recipient would be worse than
    an unknown one, because the page colours by it.
    """
    rows = db.query(
        "SELECT event_id, base_asset, event_type, event_date_utc, recipient_type, "
        "       magnitude_tokens, magnitude_usd, pct_of_circulating, description, "
        "       source, confidence, first_seen_utc FROM scheduled_event "
        "WHERE event_date_utc BETWEEN ? AND ? AND first_seen_utc <= ? "
        "ORDER BY event_date_utc",
        (run_date, add_days(run_date, 90), f"{run_date}T23:59:59Z"),
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "run_date": run_date,
        "generated_at_utc": utc_now_iso(),
        "window_days": 90,
        "events": [
            {**r, "is_survivor": r["base_asset"] in survivors} for r in rows
        ],
        "note": (
            "The unlock calendar is a manually maintained fallback: every free "
            "unlock API is paywalled. An asset with no rows here has no "
            "RECORDED unlock, which is not the same as having none."
        ),
    }


# ==============================================================================
# health.json -- system status
# ==============================================================================
def build_health(db: Database, run_date: str, quality: dict[str, Any]) -> dict[str, Any]:
    """Is the machine actually running? The page that catches silent death.

    The last-successful-run-per-collector table is the one that matters. A
    collector that stopped three weeks ago leaves no error anywhere: the screen
    still runs, the report still renders, and the data quietly goes stale. This
    is where that shows up.
    """
    last_runs = db.query(
        "SELECT collector_name, MAX(started_at_utc) AS last_run, status, "
        "       rows_written, error_message FROM collector_run "
        "GROUP BY collector_name ORDER BY collector_name"
    )
    recent_failures = db.query(
        "SELECT collector_name, started_at_utc, status, error_message FROM collector_run "
        "WHERE status = 'failed' ORDER BY started_at_utc DESC LIMIT 25"
    )
    lag = db.query(
        "SELECT workflow, actual_start_utc, lag_seconds FROM trigger_lag "
        "ORDER BY actual_start_utc DESC LIMIT 200"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "run_date": run_date,
        "generated_at_utc": utc_now_iso(),
        "collectors": quality["collectors"],
        "last_runs": last_runs,
        "recent_failures": recent_failures,
        "tables": quality["tables"],
        "coverage": quality["coverage"],
        "news_lag_seconds": quality["news_lag_seconds"],
        "trigger_lag_seconds": quality["trigger_lag_seconds"],
        "trigger_lag_recent": lag,
        # Turso meters row READS, not rows stored, so no honest usage figure
        # can be derived from this side of the connection. Saying so beats
        # printing a plausible number that is not the metered one.
        "turso_usage": {
            "available": False,
            "reason": (
                "Turso meters row reads server-side; the client cannot observe "
                "them. Check the Turso dashboard for the metered figure."
            ),
        },
    }


# ==============================================================================
# history.json -- the funnel over time
# ==============================================================================
def build_history(db: Database, run_date: str) -> dict[str, Any]:
    """Survival rate and universe size per day, over the reporting window.

    A survival rate that drifts is the earliest visible sign of either a
    changing market or a threshold that no longer means what it did. Neither is
    detectable from a single day.
    """
    window = get_config().settings.reporting.history_window_days
    rows = db.query(
        "SELECT run_date, COUNT(*) AS universe, SUM(passed) AS survivors "
        "FROM layer1_result WHERE run_date BETWEEN ? AND ? "
        "GROUP BY run_date ORDER BY run_date",
        (add_days(run_date, -window), run_date),
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": utc_now_iso(),
        "window_days": window,
        "days": [
            {
                "run_date": r["run_date"],
                "universe": r["universe"],
                "survivors": r["survivors"] or 0,
                "survival_rate": (
                    round((r["survivors"] or 0) / r["universe"], 4) if r["universe"] else None
                ),
            }
            for r in rows
        ],
    }


# ==============================================================================
# orchestration
# ==============================================================================
def _safe_name(asset: str) -> str:
    """A filename that is safe AND unique.

    Two separate jobs, and the second one is easy to miss. Tickers arrive from
    an exchange API and are not a trusted path component, so anything outside
    a conservative alphabet is replaced -- but a pure substitution is not
    injective. Binance lists five CJK-named contracts, and every one of them
    sanitised to the same "unknown.json": four assets were overwritten and the
    dashboard would have shown one asset's verdict under five different names.

    So: an already-safe ticker keeps its own name (BTC.json, 1000PEPE.json),
    and anything altered gets a stable hash suffix. The mapping is published in
    manifest.json under `asset_files`, because a name the dashboard cannot
    reverse is a link it cannot build.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", asset).strip("._")
    if cleaned == asset and cleaned:
        return cleaned
    digest = hashlib.sha1(asset.encode("utf-8")).hexdigest()[:10]
    return f"{cleaned or 'asset'}-{digest}"


def _prune_asset_dir(asset_dir: Path, current: set[str]) -> None:
    """Delete asset files for tickers no longer in today's universe.

    Not housekeeping. A stale AAA.json served from Pages is a page showing a
    verdict from an unknown date as if it were today's. Removing it makes the
    dashboard 404, which is honest; the database keeps the history, while this
    directory is a rendering of one specific day.
    """
    if not asset_dir.exists():
        return
    keep = {f"{_safe_name(a)}.json" for a in current}
    for path in asset_dir.glob("*.json"):
        if path.name not in keep:
            path.unlink()


def publish_all(run_date: str | None = None) -> list[tuple[str, int]]:
    """Write every public JSON file. Returns (name, bytes) for each.

    Files are rewritten wholesale rather than merged: a partially-updated set
    would let the dashboard show today's funnel beside yesterday's ranking,
    and the reader would have no way to tell.
    """
    cfg = get_config()
    date = run_date or today_utc()
    out_dir = cfg.path(cfg.settings.reporting.public_json_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[tuple[str, int]] = []

    with get_db() as db:
        payload = daily.gather(db, date)
        survivors = {
            r["base_asset"]
            for r in db.query(
                "SELECT base_asset FROM layer1_result WHERE run_date = ? AND passed = 1",
                (date,),
            )
        }

        written.append(_write(out_dir, "latest.json", build_latest(db, date, payload)))
        written.append(_write(out_dir, "rejected.json", build_rejected(payload)))
        written.append(_write(out_dir, "journal.json", build_journal(db)))
        written.append(_write(out_dir, "events.json", build_events(db, date, survivors)))
        written.append(
            _write(out_dir, "health.json", build_health(db, date, payload["quality"]))
        )
        written.append(_write(out_dir, "history.json", build_history(db, date)))
        core_files = len(written)

        # One small file per asset, so the detail page fetches only what it
        # needs. A single combined file would be roughly a megabyte to read one
        # ticker.
        l1_rows = db.query(
            "SELECT base_asset, passed, failed_checks, check_values FROM layer1_result "
            "WHERE run_date = ?",
            (date,),
        )
        screened = {r["base_asset"] for r in l1_rows}
        _prune_asset_dir(out_dir / "assets", screened)
        for row in l1_rows:
            written.append(
                _write(
                    out_dir,
                    f"assets/{_safe_name(row['base_asset'])}.json",
                    build_asset(db, date, row["base_asset"], row),
                )
            )

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_date": date,
        "generated_at_utc": utc_now_iso(),
        "assets": len(written) - core_files,
        # ticker -> filename, because sanitising is not reversible.
        "asset_files": {
            asset: f"assets/{_safe_name(asset)}.json" for asset in sorted(screened)
        },
        "files": [{"name": name, "bytes": size} for name, size in written],
        "total_bytes": sum(size for _, size in written),
    }
    written.append(_write(out_dir, "manifest.json", manifest))

    log.info(
        "published",
        run_date=date,
        files=len(written),
        assets=manifest["assets"],
        total_bytes=manifest["total_bytes"],
    )
    return written


__all__ = [
    "SCHEMA_VERSION",
    "SecretLeak",
    "build_asset",
    "build_events",
    "build_health",
    "build_history",
    "build_journal",
    "build_latest",
    "build_rejected",
    "publish_all",
]
