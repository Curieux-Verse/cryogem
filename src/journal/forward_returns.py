"""
# WHY: ------------------------------------------------------------------------
# The journal. The ONLY component of this system that produces truth.
# Everything before it produces plausible-looking output.
#
# Its job is to answer one question from the user's own recorded data:
#
#     "Of every coin my screener ranked in the top 15 over the last six months,
#      what was the distribution of 30-day forward returns relative to BTC, and
#      did it beat a random selection from the same surviving universe?"
#
# THE RULES, and they are absolute:
#
#   1. APPEND ONLY. No deletions, no edits, no exclusions -- not even "the one
#      where I'd have known better". That exclusion is the exact mechanism by
#      which every signal channel comes to look profitable. The database
#      enforces it with triggers; this module never even attempts it.
#
#   2. A RANDOM CONTROL is journalled alongside every real signal, drawn from
#      the L1 survivors that did NOT make the ranked list. Without it a
#      positive return says nothing -- everything may simply have gone up. The
#      control is what turns the journal from a diary into an experiment.
#
#   3. MEDIAN AND MEAN, ALWAYS BOTH. In the unlock study the mean was -8.10%
#      while the median was -16.26%: the mean was dragged by a few outliers
#      while the median described the typical experience. Results here will
#      show the same asymmetry in the other direction. A strategy whose mean is
#      positive and median negative is a lottery-ticket strategy -- legitimate,
#      but it must be SIZED as one, and that is invisible unless both numbers
#      are on the page.
#
#   4. MAX FAVOURABLE AND MAX ADVERSE EXCURSION on every horizon. A +20%
#      30-day return that first drew down 40% is not a winning trade, it is a
#      trade that stopped you out before it worked. Endpoint returns hide that.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

import random
import statistics
from typing import Any

from src.config import get_config
from src.db.connection import Database, get_db
from src.db.writes import deterministic_id, json_dump, json_load, upsert
from src.logging_setup import get_logger
from src.timeutil import add_days, horizon_to_days, today_utc, utc_now_iso

log = get_logger("journal")

BTC = "BTC"

#: The one price series returns are measured on. CoinGecko also writes
#: price_daily, but at collection time (~03:10) and keyed on its own ticker,
#: which is not always the Binance token of that name. Klines are keyed by the
#: Binance contract and are the only series the backtest can rebuild, so the
#: journal and the harness read the same bars and agree by construction (D-047).
PRICE_SOURCE = "binance_klines"


def _prices_on(db: Database, run_date: str) -> dict[str, float]:
    """Closing prices on the most recent day at or before `run_date`."""
    snapshot_date = db.scalar(
        "SELECT MAX(snapshot_date) FROM price_daily WHERE snapshot_date <= ?", (run_date,)
    )
    if not snapshot_date:
        return {}
    rows = db.query(
        "SELECT base_asset, close_usd FROM price_daily WHERE snapshot_date = ?", (snapshot_date,)
    )
    return {r["base_asset"]: r["close_usd"] for r in rows if r["close_usd"]}


def _news_context(db: Database, asset: str, run_date: str) -> list[str]:
    """news_ids mentioning this asset within +/-24h of the signal.

    Labelling only. After six months this answers whether the system's signals
    LEAD news or FOLLOW it. If they consistently follow, the design is a
    lagging indicator and needs rethinking -- a finding worth more than any
    alert, and it costs nothing extra to collect.
    """
    rows = db.query(
        "SELECT news_id, assets FROM news_item WHERE published_at_utc BETWEEN ? AND ?",
        (f"{add_days(run_date, -1)}T00:00:00Z", f"{add_days(run_date, 1)}T23:59:59Z"),
    )
    return [r["news_id"] for r in rows if asset in (json_load(r["assets"], []) or [])]


def _events_context(db: Database, asset: str, run_date: str) -> list[str]:
    """event_ids for this asset within +/-30d, knowable as of the run date."""
    rows = db.query(
        "SELECT event_id FROM scheduled_event WHERE base_asset = ? "
        "AND event_date_utc BETWEEN ? AND ? AND first_seen_utc <= ?",
        (asset, add_days(run_date, -30), add_days(run_date, 30), f"{run_date}T23:59:59Z"),
    )
    return [r["event_id"] for r in rows]


def _draw_controls(
    db: Database, run_date: str, chosen: list[str], seed: int | None = None
) -> list[str]:
    """Random L1 survivors that did NOT make the ranked list.

    This comparison is what makes the journal an experiment rather than a
    diary. "My picks returned +8%" is unreadable on its own; the honest
    question is whether they beat a coin drawn at random from the same pool.
    """
    survivors = [
        r["base_asset"]
        for r in db.query(
            # Sorted: a seeded sample of an unordered result depends on the
            # order the backend returns rows in, which differs between sqlite
            # and Turso and changes with an index (D-062).
            "SELECT base_asset FROM layer1_result WHERE run_date = ? AND passed = 1 "
            "ORDER BY base_asset",
            (run_date,),
        )
    ]
    pool = [a for a in survivors if a not in set(chosen)]
    if not pool:
        return []
    rng = random.Random(seed if seed is not None else run_date)
    return rng.sample(pool, k=min(len(chosen), len(pool)))


def draw_controls(db: Database, run_date: str, seed: int | None = None) -> list[str]:
    """The control group for a run date: the journalled one if it exists (D-062).

    Control ids are per asset, so re-running a day whose survivor pool had
    changed inserted a second, different control group beside the first. And
    the harness drew its own controls from a different pool (excluding its top
    15, not the journal's top 25), so "agree by construction" was not true.
    Both now come through here.
    """
    recorded = [
        r["base_asset"]
        for r in db.query(
            "SELECT base_asset FROM journal_entry WHERE run_date = ? AND is_control = 1 "
            "ORDER BY base_asset",
            (run_date,),
        )
    ]
    if recorded:
        return recorded
    top_n = get_config().thresholds.journal.top_n_to_journal
    chosen = [
        r["base_asset"]
        for r in db.query(
            "SELECT base_asset FROM layer2_result WHERE run_date = ? ORDER BY rank LIMIT ?",
            (run_date, top_n),
        )
    ]
    return _draw_controls(db, run_date, chosen, seed)


def entries_on(run_date: str) -> int:
    """Journal entries on file for a run date, signals and controls alike."""
    with get_db() as db:
        return db.scalar("SELECT COUNT(*) FROM journal_entry WHERE run_date = ?", (run_date,)) or 0


def _build_entry(
    db: Database,
    run_date: str,
    ranked: dict[str, Any],
    prices: dict[str, float],
    btc_price: float,
    is_control: bool,
) -> dict[str, Any] | None:
    asset = ranked["base_asset"]
    price = prices.get(asset)
    if not price:
        log.warning("journal_entry_skipped_no_price", asset=asset, run_date=run_date)
        return None

    l1 = db.query_one(
        "SELECT check_values FROM layer1_result WHERE run_date = ? AND base_asset = ?",
        (run_date, asset),
    )
    l2 = db.query_one(
        "SELECT percentiles, score_fundamental, score_supply, score_sector, "
        "score_events, score_attention, score_drawdown FROM layer2_result "
        "WHERE run_date = ? AND base_asset = ?",
        (run_date, asset),
    )
    l3 = db.query_one(
        "SELECT setup_detected, setup_type, invalidation_price, risk_flags "
        "FROM layer3_result WHERE run_date = ? AND base_asset = ?",
        (run_date, asset),
    )

    kind = "control" if is_control else "signal"

    # A control is constructed with an explicit rank 0 / score 0.0 sentinel
    # by its caller: it was never ranked, and that is the point of it. A
    # SIGNAL without a score is a different thing entirely -- a defect
    # upstream -- and coercing it to 0.0 would write a fabricated number into
    # an append-only table that by design can never be corrected. Refuse.
    score = ranked.get("total_score")
    if not is_control and score is None:
        log.error(
            "journal_skipped_no_score",
            run_date=run_date,
            base_asset=asset,
            reason="ranked row carried no total_score; refusing to invent one",
        )
        return None

    return {
        # Deterministic id: re-running a day cannot duplicate an entry, and
        # INSERT OR IGNORE preserves the original row.
        "entry_id": deterministic_id("journal", run_date, asset, kind),
        "run_date": run_date,
        "base_asset": asset,
        "rank": int(ranked.get("rank") or 0),
        "total_score": float(score if score is not None else 0.0),
        # The price the screen saw at run time, kept as context. Returns are
        # NOT measured from it: see entry_close and D-047.
        "price_at_signal": price,
        "btc_price_at_signal": btc_price,
        "is_control": 1 if is_control else 0,
        "layer1_values": (l1 or {}).get("check_values"),
        "layer2_values": json_dump(l2) if l2 else None,
        "layer3_values": json_dump(l3) if l3 else None,
        "news_context": json_dump(_news_context(db, asset, run_date)),
        "events_context": json_dump(_events_context(db, asset, run_date)),
        "created_at_utc": utc_now_iso(),
    }


def write_entries(run_date: str | None = None, seed: int | None = None) -> int:
    """Journal today's ranked assets, plus a random control group.

    Idempotent: entry_id is a deterministic hash, so re-running a day adds
    nothing and cannot overwrite what is there.
    """
    date = run_date or today_utc()
    top_n = get_config().thresholds.journal.top_n_to_journal

    with get_db() as db:
        ranked = db.query(
            "SELECT base_asset, rank, total_score FROM layer2_result "
            "WHERE run_date = ? ORDER BY rank LIMIT ?",
            (date, top_n),
        )
        if not ranked:
            log.warning("journal_no_ranked_assets", run_date=date)
            return 0

        prices = _prices_on(db, date)
        btc_price = prices.get(BTC)
        if not btc_price:
            # Every return is measured against BTC. Without it the entries
            # would be unusable, and a journal row cannot be corrected after
            # the fact -- it is append-only by design. Better to write nothing.
            log.error("journal_skipped_no_btc_price", run_date=date)
            return 0

        chosen = [r["base_asset"] for r in ranked]
        controls = draw_controls(db, date, seed)

        rows: list[dict[str, Any]] = []
        for entry in ranked:
            row = _build_entry(db, date, entry, prices, btc_price, is_control=False)
            if row:
                rows.append(row)
        for asset in controls:
            row = _build_entry(
                db,
                date,
                {"base_asset": asset, "rank": 0, "total_score": 0.0},
                prices,
                btc_price,
                is_control=True,
            )
            if row:
                rows.append(row)

        written = upsert(db, "journal_entry", rows)

    log.info(
        "journal_written", run_date=date, signals=len(ranked), controls=len(controls), rows=written
    )
    return written


def backfill_returns(as_of: str | None = None) -> int:
    """Compute forward returns for every entry whose horizon has now elapsed.

    Finds entries missing a `forward_return` row for a horizon that has passed,
    looks up the price then and now, and writes the result. Runs daily; each
    entry is filled once per horizon and never revisited.
    """
    date = as_of or today_utc()
    horizons = get_config().thresholds.journal.horizons
    filled = 0

    with get_db() as db:
        for horizon in horizons:
            days = horizon_to_days(horizon)
            # Entries old enough for this horizon that do not yet have a row.
            pending = db.query(
                "SELECT e.entry_id, e.run_date, e.base_asset, e.price_at_signal, "
                "       e.btc_price_at_signal "
                "FROM journal_entry e "
                "LEFT JOIN forward_return f "
                "  ON f.entry_id = e.entry_id AND f.horizon = ? "
                "WHERE f.entry_id IS NULL AND e.run_date <= ?",
                (horizon, add_days(date, -days)),
            )
            if not pending:
                continue

            rows: list[dict[str, Any]] = []
            for entry in pending:
                asset, run_date = entry["base_asset"], entry["run_date"]
                asset_then = entry_close(db, asset, run_date)
                btc_then = entry_close(db, BTC, run_date)
                closing = exit_bar(db, asset, run_date, days)
                # BTC over the holding period the position actually had: a
                # delisted asset exits early, and BTC exits with it (D-061).
                btc_now = close_on(db, BTC, closing[0], run_date) if closing else None
                if not asset_then or not btc_then or closing is None or btc_now is None:
                    # A bar is missing. Leave it pending rather than writing a
                    # fabricated number -- the row is append-only and a wrong
                    # value could never be corrected.
                    continue

                exit_date, asset_now, exit_reason = closing
                raw = (asset_now - asset_then) / asset_then
                btc_return = (btc_now - btc_then) / btc_then
                excursion = _excursion(db, asset, add_days(run_date, 1), exit_date, asset_then)

                rows.append(
                    {
                        "entry_id": entry["entry_id"],
                        "horizon": horizon,
                        "entry_price": asset_then,
                        "price_source": PRICE_SOURCE,
                        "exit_reason": exit_reason,
                        "price_at_horizon": asset_now,
                        "return_raw": round(raw, 6),
                        # Relative to BTC: the only number that distinguishes
                        # a good pick from a rising tide.
                        "return_vs_btc": round(raw - btc_return, 6),
                        "max_favourable": excursion["max_favourable"],
                        "max_adverse": excursion["max_adverse"],
                        "computed_at_utc": utc_now_iso(),
                    }
                )
            filled += upsert(db, "forward_return", rows)

    log.info("forward_returns_backfilled", filled=filled, as_of=date)
    return filled


def entry_close(db: Database, asset: str, run_date: str) -> float | None:
    """The kline close of the signal day itself.

    The screen runs at ~03:10 on the signal day, on data collected minutes
    earlier, so that day's close is the first price strictly after everything
    the ranking knew. There is no fallback to an earlier day: an earlier close
    precedes information the ranking used, which is look-ahead in the entry.
    """
    return db.scalar(
        "SELECT close_usd FROM price_daily WHERE base_asset = ? AND snapshot_date = ? "
        "AND source = ?",
        (asset, run_date, PRICE_SOURCE),
    )


def horizon_close(db: Database, asset: str, run_date: str, days: int) -> float | None:
    """The kline close `days` after the signal day, tolerating a missed week."""
    target = add_days(run_date, days)
    # Never back to (or before) the signal day -- see _price_at.
    earliest = max(add_days(target, -7), add_days(run_date, 1))
    return _price_at(db, asset, target, earliest)


def _price_at(db: Database, asset: str, date: str, earliest: str) -> float | None:
    """Closing price on `date`, or the nearest earlier day back to `earliest`.

    Two bounds, and both are load-bearing:

      * A price from three weeks ago is not "the price at the 30-day horizon",
        so the lookback is short (the caller allows a week, to tolerate a
        missed collection day).

      * The lookback must never reach the signal day itself. Caught by
        test_missing_future_price_leaves_the_row_pending: with a flat week of
        lookback, the 1d horizon resolves to the price ON the signal day and
        every 1d return is exactly 0.00%. That is not a missing number, which
        would be obvious -- it is a plausible-looking one, which is worse. The
        caller therefore passes earliest = run_date + 1 day at minimum.
    """
    bar = _bar_at(db, asset, date, earliest)
    return bar[1] if bar else None


def _bar_at(db: Database, asset: str, date: str, earliest: str) -> tuple[str, float] | None:
    """(date, close) of the newest kline bar in [earliest, date]."""
    if earliest > date:
        return None
    row = db.query_one(
        "SELECT snapshot_date, close_usd FROM price_daily WHERE base_asset = ? "
        "AND snapshot_date <= ? AND snapshot_date >= ? AND source = ? "
        "ORDER BY snapshot_date DESC LIMIT 1",
        (asset, date, earliest, PRICE_SOURCE),
    )
    return (row["snapshot_date"], row["close_usd"]) if row else None


def close_on(db: Database, asset: str, date: str, run_date: str) -> float | None:
    """The close on `date`, or up to a week before it, never back to the signal day."""
    return _price_at(db, asset, date, max(add_days(date, -7), add_days(run_date, 1)))


def exit_bar(
    db: Database, asset: str, run_date: str, days: int
) -> tuple[str, float, str] | None:
    """(date, close, reason) for a position opened on `run_date`; None while pending.

    At the horizon when a bar exists there ('horizon'). When the asset has
    stopped trading -- a recent Binance universe snapshot no longer lists it --
    at its last close after the signal ('delisted', D-061). Before, a delisted
    asset had no horizon price and stayed pending forever, so the worst outcomes
    in the system never reached its statistics.
    """
    target = add_days(run_date, days)
    bar = _bar_at(db, asset, target, max(add_days(target, -7), add_days(run_date, 1)))
    if bar is not None:
        return bar[0], bar[1], "horizon"
    if not _stopped_trading(db, asset, target):
        return None
    last = _bar_at(db, asset, target, add_days(run_date, 1))
    return (last[0], last[1], "delisted") if last else None


def _stopped_trading(db: Database, asset: str, target: str) -> bool:
    """True when a Binance universe snapshot from the week before `target` omits the asset.

    A missing or stale snapshot is an outage of ours, not a delisting, and
    answers False: the row stays pending rather than closing on a guess.
    """
    latest = db.scalar(
        "SELECT MAX(snapshot_date) FROM universe_snapshot "
        "WHERE exchange = 'binance' AND snapshot_date <= ?",
        (target,),
    )
    if not latest or latest < add_days(target, -7):
        return False
    listed = db.scalar(
        "SELECT COUNT(*) FROM universe_snapshot WHERE exchange = 'binance' "
        "AND snapshot_date = ? AND base_asset = ? AND status = 'TRADING'",
        (latest, asset),
    )
    return not listed


def _excursion(
    db: Database, asset: str, start: str, end: str, entry_price: float
) -> dict[str, float | None]:
    """Best and worst points reached inside the window.

    This is what tells the user whether a stop would have been hit before the
    target. A +20% endpoint return that first drew down 40% is not a winning
    trade, and the endpoint alone cannot show that.

    The caller starts the window the day AFTER the signal: the entry is that
    day's close, so its intraday high and low happened before the position did.
    """
    rows = db.query(
        "SELECT close_usd, high_usd, low_usd FROM price_daily "
        "WHERE base_asset = ? AND snapshot_date BETWEEN ? AND ? AND source = ?",
        (asset, start, end, PRICE_SOURCE),
    )
    if not rows:
        return {"max_favourable": None, "max_adverse": None}

    # high/low are not populated by every source; fall back to close, which
    # understates both excursions. Understating is the honest direction here.
    highs = [r["high_usd"] or r["close_usd"] for r in rows if (r["high_usd"] or r["close_usd"])]
    lows = [r["low_usd"] or r["close_usd"] for r in rows if (r["low_usd"] or r["close_usd"])]
    if not highs or not lows:
        return {"max_favourable": None, "max_adverse": None}

    return {
        "max_favourable": round((max(highs) - entry_price) / entry_price, 6),
        "max_adverse": round((min(lows) - entry_price) / entry_price, 6),
    }


def compute_statistics(horizon: str = "30d") -> dict[str, Any]:
    """Signal vs control statistics for one horizon.

    Reports median AND mean separately, always. They diverge, and the
    divergence IS the finding: a strategy whose mean is positive and median
    negative is a lottery-ticket strategy and must be sized as one.
    """
    with get_db() as db:
        rows = db.query(
            "SELECT e.is_control, e.base_asset, e.run_date, e.rank, "
            "       f.return_raw, f.return_vs_btc, f.max_favourable, f.max_adverse "
            "FROM journal_entry e JOIN forward_return f ON f.entry_id = e.entry_id "
            "WHERE f.horizon = ?",
            (horizon,),
        )
        total_entries = db.scalar("SELECT COUNT(*) FROM journal_entry") or 0

    signals = [r for r in rows if not r["is_control"]]
    controls = [r for r in rows if r["is_control"]]

    return {
        "horizon": horizon,
        "entries_total": total_entries,
        "entries_with_returns": len(rows),
        "signal": _describe(signals),
        "control": _describe(controls),
        "edge_vs_control": _edge(signals, controls),
    }


def _describe(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Both central tendencies, plus the shape of the distribution."""
    if not rows:
        return {"n": 0}

    raw = [r["return_raw"] for r in rows if r["return_raw"] is not None]
    vs_btc = [r["return_vs_btc"] for r in rows if r["return_vs_btc"] is not None]
    adverse = [r["max_adverse"] for r in rows if r["max_adverse"] is not None]
    favourable = [r["max_favourable"] for r in rows if r["max_favourable"] is not None]

    def stats(values: list[float]) -> dict[str, float | None]:
        if not values:
            return {"mean": None, "median": None}
        return {
            "mean": round(statistics.fmean(values), 4),
            "median": round(statistics.median(values), 4),
            "stdev": round(statistics.stdev(values), 4) if len(values) > 1 else None,
            "min": round(min(values), 4),
            "max": round(max(values), 4),
        }

    return {
        "n": len(rows),
        "raw": stats(raw),
        "vs_btc": stats(vs_btc),
        # Hit rate is measured against BTC, not against zero. Beating zero in a
        # bull market is not evidence of anything.
        "hit_rate_vs_btc": (
            round(sum(1 for v in vs_btc if v > 0) / len(vs_btc), 4) if vs_btc else None
        ),
        "max_adverse": stats(adverse),
        "max_favourable": stats(favourable),
    }


def _edge(signals: list[dict], controls: list[dict]) -> dict[str, Any]:
    """Did the ranked picks beat a random survivor? The only question that matters."""
    sig = [r["return_vs_btc"] for r in signals if r["return_vs_btc"] is not None]
    ctl = [r["return_vs_btc"] for r in controls if r["return_vs_btc"] is not None]
    if not sig or not ctl:
        return {"available": False, "reason": "not enough paired data yet"}
    return {
        "available": True,
        "median_difference": round(statistics.median(sig) - statistics.median(ctl), 4),
        "mean_difference": round(statistics.fmean(sig) - statistics.fmean(ctl), 4),
        "signal_n": len(sig),
        "control_n": len(ctl),
    }


def render_report() -> str:
    """Human-readable journal statistics, for the CLI.

    Deliberately refuses to flatter the system: if there is not enough data to
    draw a conclusion, it says so and shows how far off it is, rather than
    printing an encouraging partial number.
    """
    cfg = get_config()
    lines = ["# Journal", ""]

    for horizon in cfg.thresholds.journal.horizons:
        stats = compute_statistics(horizon)
        lines.append(f"## Horizon {horizon}")
        signal = stats["signal"]
        control = stats["control"]

        lines.append(
            f"entries: {stats['entries_total']} total, "
            f"{stats['entries_with_returns']} with complete {horizon} returns"
        )

        if not signal.get("n"):
            lines.append("")
            lines.append(
                f"Not enough data yet at {horizon}. No conclusion can be drawn, and a "
                "partial number here would be misleading rather than encouraging."
            )
            lines.append("")
            continue

        lines.append("")
        lines.append("| group | n | median vs BTC | mean vs BTC | hit rate | worst drawdown |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for label, group in (("signal", signal), ("control", control)):
            if not group.get("n"):
                lines.append(f"| {label} | 0 | - | - | - | - |")
                continue
            lines.append(
                f"| {label} | {group['n']} | "
                f"{_pct(group['vs_btc']['median'])} | {_pct(group['vs_btc']['mean'])} | "
                f"{_pct(group['hit_rate_vs_btc'], of_one=True)} | "
                f"{_pct((group.get('max_adverse') or {}).get('min'))} |"
            )

        edge = stats["edge_vs_control"]
        lines.append("")
        if edge.get("available"):
            lines.append(
                f"**Edge over a random L1 survivor:** median "
                f"{_pct(edge['median_difference'])}, mean {_pct(edge['mean_difference'])}."
            )
            median = signal["vs_btc"]["median"]
            mean = signal["vs_btc"]["mean"]
            if median is not None and mean is not None and mean > 0 > median:
                lines.append("")
                lines.append(
                    "> **Mean positive, median negative.** This is a lottery-ticket "
                    "distribution: a few large winners carry the average while the typical "
                    "outcome is a loss. Legitimate, but it has to be sized as one."
                )
        else:
            lines.append(f"_Edge vs control: {edge.get('reason')}_")
        lines.append("")

    return "\n".join(lines)


def _pct(value: float | None, of_one: bool = False) -> str:
    if value is None:
        return "-"
    return f"{value * 100:+.1f}%" if not of_one else f"{value * 100:.0f}%"


__all__ = [
    "PRICE_SOURCE",
    "backfill_returns",
    "close_on",
    "compute_statistics",
    "draw_controls",
    "entries_on",
    "entry_close",
    "exit_bar",
    "horizon_close",
    "render_report",
    "write_entries",
]
