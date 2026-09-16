"""
# WHY: ------------------------------------------------------------------------
# The OPTIONAL Tier-A daemon, for a machine that stays on.
#
# Read this before using it: this file is the only place in the project where a
# scheduling loop is allowed to exist, and it is deliberately OUTSIDE src/. The
# collectors themselves remain stateless functions of a timestamp; APScheduler
# calls them on an interval. That separation is what lets the same collector run
# under GitHub Actions, under this daemon, and under pytest unchanged.
#
# WHEN YOU NEED THIS: only for the 5-minute tier (intraday positioning and
# depth). Layers 1 and 2, the journal and the dashboard are ENTIRELY daily
# computations and run fine on Actions without this file. If you never get a
# persistent host, you lose the intraday positioning layer and nothing else --
# write that in DECISIONS.md and move on. It is an honest limitation, not a
# failure.
#
# WHY NOT ON GITHUB ACTIONS: 288 runs/day of continuous collection is the
# "serverless computing" clause of the Actions Terms of Service. The penalty is
# not a bill, it is losing Actions on the account that runs everything else.
#
# Usage:  python scripts/run_collectors.py --tier fast
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # noqa: E402

from src.collectors import registry  # noqa: E402
from src.config import get_config  # noqa: E402
from src.logging_setup import get_logger  # noqa: E402
from src.timeutil import utc_now  # noqa: E402

log = get_logger("daemon")

#: Consecutive failures per collector. Three in a row means something is broken
#: rather than flaky, and deserves an alert rather than another retry.
_failures: dict[str, int] = defaultdict(int)


async def run_tier(tier: str) -> None:
    """One cycle of one tier. Called by APScheduler; never loops itself."""
    cfg = get_config()
    threshold = cfg.thresholds.collectors.consecutive_failures_before_alert
    collectors = registry.resolve(tier)
    results = await registry.run_many(collectors, utc_now())

    for result in results:
        if result.ok:
            _failures[result.collector] = 0
            continue
        _failures[result.collector] += 1
        if _failures[result.collector] >= threshold:
            log.error(
                "collector_repeatedly_failing",
                collector=result.collector,
                consecutive_failures=_failures[result.collector],
                error=result.error_message,
            )
            _alert(result.collector, _failures[result.collector], result.error_message)


def _alert(collector: str, failures: int, error: str | None) -> None:
    """Notify via Telegram if configured. Notification only -- never execution."""
    try:
        from src.report.telegram import send_message

        send_message(
            f"collector `{collector}` has failed {failures} times in a row.\n"
            f"last error: {error or 'unknown'}"
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("alert_delivery_failed", error=str(exc))


def main() -> None:
    parser = argparse.ArgumentParser(description="Tier-A collector daemon (host only).")
    parser.add_argument(
        "--tier",
        default="fast",
        choices=sorted(registry.TIERS),
        help="Which cadence tier to run. 'fast' is the reason this file exists.",
    )
    parser.add_argument(
        "--interval-minutes",
        type=int,
        default=None,
        help="Override the configured interval. Default comes from thresholds.yaml.",
    )
    args = parser.parse_args()

    cfg = get_config()
    # Each tier at its own cadence (D-065). Every tier ran at the 5-minute
    # derivatives interval, so `--tier daily` would re-fetch the day's universe
    # 288 times a day.
    tier_minutes = {
        "fast": cfg.thresholds.collectors.derivatives_interval_minutes,
        "hourly": 60,
        "daily": 24 * 60,
        "supply": 24 * 60,
    }
    interval = args.interval_minutes or tier_minutes.get(args.tier, 24 * 60)
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        run_tier,
        "interval",
        minutes=interval,
        args=[args.tier],
        id=f"tier-{args.tier}",
        # Never let two cycles overlap: two collectors writing the same
        # snapshot key would contend, and a backlog of missed runs firing at
        # once helps nobody.
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
        next_run_time=utc_now(),
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    scheduler.start()
    log.info("daemon_started", tier=args.tier, interval_minutes=interval)

    def shutdown(*_args: object) -> None:
        log.info("daemon_stopping")
        scheduler.shutdown(wait=False)
        loop.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown)
        except NotImplementedError:  # Windows
            signal.signal(sig, lambda *_: shutdown())

    try:
        loop.run_forever()
    finally:
        loop.close()
        log.info("daemon_stopped")


if __name__ == "__main__":
    main()
