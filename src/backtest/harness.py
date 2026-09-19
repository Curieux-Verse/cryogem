"""
# WHY: ------------------------------------------------------------------------
# The backtest harness (spec 13).
#
# BUILD IT EARLY, RUN IT LATE. Before roughly six months of recorded screening
# days exist, every number here is noise that looks like signal, so
# `assert_runnable` refuses rather than obliges. That refusal is the most
# valuable function in the module.
#
# The four rules that make the difference between a backtest and a
# self-portrait:
#
#   1. IT REPLAYS RECORDED RANKINGS. It does NOT re-run L1 and L2 over historic
#      dates using today's data. Fundamentals, holder concentration and unlock
#      calendars are all revised after the fact; re-screening 2026-03-01 with
#      the 2026-09-01 version of those tables would score assets on information
#      that did not exist. So the harness reads `layer2_result` rows that were
#      written on the day, and a date with no recorded ranking is a date the
#      backtest skips.
#
#   2. COSTS ARE REAL. Taker fee both ways, funding accrued while held, and
#      slippage sized against RECORDED depth rather than the printed price. If
#      the cost drag comes out near zero, the model is not being applied --
#      that is an acceptance criterion, not a nice-to-have.
#
#   3. EVERY SIGNAL, AND BOTH CENTRAL TENDENCIES. No filtering, ever, and
#      median beside mean because they diverge and the divergence is the
#      finding.
#
#   4. THE HOLDOUT IS ONE SHOT. Develop on the first two thirds, test once on
#      the final third. Because "once" is a promise no code can keep by asking
#      politely, every holdout run is RECORDED, and a second one is reported as
#      what it is: no longer a holdout.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

import math
import statistics
import uuid
from dataclasses import dataclass, field
from typing import Any

from src.backtest import universe as bt_universe
from src.config import get_config
from src.db.connection import Database, get_db
from src.db.writes import upsert
from src.journal import forward_returns as journal_prices
from src.logging_setup import get_logger
from src.timeutil import add_days, days_between, horizon_to_days, utc_now_iso

log = get_logger("backtest.harness")

BTC = "BTC"

#: Recorded screening days required before the harness will report anything.
#: ~6 months of daily runs. Below this the sample cannot separate the strategy
#: from the market it ran in.
MIN_SCREENING_DAYS = 120

#: Distinct signals required. A hundred days that all ranked the same fifteen
#: assets is not a hundred independent observations.
MIN_SIGNALS = 200


class InsufficientHistory(RuntimeError):
    """Raised instead of returning a number the data cannot support."""


@dataclass
class Trade:
    """One signal held for one horizon, with its costs applied."""

    run_date: str
    base_asset: str
    rank: int
    is_control: bool
    entry_price: float
    exit_price: float
    btc_entry: float
    btc_exit: float
    gross_return: float
    fee_cost: float
    funding_cost: float
    slippage_cost: float
    net_return: float
    net_return_vs_btc: float
    max_adverse: float | None = None
    max_favourable: float | None = None
    regime: str | None = None
    blocks: dict[str, float | None] = field(default_factory=dict)
    #: 'horizon', or 'delisted' when the asset stopped trading first (D-061).
    exit_reason: str = "horizon"

    @property
    def total_cost(self) -> float:
        return self.fee_cost + self.funding_cost + self.slippage_cost


# ==============================================================================
# Gating
# ==============================================================================
def history(db: Database) -> dict[str, Any]:
    """What recorded history actually exists. Read this before anything else."""
    dates = [
        r["run_date"]
        for r in db.query("SELECT DISTINCT run_date FROM layer2_result ORDER BY run_date")
    ]
    # Rankings that would have been reported, not every scored row: one day of
    # ~200 survivors met a 200-signal threshold on its own (D-063).
    signals = (
        db.scalar(
            "SELECT COUNT(*) FROM layer2_result WHERE rank <= ?",
            (get_config().thresholds.journal.report_top_n,),
        )
        or 0
    )
    return {
        "screening_days": len(dates),
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
        "recorded_rankings": signals,
        "days_required": MIN_SCREENING_DAYS,
        "signals_required": MIN_SIGNALS,
    }


def assert_runnable(db: Database) -> dict[str, Any]:
    """Refuse to backtest data that cannot support a conclusion.

    This is deliberately a hard failure and not a warning. A warning printed
    above a plausible-looking Sharpe ratio is a warning nobody reads, and the
    number is then quoted without it.
    """
    state = history(db)
    if state["screening_days"] < MIN_SCREENING_DAYS or state["recorded_rankings"] < MIN_SIGNALS:
        raise InsufficientHistory(
            f"{state['screening_days']} recorded screening days and "
            f"{state['recorded_rankings']} rankings. This harness needs "
            f"{MIN_SCREENING_DAYS} days and {MIN_SIGNALS} rankings before any "
            "number it produces means anything.\n\n"
            "This is not a bug and there is no flag to bypass it. The screener "
            "has to run daily for about six months first. Until then the only "
            "honest instrument is `journal --report`, which reports what has "
            "been recorded and says how far short it is."
        )
    return state


# ==============================================================================
# Costs
# ==============================================================================
def fee_cost(taker_fee_bps: float) -> float:
    """Taker fee, both ways. Entry and exit are two fills, not one."""
    return 2.0 * (taker_fee_bps / 10_000.0)


def funding_cost(db: Database, base_asset: str, start: str, end: str) -> float:
    """Funding accrued while held, as a share of notional.

    Uses the recorded interval-normalised APR, integrated over the holding
    period. Sign convention: a LONG pays positive funding, so positive funding
    is a cost. Assuming 8h intervals here would misprice every Hyperliquid
    position by 8x -- Hyperliquid funds hourly.
    """
    rows = db.query(
        "SELECT funding_apr FROM derivatives_snapshot WHERE base_asset = ? "
        "AND ts_utc BETWEEN ? AND ? AND funding_apr IS NOT NULL",
        (base_asset, f"{start}T00:00:00Z", f"{end}T23:59:59Z"),
    )
    if not rows:
        return 0.0
    mean_apr = statistics.fmean(r["funding_apr"] for r in rows)
    held_days = max(1, days_between(start, end))
    return float(mean_apr) * (held_days / 365.0)


def slippage_cost(db: Database, base_asset: str, as_of: str, position_usd: float) -> float:
    """Slippage sized against RECORDED depth, never the printed price.

    Model: a position that fits inside the bid depth within 2% of mid pays
    proportionally up to 2%; a position larger than that depth pays the full 2%
    plus a penalty for the excess. Crude, and deliberately pessimistic --
    understating slippage is how a backtest turns an unexecutable strategy into
    a profitable one.

    With no depth recorded, this returns the full 2% rather than zero. Zero
    would mean "free to trade", which is the opposite of what "we do not know
    the exit cost" implies.
    """
    band = 0.02
    row = db.query_one(
        "SELECT bid_depth_2p0 FROM depth_snapshot d "
        "JOIN universe_snapshot u ON u.symbol = d.symbol "
        "WHERE u.base_asset = ? AND d.ts_utc <= ? AND d.bid_depth_2p0 IS NOT NULL "
        "ORDER BY d.ts_utc DESC LIMIT 1",
        (base_asset, f"{as_of}T23:59:59Z"),
    )
    depth = (row or {}).get("bid_depth_2p0")
    if not depth:
        return band

    share = position_usd / float(depth)
    if share <= 1.0:
        # Sweeping half the band's depth is modelled as paying half the band.
        return band * share
    # Beyond the band there is no recorded liquidity at all, so the excess is
    # charged at the band rate again. It is a floor on the true cost.
    return band + band * (share - 1.0)


# ==============================================================================
# Replay
# ==============================================================================
def _price_on(db: Database, base_asset: str, day: str, lookback: int = 3) -> float | None:
    return db.scalar(
        "SELECT close_usd FROM price_daily WHERE base_asset = ? "
        "AND snapshot_date <= ? AND snapshot_date >= ? ORDER BY snapshot_date DESC LIMIT 1",
        (base_asset, day, add_days(day, -lookback)),
    )


def _excursions(db: Database, base_asset: str, start: str, end: str, entry: float) -> tuple:
    rows = db.query(
        "SELECT high_usd, low_usd, close_usd FROM price_daily "
        "WHERE base_asset = ? AND snapshot_date BETWEEN ? AND ? AND source = ?",
        (base_asset, start, end, journal_prices.PRICE_SOURCE),
    )
    highs = [r["high_usd"] or r["close_usd"] for r in rows if (r["high_usd"] or r["close_usd"])]
    lows = [r["low_usd"] or r["close_usd"] for r in rows if (r["low_usd"] or r["close_usd"])]
    if not highs or not lows:
        return None, None
    return (max(highs) - entry) / entry, (min(lows) - entry) / entry


def regime_on(db: Database, day: str) -> str:
    """BTC-up, BTC-down or BTC-flat, from the recorded 30-day BTC return.

    Computed from `price_daily` rather than read from `market_regime`, because
    the regime label is only stored on days the regime collector ran, and a
    backtest that silently drops those days is testing a different sample.
    """
    cfg = get_config().thresholds.backtest
    now = _price_on(db, BTC, day)
    then = _price_on(db, BTC, add_days(day, -30))
    if not now or not then:
        return "unknown"
    change = (now - then) / then
    if change >= cfg.regime_btc_up_pct:
        return "btc_up"
    if change <= cfg.regime_btc_down_pct:
        return "btc_down"
    return "btc_flat"


def replay(
    db: Database,
    start: str,
    end: str,
    horizon: str = "30d",
    top_n: int | None = None,
    position_usd: float = 10_000.0,
    include_controls: bool = True,
) -> list[Trade]:
    """Walk the recorded rankings and build the trade list.

    Reads `layer2_result` as it was written on each day. Nothing here re-scores
    an asset: a re-score would use tables that have since been revised, and the
    result would be a backtest of hindsight.
    """
    cfg = get_config()
    limit = top_n if top_n is not None else cfg.thresholds.journal.report_top_n
    fees = fee_cost(cfg.thresholds.backtest.taker_fee_bps)
    days = horizon_to_days(horizon)

    dates = [
        r["run_date"]
        for r in db.query(
            "SELECT DISTINCT run_date FROM layer2_result WHERE run_date BETWEEN ? AND ? "
            "ORDER BY run_date",
            (start, end),
        )
    ]
    trades: list[Trade] = []
    skipped_no_price = 0

    for run_date in dates:
        # The journal's own price functions, so both sides read the same bars
        # and a disagreement means a real defect, not two conventions (D-047).
        btc_entry = journal_prices.entry_close(db, BTC, run_date)
        if not btc_entry or journal_prices.horizon_close(db, BTC, run_date, days) is None:
            # Every return is measured against BTC. Without the benchmark the
            # day contributes nothing measurable, and substituting zero would
            # quietly convert a missing benchmark into an outperforming one.
            continue

        ranked = db.query(
            "SELECT base_asset, rank, total_score, score_fundamental, score_supply, "
            "score_momentum, score_sector, score_events, score_attention, score_drawdown "
            "FROM layer2_result WHERE run_date = ? ORDER BY rank LIMIT ?",
            (run_date, limit),
        )
        chosen = {r["base_asset"] for r in ranked}
        candidates = [(r, False) for r in ranked]

        if include_controls:
            for asset in _controls(db, run_date, chosen):
                candidates.append(
                    ({"base_asset": asset, "rank": 0, "total_score": None}, True)
                )

        regime = regime_on(db, run_date)
        for row, is_control in candidates:
            asset = row["base_asset"]
            entry = journal_prices.entry_close(db, asset, run_date)
            closing = journal_prices.exit_bar(db, asset, run_date, days)
            if not entry or closing is None:
                skipped_no_price += 1
                continue
            exit_on, exit_price, reason = closing
            # BTC over the holding period the position actually had (D-061).
            btc_exit = journal_prices.close_on(db, BTC, exit_on, run_date)
            if not btc_exit:
                skipped_no_price += 1
                continue

            gross = (exit_price - entry) / entry
            funding = funding_cost(db, asset, run_date, exit_on)
            # Both fills cross the book, each at the depth on file then (D-063).
            slippage = slippage_cost(db, asset, run_date, position_usd) + slippage_cost(
                db, asset, exit_on, position_usd
            )
            net = gross - fees - funding - slippage
            btc_return = (btc_exit - btc_entry) / btc_entry
            # From the day after the signal: the entry is that day's close.
            favourable, adverse = _excursions(
                db, asset, add_days(run_date, 1), exit_on, entry
            )

            trades.append(
                Trade(
                    run_date=run_date,
                    base_asset=asset,
                    rank=int(row.get("rank") or 0),
                    is_control=is_control,
                    entry_price=entry,
                    exit_price=exit_price,
                    btc_entry=btc_entry,
                    btc_exit=btc_exit,
                    gross_return=gross,
                    fee_cost=fees,
                    funding_cost=funding,
                    slippage_cost=slippage,
                    net_return=net,
                    net_return_vs_btc=net - btc_return,
                    max_favourable=favourable,
                    max_adverse=adverse,
                    regime=regime,
                    exit_reason=reason,
                    blocks={
                        key.replace("score_", ""): row.get(key)
                        for key in (
                            "score_fundamental",
                            "score_supply",
                            "score_momentum",
                            "score_sector",
                            "score_events",
                            "score_attention",
                            "score_drawdown",
                        )
                    },
                )
            )

    if skipped_no_price:
        log.warning(
            "backtest_skipped_missing_price",
            count=skipped_no_price,
            note="left out rather than filled with a zero return",
        )
    log.info("backtest_replayed", days=len(dates), trades=len(trades), horizon=horizon)
    return trades


def _controls(db: Database, run_date: str, chosen: set[str]) -> list[str]:
    """Random L1 survivors outside the ranking, drawn the SAME way the journal
    draws them -- seeded on run_date -- so the two agree by construction."""
    # The journal's own control group -- the journalled one when it exists --
    # so the harness and the journal compare against the same assets (D-062).
    return [a for a in journal_prices.draw_controls(db, run_date) if a not in chosen]


# ==============================================================================
# Metrics
# ==============================================================================
def equity_curve(trades: list[Trade], horizon_days: int = 30) -> list[dict[str, Any]]:
    """Equal-weight net returns per run date, compounded, beside BTC hold.

    Equal weight and not score weight: score-weighting is a second strategy
    layered on the first, and its result would not be attributable to the
    ranking under test.
    """
    by_date: dict[str, list[Trade]] = {}
    for trade in trades:
        if trade.is_control:
            continue
        by_date.setdefault(trade.run_date, []).append(trade)

    equity, btc_equity = 1.0, 1.0
    out: list[dict[str, Any]] = []
    next_entry: str | None = None
    for date in sorted(by_date):
        # Non-overlapping (D-063). Compounding a 30-day return on every run date
        # counted each month about thirty times over. Enter, hold one horizon,
        # enter again: a sequence one account could actually have run.
        if next_entry is not None and date < next_entry:
            continue
        next_entry = add_days(date, horizon_days)
        batch = by_date[date]
        strategy = statistics.fmean(t.net_return for t in batch)
        btc = statistics.fmean((t.btc_exit - t.btc_entry) / t.btc_entry for t in batch)
        equity *= 1.0 + strategy
        btc_equity *= 1.0 + btc
        out.append(
            {
                "run_date": date,
                "n": len(batch),
                "period_return": round(strategy, 6),
                "equity": round(equity, 6),
                "btc_period_return": round(btc, 6),
                "btc_equity": round(btc_equity, 6),
            }
        )
    return out


def max_drawdown(curve: list[dict[str, Any]], key: str = "equity") -> float:
    peak, worst = 0.0, 0.0
    for point in curve:
        value = point[key]
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, (value - peak) / peak)
    return round(worst, 6)


def _ratio(returns: list[float], downside_only: bool = False) -> float | None:
    """Sharpe, or Sortino when downside_only.

    No risk-free rate and no annualisation. Annualising a 30-day-horizon series
    of overlapping trades produces a number that looks comparable to a
    published Sharpe and is not, and the honest version of this metric is the
    raw one with its horizon stated.
    """
    if len(returns) < 2:
        return None
    mean = statistics.fmean(returns)
    if downside_only:
        # Downside deviation: the root mean square of shortfalls below zero,
        # over EVERY return. The standard deviation of the losses alone measured
        # how much the losses differed from each other (D-063).
        if sum(1 for r in returns if r < 0) < 2:
            return None
        deviation = math.sqrt(statistics.fmean(min(r, 0.0) ** 2 for r in returns))
    else:
        deviation = statistics.pstdev(returns)
    if not deviation:
        return None
    return round(mean / deviation, 4)


def describe(trades: list[Trade], label: str) -> dict[str, Any]:
    """Both central tendencies, costs, and the shape. Never one without the others."""
    if not trades:
        return {"label": label, "n": 0}

    net = [t.net_return_vs_btc for t in trades]
    gross = [t.gross_return for t in trades]
    wins = [r for r in net if r > 0]
    losses = [r for r in net if r < 0]
    costs = [t.total_cost for t in trades]
    gross_abs = sum(abs(g) for g in gross)

    return {
        "label": label,
        "n": len(trades),
        "median_vs_btc": round(statistics.median(net), 4),
        "mean_vs_btc": round(statistics.fmean(net), 4),
        "median_gross": round(statistics.median(gross), 4),
        "mean_gross": round(statistics.fmean(gross), 4),
        "hit_rate_vs_btc": round(len(wins) / len(net), 4),
        "avg_win": round(statistics.fmean(wins), 4) if wins else None,
        "avg_loss": round(statistics.fmean(losses), 4) if losses else None,
        "win_loss_ratio": (
            round(statistics.fmean(wins) / abs(statistics.fmean(losses)), 4)
            if wins and losses
            else None
        ),
        "sharpe": _ratio(net),
        "sortino": _ratio(net, downside_only=True),
        "worst_drawdown_in_trade": (
            round(min(t.max_adverse for t in trades if t.max_adverse is not None), 4)
            if any(t.max_adverse is not None for t in trades)
            else None
        ),
        "total_cost_mean": round(statistics.fmean(costs), 6),
        # Cost drag as a share of gross movement. An acceptance criterion says
        # this must be non-trivial: if it is ~0 the cost model is not applied.
        "cost_drag_share_of_gross": (
            round(sum(costs) / gross_abs, 4) if gross_abs else None
        ),
    }


def block_attribution(trades: list[Trade]) -> dict[str, Any]:
    """Which L2 block actually predicted anything.

    Spearman-style: rank the block score and rank the outcome, then correlate.
    Rank correlation because the blocks are percentiles already and a Pearson
    figure would be dominated by the return distribution's tails.
    """
    import numpy as np

    signals = [t for t in trades if not t.is_control]
    out: dict[str, Any] = {}
    for block in ("fundamental", "supply", "sector", "events", "attention", "drawdown"):
        pairs = [
            (t.blocks.get(block), t.net_return_vs_btc)
            for t in signals
            if t.blocks.get(block) is not None
        ]
        if len(pairs) < 30:
            out[block] = {"n": len(pairs), "correlation": None, "note": "too few observations"}
            continue
        scores = np.argsort(np.argsort([p[0] for p in pairs]))
        outcomes = np.argsort(np.argsort([p[1] for p in pairs]))
        correlation = float(np.corrcoef(scores, outcomes)[0, 1])
        out[block] = {"n": len(pairs), "correlation": round(correlation, 4)}
    return out


# ==============================================================================
# Holdout -- one shot, and enforced rather than promised
# ==============================================================================
def split_dates(
    dates: list[str], holdout_fraction: float | None = None, embargo_days: int = 0
) -> tuple[list[str], list[str]]:
    """Chronological split. Development first, holdout last, never shuffled.

    A random split would put future days in the development set, and with
    overlapping 30-day horizons that leaks the answer directly.
    """
    fraction = (
        holdout_fraction
        if holdout_fraction is not None
        else get_config().thresholds.backtest.holdout_fraction
    )
    if not dates:
        return [], []
    cut = int(len(dates) * (1.0 - fraction))
    development, holdout = dates[:cut], dates[cut:]
    if embargo_days and holdout:
        # A development trade entered within one horizon of the holdout exits
        # inside it, so its outcome is holdout data (D-063).
        development = [d for d in development if add_days(d, embargo_days) < holdout[0]]
    return development, holdout


def record_holdout_run(
    db: Database, split: str, window: dict[str, Any], horizon: str, summary: dict[str, Any]
) -> int:
    """Log that the holdout was consumed, and return how many times it now has.

    "Test once" is a promise no code can keep by asking politely. So each run
    is recorded, and run_backtest reports the count back. A second run is not
    blocked -- there are legitimate reasons to re-run after a bug fix -- but it
    is labelled, so nobody can later present a fourth-attempt result as an
    out-of-sample one.

    `run_id` is a RANDOM uuid. The first version derived it from the window and
    a second-resolution timestamp, so two runs inside the same second collided,
    the upsert replaced the row, and the counter stayed at one -- the
    anti-self-deception mechanism was itself defeatable, and a test caught it.
    An audit log whose rows can overwrite each other is not an audit log.
    """
    upsert(
        db,
        "backtest_run",
        [
            {
                "run_id": uuid.uuid4().hex,
                "split": split,
                "window_start": window["start"],
                "window_end": window["end"],
                "horizon": horizon,
                "trades": summary.get("trades"),
                "median_vs_btc": summary.get("median_vs_btc"),
                "mean_vs_btc": summary.get("mean_vs_btc"),
                "cost_drag": summary.get("cost_drag"),
                "ran_at_utc": utc_now_iso(),
            }
        ],
    )
    return db.scalar("SELECT COUNT(*) FROM backtest_run WHERE split = 'holdout'") or 0


# ==============================================================================
# Orchestration
# ==============================================================================
def run_backtest(
    start: str,
    end: str,
    horizon: str = "30d",
    holdout: bool = False,
    top_n: int | None = None,
    position_usd: float = 10_000.0,
) -> dict[str, Any]:
    """The whole harness. Raises InsufficientHistory rather than guessing."""
    with get_db() as db:
        state = assert_runnable(db)
        coverage = bt_universe.coverage(db, start, end)

        dates = [
            r["run_date"]
            for r in db.query(
                "SELECT DISTINCT run_date FROM layer2_result WHERE run_date BETWEEN ? AND ? "
                "ORDER BY run_date",
                (start, end),
            )
        ]
        development, holdout_dates = split_dates(dates, embargo_days=horizon_to_days(horizon))
        window = holdout_dates if holdout else development
        if not window:
            raise InsufficientHistory(
                f"the {'holdout' if holdout else 'development'} split of "
                f"{start}..{end} is empty ({len(dates)} recorded days)"
            )

        trades = replay(
            db, window[0], window[-1], horizon=horizon, top_n=top_n, position_usd=position_usd
        )
        signals = [t for t in trades if not t.is_control]
        controls = [t for t in trades if t.is_control]
        curve = equity_curve(trades, horizon_to_days(horizon))

        by_regime = {}
        for regime in ("btc_up", "btc_flat", "btc_down", "unknown"):
            subset = [t for t in signals if t.regime == regime]
            if subset:
                by_regime[regime] = describe(subset, regime)

        summary: dict[str, Any] = {
            "window": {"start": window[0], "end": window[-1], "days": len(window)},
            "split": "holdout" if holdout else "development",
            "horizon": horizon,
            "history": state,
            "universe_coverage": coverage,
            "trades": len(trades),
            "signal": describe(signals, "signal"),
            "control": describe(controls, "control"),
            "edge_vs_control": _edge(signals, controls),
            "equity_curve": curve,
            "max_drawdown": max_drawdown(curve),
            "btc_max_drawdown": max_drawdown(curve, key="btc_equity"),
            "turnover_positions_per_day": (
                round(len(signals) / len(window), 2) if window else None
            ),
            # A strategy that only works in one regime is a beta bet in
            # disguise, and the split is the only way to see that.
            "by_regime": by_regime,
            "block_attribution": block_attribution(trades),
        }

        if holdout:
            count = record_holdout_run(
                db,
                "holdout",
                summary["window"],
                horizon,
                {
                    "trades": len(trades),
                    "median_vs_btc": summary["signal"].get("median_vs_btc"),
                    "mean_vs_btc": summary["signal"].get("mean_vs_btc"),
                    "cost_drag": summary["signal"].get("cost_drag_share_of_gross"),
                },
            )
            summary["holdout_runs_recorded"] = count
            if count > 1:
                summary["holdout_warning"] = (
                    f"This is holdout run #{count}. After the first look the holdout "
                    "is no longer out-of-sample, and this result must not be "
                    "presented as though it were."
                )

    log.info(
        "backtest_complete",
        split=summary["split"],
        trades=summary["trades"],
        median_vs_btc=summary["signal"].get("median_vs_btc"),
        cost_drag=summary["signal"].get("cost_drag_share_of_gross"),
    )
    return summary


def _edge(signals: list[Trade], controls: list[Trade]) -> dict[str, Any]:
    if not signals or not controls:
        return {"available": False, "reason": "need both signals and controls"}
    sig = [t.net_return_vs_btc for t in signals]
    ctl = [t.net_return_vs_btc for t in controls]
    return {
        "available": True,
        "median_difference": round(statistics.median(sig) - statistics.median(ctl), 4),
        "mean_difference": round(statistics.fmean(sig) - statistics.fmean(ctl), 4),
        "signal_n": len(sig),
        "control_n": len(ctl),
    }


def reconcile_with_journal(horizon: str = "30d", tolerance: float = 0.005) -> dict[str, Any]:
    """Compare the harness against the journal over the same signals.

    Spec 13 acceptance criterion: they must agree. If they do not, one of them
    has look-ahead bias, and finding out which is worth more than either
    number on its own. The comparison is on GROSS returns, because the harness
    applies costs and the journal deliberately does not -- comparing net
    against raw would show a difference on every row and prove nothing.
    """
    from src.journal import forward_returns as fr

    with get_db() as db:
        journal_rows = db.query(
            "SELECT e.run_date, e.base_asset, f.return_raw FROM journal_entry e "
            "JOIN forward_return f ON f.entry_id = e.entry_id "
            "WHERE f.horizon = ? AND e.is_control = 0",
            (horizon,),
        )
        if not journal_rows:
            return {"comparable": False, "reason": "the journal has no completed returns yet"}

        dates = sorted({r["run_date"] for r in journal_rows})
        trades = replay(db, dates[0], dates[-1], horizon=horizon, include_controls=False)
        statistics_payload = fr.compute_statistics(horizon)

    journal_map = {(r["run_date"], r["base_asset"]): r["return_raw"] for r in journal_rows}
    compared = 0
    disagreements: list[dict[str, Any]] = []
    for trade in trades:
        key = (trade.run_date, trade.base_asset)
        if key not in journal_map:
            continue
        compared += 1
        difference = abs(trade.gross_return - journal_map[key])
        if difference > tolerance:
            disagreements.append(
                {
                    "run_date": trade.run_date,
                    "base_asset": trade.base_asset,
                    "harness_gross": round(trade.gross_return, 6),
                    "journal_raw": round(journal_map[key], 6),
                    "difference": round(difference, 6),
                }
            )

    return {
        "comparable": compared > 0,
        "compared": compared,
        "agreed": compared - len(disagreements),
        "disagreements": disagreements[:25],
        "tolerance": tolerance,
        "verdict": (
            "agree"
            if compared and not disagreements
            else "DISAGREE -- one of the two has look-ahead bias; find it before "
            "trusting either"
        ),
        "journal_statistics": statistics_payload if compared else None,
    }


def render_report(start: str, end: str, holdout: bool = False, horizon: str = "30d") -> str:
    """The harness as markdown, for the CLI.

    When there is not enough history it renders the refusal and how far short
    the data is, rather than an empty report with zeroes in it. A zero Sharpe
    on nine trades reads as "the strategy does not work"; "nine trades" reads
    as what it is.
    """
    try:
        summary = run_backtest(start, end, horizon=horizon, holdout=holdout)
    except InsufficientHistory as exc:
        with get_db() as db:
            state = history(db)
        return "\n".join(
            [
                "# Backtest",
                "",
                "**Not run.**",
                "",
                str(exc),
                "",
                f"Recorded so far: {state['screening_days']} screening days "
                f"({state['first_date']} to {state['last_date']}), "
                f"{state['recorded_rankings']} rankings.",
                f"Needed: {state['days_required']} days and "
                f"{state['signals_required']} rankings.",
                "",
            ]
        )

    signal, control = summary["signal"], summary["control"]
    lines = [
        f"# Backtest — {summary['split']} split",
        "",
        f"Window {summary['window']['start']} to {summary['window']['end']} "
        f"({summary['window']['days']} recorded days), {horizon} horizon, "
        f"{summary['trades']} trades.",
        "",
        f"Universe snapshot completeness over the window: "
        f"{summary['universe_coverage']['completeness']:.1%}.",
        "",
        "| group | n | median vs BTC | mean vs BTC | hit rate | Sharpe | Sortino | cost drag |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for group in (signal, control):
        if not group.get("n"):
            lines.append(f"| {group['label']} | 0 | - | - | - | - | - | - |")
            continue
        lines.append(
            "| {label} | {n} | {median:+.1%} | {mean:+.1%} | {hit:.0%} | {sharpe} | "
            "{sortino} | {drag} |".format(
                label=group["label"],
                n=group["n"],
                median=group["median_vs_btc"],
                mean=group["mean_vs_btc"],
                hit=group["hit_rate_vs_btc"],
                sharpe=group["sharpe"] if group["sharpe"] is not None else "-",
                sortino=group["sortino"] if group["sortino"] is not None else "-",
                drag=(
                    f"{group['cost_drag_share_of_gross']:.1%}"
                    if group["cost_drag_share_of_gross"] is not None
                    else "-"
                ),
            )
        )

    lines += ["", f"Max drawdown {summary['max_drawdown']:.1%} against BTC "
              f"buy-and-hold {summary['btc_max_drawdown']:.1%}.", ""]

    if signal.get("n") and signal["mean_vs_btc"] > 0 > signal["median_vs_btc"]:
        lines += [
            "> **Mean positive, median negative.** A lottery-ticket distribution: "
            "a few large winners carry the average while the typical outcome is a "
            "loss. Legitimate, but it must be sized as one.",
            "",
        ]

    if summary["by_regime"]:
        lines += ["## By regime", "", "| regime | n | median vs BTC | hit rate |", "|---|---:|---:|---:|"]
        for regime, stats in summary["by_regime"].items():
            lines.append(
                f"| {regime} | {stats['n']} | {stats['median_vs_btc']:+.1%} | "
                f"{stats['hit_rate_vs_btc']:.0%} |"
            )
        lines += [
            "",
            "_A strategy that only works in one regime is a beta bet in disguise._",
            "",
        ]

    lines += ["## Block attribution", "", "| block | n | rank correlation with outcome |", "|---|---:|---:|"]
    for block, stats in summary["block_attribution"].items():
        correlation = stats["correlation"]
        lines.append(
            f"| {block} | {stats['n']} | "
            f"{'—' if correlation is None else f'{correlation:+.3f}'} |"
        )
    lines.append("")

    if summary.get("holdout_warning"):
        lines += [f"> **{summary['holdout_warning']}**", ""]
    return "\n".join(lines)


__all__ = [
    "InsufficientHistory",
    "MIN_SCREENING_DAYS",
    "MIN_SIGNALS",
    "Trade",
    "assert_runnable",
    "block_attribution",
    "describe",
    "equity_curve",
    "fee_cost",
    "funding_cost",
    "history",
    "max_drawdown",
    "reconcile_with_journal",
    "record_holdout_run",
    "regime_on",
    "render_report",
    "replay",
    "run_backtest",
    "slippage_cost",
    "split_dates",
]
