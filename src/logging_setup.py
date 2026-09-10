"""
# WHY: ------------------------------------------------------------------------
# Logging is the only thing that will tell you what a 3am unattended run did.
#
# Spec 0.1.5: "Fail loudly on data problems, silently on nothing." That rule is
# unenforceable without structured logs -- a human-readable string cannot be
# queried for "how many symbols had no market cap last Tuesday".
#
# So: JSON lines to a file (queryable, shipped as an Actions artifact), human
# text to the console (readable while developing). Same events, two renderings.
#
# One hard rule: NEVER log a secret. The repo is public and Actions logs are
# public on a public repo. Any key that reaches a log line is a leaked key, so
# redaction happens here in a processor rather than being left to call sites.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
import re
from typing import Any

import structlog

from src.config import get_config

# Substrings that mark a field as sensitive. Matched case-insensitively against
# the KEY name, so a value never needs to be inspected to decide.
_SECRET_HINTS = ("key", "token", "secret", "password", "auth", "bearer")
_REDACTED = "***redacted***"

_configured = False


#: Credential SHAPES, for fields whose NAME gives nothing away.
#:
#: Name-based redaction alone is not enough, and a security review found the
#: gap: the CryptoPanic collector logged `error=str(exc)` on failure, and httpx
#: builds its exception message from the full request URL -- query string
#: included. CryptoPanic takes its token as a query parameter, so the token
#: rode a field called "error" straight past a redactor that only inspects
#: field names, into a JSON log that CI uploads as an artifact from a PUBLIC
#: repository.
#:
#: The URL is now redacted where it enters the exception (see
#: src/collectors/base.py redact_url), which fixes that specific path. This is
#: the second, independent layer: it does not care which field the value is in
#: or what the field is called.
_SECRET_VALUE_PATTERNS = (
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"hc-ping\.com/[0-9a-f-]{36}"),
    re.compile(r'auth_?[Tt]oken=[^&\s"]+'),
    re.compile(r'api_?[Kk]ey=[^&\s"]+'),
    re.compile(r"\bbot\d{8,}:[A-Za-z0-9_-]{30,}"),
    re.compile(r'libsql://[^\s"]*\?[^\s"]*', re.I),
)


def scrub_secrets(value: str) -> str:
    """Blank any credential-shaped substring. Public: logs are not the only sink.

    A collector's error_message is recorded in collector_run and then
    republished in data/public/health.json, so an exception carrying a token
    would reach a public web page without ever passing through a log line.
    Callers that persist free-form error text run it through here.
    """
    for pattern in _SECRET_VALUE_PATTERNS:
        value = pattern.sub(_REDACTED, value)
    return value


#: Kept as the private name the log processor was written against.
_scrub_value = scrub_secrets


def _redact_secrets(_logger: Any, _method: str, event_dict: dict) -> dict:
    """Blank credentials, by field name AND by value shape.

    Two independent passes, because either alone has a blind spot. A field
    called `auth_token` is caught by name whatever it holds; a token embedded
    in a field called `error` is caught only by shape.
    """
    for field in list(event_dict):
        lowered = field.lower()
        if any(hint in lowered for hint in _SECRET_HINTS):
            event_dict[field] = _REDACTED
            continue
        value = event_dict[field]
        if isinstance(value, str) and value:
            event_dict[field] = _scrub_value(value)
    return event_dict


def configure_logging(level: str | None = None, json_file: str | None = None) -> None:
    """Set up structlog once. Safe to call repeatedly."""
    global _configured
    if _configured:
        return

    cfg = get_config()
    resolved_level = (level or cfg.settings.logging.level).upper()
    log_path = Path(json_file or cfg.path(cfg.settings.logging.json_file))
    log_path.parent.mkdir(parents=True, exist_ok=True)

    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact_secrets,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    # Console handler: human-readable. File handler: one JSON object per line.
    # Windows consoles default to cp1252, and Binance lists perps with CJK
    # symbols. Writing one to a cp1252 stream raises UnicodeEncodeError and
    # would kill a collector mid-run over a log line. Reconfigure rather than
    # let a cosmetic concern take down data collection.
    stream = sys.stderr
    try:
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, ValueError):  # pragma: no cover - non-TextIO stream
        pass
    console = logging.StreamHandler(stream)
    console.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.dev.ConsoleRenderer(colors=False)
            if cfg.settings.logging.console_human_readable
            else structlog.processors.JSONRenderer(),
            foreign_pre_chain=shared,
        )
    )
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
            foreign_pre_chain=shared,
        )
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)
    root.setLevel(resolved_level)

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    _configured = True

    missing = cfg.secrets.missing()
    if missing:
        # A single startup WARN, not one per call site. Missing keys are an
        # expected state (spec 4.2: the system must run with an empty .env).
        structlog.get_logger("startup").warning(
            "optional_secrets_unset",
            count=len(missing),
            names=sorted(missing),
            effect="affected blocks will be marked unavailable, not silently zeroed",
        )


def get_logger(name: str) -> Any:
    configure_logging()
    return structlog.get_logger(name)


__all__ = ["configure_logging", "get_logger", "scrub_secrets"]
