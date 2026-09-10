"""
# WHY: ------------------------------------------------------------------------
# The repo is public and GitHub Actions logs on a public repo are public. A key
# that reaches a log line, a database column, or data/public/ is a leaked key,
# and there is no taking it back -- the log is an uploaded artifact and the
# public JSON is served from Pages.
#
# A security review found a real path: the CryptoPanic collector passes its
# token as a URL QUERY PARAMETER, httpx builds its exception message from the
# full request URL, and the collector logged `error=str(exc)`. Redaction keyed
# on field NAMES cannot see that -- the field was called "error".
#
# These tests pin all three layers of the fix:
#   1. redact_url / PermanentHTTPError, so the URL never enters the exception.
#   2. shape-based scrubbing, so a credential is caught whatever field it is in.
#   3. scrub_secrets applied to error_message BEFORE it is persisted, because
#      collector_run is republished in data/public/health.json and never passes
#      through the log redactor at all.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

import pytest

from src.collectors.base import PermanentHTTPError, redact_url
from src.logging_setup import _redact_secrets, scrub_secrets

TOKEN = "aa11bb22cc33dd44ee55"


class TestRedactUrl:
    def test_query_string_is_dropped_entirely(self):
        got = redact_url(f"https://cryptopanic.com/api/v1/posts/?auth_token={TOKEN}")
        assert TOKEN not in got
        assert got == "https://cryptopanic.com/api/v1/posts/"

    def test_a_url_without_a_query_survives_intact(self):
        url = "https://api.binance.com/fapi/v1/exchangeInfo"
        assert redact_url(url) == url

    def test_empty_input_is_returned_unchanged(self):
        assert redact_url("") == ""


class TestPermanentHTTPError:
    def test_the_message_carries_no_query_string(self):
        exc = PermanentHTTPError(401, f"https://cryptopanic.com/x?auth_token={TOKEN}")
        assert TOKEN not in str(exc)
        assert TOKEN not in exc.url
        assert exc.status_code == 401

    def test_status_code_survives_for_the_caller_to_branch_on(self):
        assert PermanentHTTPError(429, "https://x/y").status_code == 429


class TestValueShapeScrubbing:
    """Field NAME says nothing; the VALUE has to be inspected."""

    @pytest.mark.parametrize(
        "secret",
        [
            f"auth_token={TOKEN}",
            f"authToken={TOKEN}",
            f"api_key={TOKEN}",
            f"apiKey={TOKEN}",
            "github_pat_" + "A" * 24,
            "ghp_" + "b" * 36,
            "gho_" + "c" * 36,
            "hc-ping.com/2f8b0c1a-4d3e-4f5a-9b6c-7d8e9f0a1b2c",
            "bot12345678:AAaaBBbbCCccDDddEEeeFFffGGgghhhh1234",
            "libsql://db-org.turso.io?authToken=xyz",
        ],
    )
    def test_every_known_credential_shape_is_blanked(self, secret):
        haystack = f"ConnectError: GET https://host/path {secret} failed"
        got = scrub_secrets(haystack)
        assert secret not in got
        assert "***redacted***" in got

    def test_a_telegram_token_is_caught_and_not_merely_word_boundary_dropped(self):
        """Regression: this pattern once held a literal 0x08 byte, not \b.

        The escape was written into the file as a backspace character, so the
        pattern compiled and matched nothing -- a redactor that silently does
        not redact is worse than no redactor, because it is trusted.
        """
        token = "bot87654321:ZZzzYYyyXXxxWWwwVVvvUUuuTTttssss9876"
        assert scrub_secrets(token) == "***redacted***"
        assert scrub_secrets(f"sent via {token}").count("***redacted***") == 1

    def test_harmless_text_is_left_exactly_alone(self):
        for benign in (
            "528 perps -> 207 survivors",
            "https://api.binance.com/fapi/v1/exchangeInfo",
            "L1_UNLOCK dark: no unlock data for this asset",
            "rank 7 of 207",
        ):
            assert scrub_secrets(benign) == benign


class TestLogProcessor:
    def test_a_token_in_a_field_called_error_does_not_survive(self):
        event = {
            "event": "cryptopanic_unavailable",
            "error": f"HTTPStatusError: GET https://cryptopanic.com/x?auth_token={TOKEN}",
        }
        got = _redact_secrets(None, "warning", dict(event))
        assert TOKEN not in got["error"]

    def test_a_sensitive_field_name_is_blanked_whatever_it_holds(self):
        got = _redact_secrets(None, "info", {"turso_auth_token": "plainlookingvalue"})
        assert got["turso_auth_token"] == "***redacted***"

    def test_the_event_name_itself_is_never_mangled(self):
        got = _redact_secrets(None, "info", {"event": "collector_done", "rows": 207})
        assert got["event"] == "collector_done"
        assert got["rows"] == 207


class TestPersistedErrorText:
    def test_collector_error_message_is_scrubbed_before_it_is_stored(self):
        """collector_run.error_message is republished in public health.json.

        That sink never sees the log redactor, so the scrubbing has to happen
        where the message is built.
        """
        import inspect

        from src.collectors import base

        source = inspect.getsource(base.BaseCollector.run)
        assert "scrub_secrets(f\"{type(exc).__name__}" in source, (
            "error_message must be scrubbed at construction, not at log time"
        )
