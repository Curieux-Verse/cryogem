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
from typing import Any

import structlog

from src.config import get_config

# Substrings that mark a field as sensitive. Matched case-insensitively against
# the KEY name, so a value never needs to be inspected to decide.
_SECRET_HINTS = ("key", "token", "secret", "password", "auth", "bearer")
_REDACTED = "***redacted***"

_configured = False


def _redact_secrets(_logger: Any, _method: str, event_dict: dict) -> dict:
    """Blank any field whose name suggests a credential."""
    for field in list(event_dict):
        lowered = field.lower()
        if any(hint in lowered for hint in _SECRET_HINTS):
            event_dict[field] = _REDACTED
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


__all__ = ["configure_logging", "get_logger"]
