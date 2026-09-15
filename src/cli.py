"""
# WHY: ------------------------------------------------------------------------
# The single entrypoint. Every workflow, every cron job and every manual run
# goes through this file, so there is exactly one place to look when asking
# "what does the 03:10 job actually execute?".
#
# Design rule: a command is a thin wrapper that parses arguments and calls into
# src/. No business logic lives here. That keeps the logic testable without
# spawning a subprocess, and keeps the CLI honest about what it does.
#
# Read-only by construction: there is no command that places an order, and there
# never will be (guardrail 18.1). This system reads public market data and
# writes to its own database. Nothing else.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

import asyncio
import sys
from typing import Optional

import typer

from src.logging_setup import get_logger
from src.timeutil import today_utc, utc_now

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Crypto gem discovery and risk screening. Read-only research tooling.",
)
log = get_logger("cli")


@app.command("init-db")
def init_db_command() -> None:
    """Create every table, index and trigger. Safe to run repeatedly."""
    from src.db.connection import init_db

    result = init_db()
    typer.echo(f"backend            : {result['backend']}")
    typer.echo(f"statements executed: {result['statements_executed']}")
    typer.echo(f"tables present     : {result['table_count']}")
    for table in result["tables"]:
        typer.echo(f"  - {table}")


@app.command("collect")
def collect_command(
    name: str = typer.Argument(
        ...,
        help="Collector name, a tier ('daily'/'hourly'/'supply'/'fast'), or 'all'.",
    ),
    as_of: Optional[str] = typer.Option(
        None, "--as-of", help="Logical snapshot date YYYY-MM-DD. Defaults to today (UTC)."
    ),
) -> None:
    """Run one collector, or every collector in a cadence tier.

    Collectors are stateless functions of a timestamp: this command runs them
    once and exits. It never loops. Scheduling is external (cron-job.org fires
    a GitHub Actions workflow, which invokes this command).
    """
    from src.collectors import registry

    stamp = utc_now()
    if as_of:
        from src.timeutil import parse_day

        stamp = stamp.replace(
            year=parse_day(as_of).year, month=parse_day(as_of).month, day=parse_day(as_of).day
        )

    collectors = registry.resolve(name)
    if not collectors:
        typer.secho(
            f"No collector matches {name!r}. Known: {', '.join(registry.names())}",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=2)

    if as_of:
        from src.timeutil import format_day, today_utc

        refused = registry.refuse_as_of(collectors, format_day(stamp), today_utc())
        if refused:
            # All or nothing: half a tier recorded for a past day is a snapshot
            # no later reader can tell apart from a complete one (D-048).
            for reason in refused:
                typer.secho(f"refused  {reason}", fg=typer.colors.RED)
            raise typer.Exit(code=2)

    results = asyncio.run(registry.run_many(collectors, stamp))
    failed = [r for r in results if not r.ok]
    for r in results:
        typer.echo(r.summary())
    typer.echo(f"\n{len(results) - len(failed)}/{len(results)} collectors succeeded")
    if failed:
        # Exit non-zero so the Actions job fails and healthchecks.io does NOT
        # get its ping. A silent partial failure is the thing being guarded
        # against here.
        raise typer.Exit(code=1)


@app.command("screen")
def screen_command(
    date: Optional[str] = typer.Option(None, "--date", help="Run date YYYY-MM-DD, default today."),
    skip_freshness: bool = typer.Option(
        False,
        "--skip-freshness",
        help="Bypass the staleness assertion. For backfills only, never in CI.",
    ),
    no_layer3: bool = typer.Option(
        False, "--no-layer3", help="Skip the L3 structure pass (needs OHLC history)."
    ),
) -> None:
    """Run Layer 1 (kill switch), Layer 2 (demand score), then Layer 3 (advisory)."""
    from src.screening.pipeline import run_screen

    run_date = date or today_utc()
    result = run_screen(
        run_date=run_date, enforce_freshness=not skip_freshness, layer3=not no_layer3
    )
    typer.echo(
        f"{run_date}: {result['universe']} assets -> "
        f"{result['survivors']} survived L1 ({result['survival_rate']:.1%}) -> "
        f"top {result['ranked']} ranked"
    )
    for row in result["top"]:
        typer.echo(f"  {row['rank']:>2}. {row['base_asset']:<10} {row['total_score']:.1f}")
    if result.get("layer3_analysed"):
        typer.echo(
            f"L3: {result['layer3_analysed']} analysed, {result['layer3_setups']} with a "
            f"live setup, {result['layer3_flagged']} carrying a risk flag "
            "(advisory only -- not a buy signal)"
        )


@app.command("journal")
def journal_command(
    date: Optional[str] = typer.Option(None, "--date", help="Run date YYYY-MM-DD, default today."),
    report: bool = typer.Option(False, "--report", help="Print statistics instead of writing."),
) -> None:
    """Write journal entries, and backfill forward returns whose horizon elapsed.

    Append-only. There is no command that deletes a journal entry, and the
    database refuses the operation anyway.
    """
    from src.journal import forward_returns

    if report:
        typer.echo(forward_returns.render_report())
        return

    run_date = date or today_utc()
    written = forward_returns.write_entries(run_date)
    filled = forward_returns.backfill_returns()
    typer.echo(f"{run_date}: {written} journal entries written, {filled} forward returns filled")


@app.command("report")
def report_command(
    date: Optional[str] = typer.Option(None, "--date", help="Run date YYYY-MM-DD, default today."),
    telegram: bool = typer.Option(False, "--telegram", help="Also send to Telegram if configured."),
) -> None:
    """Render the daily markdown report to reports/YYYY-MM-DD.md."""
    from src.report import daily

    run_date = date or today_utc()
    path = daily.write_report(run_date)
    typer.echo(f"wrote {path}")
    if telegram:
        from src.report import telegram as tg

        if not tg.is_configured():
            typer.echo("telegram: not configured, skipped")
            return
        if tg.send_daily_summary(run_date):
            typer.echo("telegram: sent")
            return
        # Configured and still not delivered: a wrong token, a chat the bot was
        # never messaged from, or Telegram being down. Printing "skipped" here
        # would read as a setup gap when it is a delivery failure. Exit non-zero
        # so the workflow step shows failed; it runs with continue-on-error, so
        # the screen job itself stays green.
        typer.echo(
            "telegram: configured but NOT delivered -- see the telegram_send_* warning",
            err=True,
        )
        raise typer.Exit(code=1)


@app.command("publish")
def publish_command(
    date: Optional[str] = typer.Option(None, "--date", help="Run date YYYY-MM-DD, default today."),
) -> None:
    """Serialise the database into data/public/*.json for the static dashboard.

    Everything the site displays is baked here. The browser never holds a key
    and never calls an API (guardrail 18.7).
    """
    from src.report import publish

    run_date = date or today_utc()
    written = publish.publish_all(run_date)
    for path, size in written:
        typer.echo(f"  {path} ({size:,} bytes)")
    typer.echo(f"{len(written)} files written")


@app.command("backtest")
def backtest_command(
    start: str = typer.Option(..., "--start", help="Start date YYYY-MM-DD."),
    end: str = typer.Option(..., "--end", help="End date YYYY-MM-DD."),
    holdout: bool = typer.Option(False, "--holdout", help="Run the out-of-sample third. Once."),
) -> None:
    """Run the backtest harness over recorded data.

    Do not run this before roughly six months of collection exist. Before that
    the result is noise that looks like signal.
    """
    from src.backtest import harness

    typer.echo(harness.render_report(start=start, end=end, holdout=holdout))


@app.command("migrate-turso")
def migrate_turso_command(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Count what would move. Writes nothing."
    ),
    verify: bool = typer.Option(
        False, "--verify", help="Check Turso's row counts and append-only triggers."
    ),
) -> None:
    """Copy the local SQLite database into Turso. One-way, run once at cutover.

    Most of what moves is replaceable from the APIs. The journal and the
    point-in-time layer1/layer2 results are not: they record what the system
    believed on a given day, and recomputing them later would use revised data.
    """
    from src.ops import migrate as migrate_ops

    if verify:
        state = migrate_ops.verify()
        for table, count in state["counts"].items():
            typer.echo(f"  {table:24s} {count:>8,}")
        typer.echo("")
        typer.echo(f"append-only enforced: {state['append_only_enforced']}")
        typer.echo(f"detail: {state['detail']}")
        return

    results = migrate_ops.migrate(dry_run=dry_run)
    for row in results:
        note = f"  ({row['skipped']})" if row.get("skipped") else ""
        typer.echo(f"  {row['table']:24s} {row['rows']:>8,} rows{note}")
    total = sum(r["copied"] for r in results)
    typer.echo("")
    typer.echo(
        "DRY RUN - nothing written" if dry_run else f"{total:,} rows copied into Turso"
    )


@app.command("doctor")
def doctor_command() -> None:
    """Check configuration, database and data freshness. Prints what is wrong."""
    from src.ops.doctor import run_diagnostics

    report = run_diagnostics()
    for line in report["lines"]:
        typer.echo(line)
    if report["problems"]:
        raise typer.Exit(code=1)


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:  # pragma: no cover
        typer.secho("interrupted", fg=typer.colors.YELLOW)
        sys.exit(130)


if __name__ == "__main__":
    main()
