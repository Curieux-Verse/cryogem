"""
The daily Telegram summary: what the CLI reports, and what reaches the log.

Two properties matter because the repository and its Actions logs are public.
A failed delivery must not read as "not configured" -- that sends the operator
chasing a setup gap that does not exist. And the bot token, which Telegram
carries as a URL path segment, must not survive httpx's own request log line:
telegram.py never logs the URL, but httpx logs every request at INFO anyway.
"""

from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

from src import cli
from src.logging_setup import _redact_secrets
from src.report import daily, telegram

#: Synthetic. Real-token shape: numeric bot id, colon, 35+ url-safe characters.
FAKE_TOKEN = "123456789:AAaaBBbbCCccDDddEEeeFFffGGgghhhh1234"

runner = CliRunner()


@pytest.fixture
def no_disk_report(monkeypatch, tmp_path):
    """The report file is not under test; keep the database out of it."""
    monkeypatch.setattr(daily, "write_report", lambda run_date=None: tmp_path / "report.md")


def _invoke():
    return runner.invoke(cli.app, ["report", "--telegram", "--date", "2026-09-10"])


class TestReportTelegramOutcome:
    def test_unconfigured_is_a_skip_not_a_failure(self, monkeypatch, no_disk_report):
        monkeypatch.setattr(telegram, "is_configured", lambda: False)
        monkeypatch.setattr(
            telegram,
            "send_daily_summary",
            lambda run_date=None: pytest.fail("must not attempt a send when unconfigured"),
        )
        result = _invoke()
        assert result.exit_code == 0
        assert "not configured, skipped" in result.output

    def test_delivered_exits_zero(self, monkeypatch, no_disk_report):
        monkeypatch.setattr(telegram, "is_configured", lambda: True)
        monkeypatch.setattr(telegram, "send_daily_summary", lambda run_date=None: True)
        result = _invoke()
        assert result.exit_code == 0
        assert "telegram: sent" in result.output

    def test_configured_but_rejected_fails_and_says_so(self, monkeypatch, no_disk_report):
        """Regression: this case used to print 'not configured, skipped'."""
        monkeypatch.setattr(telegram, "is_configured", lambda: True)
        monkeypatch.setattr(telegram, "send_daily_summary", lambda run_date=None: False)
        result = _invoke()
        assert result.exit_code == 1
        assert "NOT delivered" in result.output
        assert "not configured" not in result.output


class TestHttpxRequestLineIsRedacted:
    def test_bot_token_in_the_url_path_is_blanked(self):
        line = (
            f"HTTP Request: POST https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage "
            '"HTTP/1.1 401 Unauthorized"'
        )
        out = _redact_secrets(None, "info", {"event": line})
        assert FAKE_TOKEN.split(":", 1)[1] not in out["event"]
        assert FAKE_TOKEN not in out["event"]
        # The credential goes, the diagnosis stays.
        assert "api.telegram.org" in out["event"]
        assert "401" in out["event"]


class TestSummaryIsParseableMarkdown:
    """Regression: check IDs carry underscores, which legacy Markdown reads as
    italics. Five of them on the 10 Sep dark-check line left an entity unclosed,
    and Telegram rejects the whole message when that happens."""

    PAYLOAD = {
        "run_date": "2026-09-10",
        "universe": 528,
        "survivors": 207,
        "survival_rate": 0.392,
        "regime": {"regime": "risk_off"},
        "dark_checks": ["L1_HOLDER_CONC", "L1_MCAP_LIQ", "L1_UNLOCK"],
        "ranked": [{"rank": 1, "base_asset": "AERO", "total_score": 81.2}],
    }

    def test_no_unescaped_delimiter_outside_a_code_span(self):
        text = telegram.format_summary(self.PAYLOAD)
        outside_code = re.sub(r"`[^`]*`", "", text)
        assert re.search(r"(?<!\\)[_\[]", outside_code) is None, text
        # Our own *bold* markup is intentional; it just has to close.
        assert len(re.findall(r"(?<!\\)\*", outside_code)) % 2 == 0, text

    def test_check_ids_still_read_correctly(self):
        # Escaped in the payload; Telegram renders `L1\_HOLDER\_CONC` as L1_HOLDER_CONC.
        text = telegram.format_summary(self.PAYLOAD)
        assert r"L1\_HOLDER\_CONC" in text
        assert r"risk\_off" in text
