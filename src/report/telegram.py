"""
# WHY: ------------------------------------------------------------------------
# Telegram delivery. NOTIFICATION ONLY (spec 14).
#
# There is no command handler here, no polling loop, no callback, and there
# never will be. The reasons are not stylistic:
#
#   * An interactive bot is an inbound control channel into a system that holds
#     API credentials. The screener's own guardrail is that it never places an
#     order; a bot that accepts commands is the first step toward one that can.
#
#   * A poller is a process that must stay alive, which is exactly the
#     architecture the whole system rejects (external cron, stateless jobs).
#
# So this module has one verb: send. It is fire-and-forget, it degrades to a
# no-op when unconfigured, and a delivery failure must NEVER fail the job that
# called it -- the report is already written to disk and to the dashboard, and
# losing a phone notification is not worth losing a screening run.
#
# The token is read from the environment and is never logged, never echoed in
# an error, and never written into the payload.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

from typing import Any

import httpx

from src.config import get_config
from src.db.connection import get_db
from src.logging_setup import get_logger
from src.report import daily
from src.timeutil import today_utc

log = get_logger("report.telegram")

API_BASE = "https://api.telegram.org"

#: Telegram rejects a message body above 4096 characters. Truncating with a
#: visible marker beats a 400 that loses the whole notification.
MAX_MESSAGE_CHARS = 4000

#: Characters legacy Markdown (parse_mode="Markdown") reads as entity
#: delimiters. An unclosed one gets the WHOLE message rejected, and every check
#: ID in this system carries an underscore -- L1_HOLDER_CONC, L1_UNLOCK -- so
#: an unescaped dark-check line would fail delivery on exactly the days that
#: line matters. The Bot API's escape for these is a preceding backslash.
_MARKDOWN_SPECIAL = ("_", "*", "`", "[")


def _md(text: object) -> str:
    """Escape dynamic text for legacy Markdown. Never apply to our own markup."""
    out = str(text)
    for char in _MARKDOWN_SPECIAL:
        out = out.replace(char, "\\" + char)
    return out


def is_configured() -> bool:
    secrets = get_config().secrets
    return bool(secrets.telegram_bot_token and secrets.telegram_chat_id)


def send_message(text: str) -> bool:
    """Send one message. Returns False on any problem, and never raises.

    A raise here would propagate into the report job, whose actual product is
    already on disk. The notification is the least important artefact in the
    pipeline and must be the least able to break it.
    """
    secrets = get_config().secrets
    if not is_configured():
        log.warning(
            "telegram_not_configured",
            effect="notification skipped; the report and dashboard are unaffected",
        )
        return False

    if len(text) > MAX_MESSAGE_CHARS:
        text = text[: MAX_MESSAGE_CHARS - 40] + "\n\n[truncated - see the dashboard]"

    try:
        response = httpx.post(
            # The token is a path segment of the URL. Nothing in this function
            # logs the URL, and the exception handler below logs only the type
            # and status -- httpx puts the full URL in str(exc), which is
            # exactly how a bot token ends up in a public Actions log.
            f"{API_BASE}/bot{secrets.telegram_bot_token}/sendMessage",
            json={
                "chat_id": secrets.telegram_chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=get_config().settings.http.timeout_seconds,
        )
    except httpx.HTTPError as exc:
        log.warning("telegram_send_failed", error_type=type(exc).__name__)
        return False

    if response.status_code != 200:
        # Body may quote the request URL, so it is not logged either.
        log.warning("telegram_send_rejected", status=response.status_code)
        return False

    log.info("telegram_sent", chars=len(text))
    return True


def format_summary(payload: dict[str, Any]) -> str:
    """The phone-sized version of the daily report.

    Carries the funnel, the top five, and any dark check. The dark-check line
    is not optional: the whole value of a glanceable summary is that the reader
    trusts it, and a summary that omits "three checks were inoperative today"
    is a summary that overstates what was verified.
    """
    p = payload
    if not p["universe"]:
        return f"*Screen {p['run_date']}*\nNo universe rows. Nothing was screened."

    lines = [
        f"*Screen {p['run_date']}*",
        f"{p['universe']} perps -> {p['survivors']} survived L1 ({p['survival_rate']:.0%})",
    ]

    regime = p.get("regime") or {}
    if regime.get("regime"):
        lines.append(f"Regime: {_md(regime['regime'])}")

    if p["dark_checks"]:
        lines.append("")
        lines.append(
            "*Checks dark:* " + ", ".join(_md(check) for check in p["dark_checks"])
            + " - survivors are "
            "UNMEASURED on these, not clean."
        )

    top = p["ranked"][:5]
    if top:
        lines.append("")
        lines.append("*Top 5*")
        for row in top:
            score = row["total_score"]
            lines.append(f"{row['rank']}. `{row['base_asset']}`  {score:.1f}")

    lines.append("")
    lines.append("Ranking is not a buy signal. L3 timing and sizing are separate.")
    return "\n".join(lines)


def send_daily_summary(run_date: str | None = None) -> bool:
    """Render and send today's summary. No-op when unconfigured."""
    if not is_configured():
        log.warning("telegram_not_configured", effect="daily summary skipped")
        return False
    date = run_date or today_utc()
    with get_db() as db:
        payload = daily.gather(db, date)
    return send_message(format_summary(payload))


__all__ = ["format_summary", "is_configured", "send_daily_summary", "send_message"]
