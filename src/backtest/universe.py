"""
# WHY: ------------------------------------------------------------------------
# Point-in-time universe reconstruction. The single line of defence against
# survivorship bias, which is the bias that makes every naive crypto backtest
# look profitable.
#
# The mechanism is worth stating precisely, because it is easy to nod along to
# and then implement wrongly:
#
#   A delisted symbol VANISHES from Binance's exchangeInfo. It is not marked
#   dead; it is simply absent. So a backtest that takes today's symbol list and
#   runs it over last year is not testing "the screener over last year" -- it
#   is testing the screener over the subset of assets that survived to today.
#   Every asset that went to zero, got delisted, or was quietly removed is
#   excluded from the sample. Those are precisely the assets an L1 kill switch
#   exists to catch, so removing them deletes the evidence that the filter
#   works, and simultaneously inflates the returns of anything that passed.
#
# This module therefore reads ONLY `universe_snapshot`, which is append-only by
# convention and never pruned, and reconstructs what the symbol list actually
# was on a given morning.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

from typing import Any

from src.db.connection import Database
from src.logging_setup import get_logger

log = get_logger("backtest.universe")


def snapshot_dates(db: Database) -> list[str]:
    """Every date for which a universe snapshot exists, ascending."""
    return [
        r["snapshot_date"]
        for r in db.query(
            "SELECT DISTINCT snapshot_date FROM universe_snapshot ORDER BY snapshot_date"
        )
    ]


def point_in_time_universe(db: Database, as_of: str, exchange: str | None = None) -> list[str]:
    """The symbol list as it stood on `as_of`.

    Uses the snapshot for that exact date when one exists. When it does not --
    a missed collection day -- it falls back to the most recent EARLIER
    snapshot and says so. It never falls forward: a later snapshot contains
    symbols that had not yet listed, and using one would let the backtest trade
    an asset before it existed.
    """
    exact = db.scalar(
        "SELECT COUNT(*) FROM universe_snapshot WHERE snapshot_date = ?", (as_of,)
    )
    source_date = as_of
    if not exact:
        source_date = db.scalar(
            "SELECT MAX(snapshot_date) FROM universe_snapshot WHERE snapshot_date < ?",
            (as_of,),
        )
        if not source_date:
            return []
        log.warning(
            "universe_snapshot_missing",
            as_of=as_of,
            used=source_date,
            note="fell BACK to an earlier snapshot; never forward, which would "
            "admit symbols that had not yet listed",
        )

    params: list[Any] = [source_date]
    clause = ""
    if exchange:
        clause = " AND exchange = ?"
        params.append(exchange)
    return [
        r["symbol"]
        for r in db.query(
            f"SELECT symbol FROM universe_snapshot WHERE snapshot_date = ?{clause} "
            "ORDER BY symbol",
            tuple(params),
        )
    ]


def delistings_between(db: Database, start: str, end: str) -> list[dict[str, Any]]:
    """Symbols present at `start` and absent at `end`.

    The verification hook for the whole module. Run it across a window
    containing a delisting you remember, and if the symbol does not appear
    here, the universe is not being reconstructed point-in-time and no number
    downstream can be trusted.
    """
    before = set(point_in_time_universe(db, start))
    after = set(point_in_time_universe(db, end))
    gone = sorted(before - after)
    if not gone:
        return []

    out: list[dict[str, Any]] = []
    for symbol in gone:
        last = db.query_one(
            "SELECT MAX(snapshot_date) AS last_seen, base_asset, status "
            "FROM universe_snapshot WHERE symbol = ? AND snapshot_date <= ?",
            (symbol, end),
        )
        out.append(
            {
                "symbol": symbol,
                "base_asset": (last or {}).get("base_asset"),
                "last_seen": (last or {}).get("last_seen"),
                "last_status": (last or {}).get("status"),
            }
        )
    return out


def coverage(db: Database, start: str, end: str) -> dict[str, Any]:
    """How many days in the window actually have a snapshot.

    A backtest over a window with holes is a backtest over a different window,
    and the caller has to be told the size of the holes rather than discovering
    a smooth-looking equity curve built from Tuesdays.
    """
    from src.timeutil import days_between

    dates = [d for d in snapshot_dates(db) if start <= d <= end]
    expected = days_between(start, end) + 1
    return {
        "start": start,
        "end": end,
        "expected_days": expected,
        "days_with_snapshot": len(dates),
        "completeness": round(len(dates) / expected, 4) if expected else 0.0,
        "first": dates[0] if dates else None,
        "last": dates[-1] if dates else None,
    }


__all__ = ["coverage", "delistings_between", "point_in_time_universe", "snapshot_dates"]
