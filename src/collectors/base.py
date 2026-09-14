"""
# WHY: ------------------------------------------------------------------------
# The contract every collector obeys, and the single most important design
# decision in the project.
#
# A collector is a STATELESS FUNCTION of a timestamp. It fetches, transforms,
# writes, and returns. It never contains a scheduling loop.
#
#     # correct -- schedule-agnostic, runs anywhere
#     async def run(self, as_of: datetime) -> CollectorRunResult
#
#     # wrong -- cannot run in CI, cannot be tested, ties the project to a laptop
#     while True:
#         collect(); time.sleep(300)
#
# Why this specific rule: the same code has to run under GitHub Actions (which
# starts a fresh process and kills it), under APScheduler on a persistent host,
# and under pytest. A `while True` inside a collector makes two of those three
# impossible, and getting into CI is what makes the dataset accumulate whether
# or not anyone opens a laptop. Binance keeps 30 days of OI history and
# Coinalyze deletes intraday data daily -- a day not collected is a day that can
# never be backtested.
#
# What this base class provides so no collector has to reimplement it:
#   * rate limiting per source (free tiers are strict; Coinalyze is 40/min)
#   * retry with exponential backoff on 429/5xx/timeouts, never on 4xx
#   * a collector_run row on BOTH the success and the failure path
#   * fetched_at_utc stamped from the ACTUAL run time, never a nominal slot
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

import asyncio
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from pyrate_limiter import Duration, Limiter, Rate
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from src.config import get_config
from src.db.connection import get_db
from src.db.writes import record_collector_run
from src.logging_setup import get_logger, scrub_secrets
from src.timeutil import utc_now, utc_now_iso

# HTTP statuses worth retrying: rate limits, IP bans, and server-side faults.
# A 400/401/403/404 is a bug in our request; retrying it just wastes the budget
# and hides the error.
RETRYABLE_STATUS = frozenset({408, 418, 425, 429, 500, 502, 503, 504})


@dataclass
class CollectorRunResult:
    """What a collector reports back. Written to collector_run either way."""

    collector: str
    run_id: str
    status: str  # 'success' | 'partial' | 'failed'
    rows_written: int
    started_at_utc: str
    ended_at_utc: str
    error_message: str | None = None
    warnings: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {"success", "partial"}

    def summary(self) -> str:
        note = f" ({self.error_message})" if self.error_message else ""
        return f"{self.collector}: {self.status}, {self.rows_written} rows{note}"


def redact_url(url: str) -> str:
    """A URL with its query string removed.

    Some APIs take credentials as a query parameter (CryptoPanic requires it),
    so a full URL is a potential credential. httpx puts the full URL into
    `str(exc)` for its own errors, and any `error=str(exc)` log field then
    carries the token into the JSON log -- which is uploaded as a CI artifact
    on a PUBLIC repository.

    Redacting at the point the URL enters an exception protects every collector
    at once, rather than relying on each one to remember.
    """
    return url.split("?", 1)[0] if url else url


class PermanentHTTPError(Exception):
    """A 4xx that will not improve on retry. Message carries no query string."""

    def __init__(self, status_code: int, url: str) -> None:
        super().__init__(f"HTTP {status_code} from {redact_url(url)}")
        self.status_code = status_code
        self.url = redact_url(url)


class RetryableHTTPError(Exception):
    """A transient HTTP failure. Distinct from a permanent one on purpose."""

    def __init__(self, status_code: int, url: str, body: str = "") -> None:
        # The path only. See redact_url: a query string can BE the credential.
        super().__init__(f"HTTP {status_code} from {redact_url(url)}: {body[:200]}")
        self.status_code = status_code
        self.url = redact_url(url)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, RetryableHTTPError):
        return True
    # Network-level failures: connect timeouts, read timeouts, DNS, resets.
    return isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError))


class BaseCollector(ABC):
    """Base class for every collector. Subclasses implement three methods.

    Subclass responsibilities:
        fetch(as_of)      -- talk to the API, return raw payloads. No DB access.
        transform(raw)    -- shape raw payloads into DB rows. No I/O at all,
                             which is what makes it unit-testable from fixtures.
        write(rows)       -- persist. Uses upsert(), so it is idempotent.

    Everything else -- retry, rate limiting, run accounting, logging -- happens
    in run() and must not be duplicated in a subclass.
    """

    #: Stable identifier, used in collector_run and the Health page.
    name: str = "unnamed"
    #: Which key in settings.rate_limits governs this collector.
    rate_limit_key: str = ""
    #: Tier C (daily), B (hourly) or A (5-minute host-only). Drives the CLI.
    tier: str = "C"

    def __init__(self) -> None:
        self.config = get_config()
        self.log = get_logger(f"collector.{self.name}")
        self._limiter = self._build_limiter()
        #: Extra limiters for collectors that talk to more than one source --
        #: a holder read hits GoPlus and a block explorer, which have separate
        #: budgets. Sharing one bucket would pace the fast source at the slow
        #: one's rate. Built lazily from settings.rate_limits.
        self._source_limiters: dict[str, Limiter | None] = {}
        #: Earliest monotonic time the next call to an evenly spaced source may go.
        self._next_slot: dict[str, float] = {}
        self._warnings: list[str] = []

    # -- infrastructure ------------------------------------------------------
    def _build_limiter(self) -> Limiter | None:
        per_minute = self.config.settings.rate_limits.get(self.rate_limit_key)
        if not per_minute:
            return None
        # raise_when_fail=False so a full bucket returns False and we sleep,
        # rather than throwing and burning a retry attempt on our own limiter.
        return Limiter(Rate(per_minute, Duration.MINUTE), raise_when_fail=False)

    def _limiter_for(self, key: str) -> Limiter | None:
        if key not in self._source_limiters:
            per_minute = self.config.settings.rate_limits.get(key)
            self._source_limiters[key] = (
                Limiter(Rate(per_minute, Duration.MINUTE), raise_when_fail=False)
                if per_minute
                else None
            )
        return self._source_limiters[key]

    async def _acquire(self, limiter_key: str | None = None) -> None:
        limiter = self._limiter if limiter_key is None else self._limiter_for(limiter_key)
        if limiter is None:
            return
        await self._space(limiter_key or self.rate_limit_key)
        bucket = self.name if limiter_key is None else f"{self.name}:{limiter_key}"
        for _ in range(600):  # bounded: never spin forever on a stuck bucket
            if limiter.try_acquire(bucket):
                return
            await asyncio.sleep(0.1)
        raise RuntimeError(f"{self.name}: rate limiter did not release within 60s")

    async def _space(self, key: str) -> None:
        # A sliding-window limiter lets the whole per-minute budget go in the
        # first seconds of the window. GoPlus answers such a burst with code 4029
        # even when the minute total is within its documented limit, so a source
        # listed in rate_limit_spacing gets one call every 60/rate seconds.
        delay = self._reserve_slot(key)
        if delay > 0:
            await asyncio.sleep(delay)

    def _reserve_slot(self, key: str) -> float:
        """Book the next evenly spaced slot for `key`; return seconds to wait."""
        settings = self.config.settings
        per_minute = settings.rate_limits.get(key)
        if key not in settings.rate_limit_spacing or not per_minute:
            return 0.0
        now = time.monotonic()
        slot = max(now, self._next_slot.get(key, now))
        self._next_slot[key] = slot + 60.0 / per_minute
        return slot - now

    def client(self, base_url: str = "", **kwargs: Any) -> httpx.AsyncClient:
        """An HTTP client with this project's timeout and user agent applied."""
        http = self.config.settings.http
        headers = {"User-Agent": http.user_agent, "Accept": "application/json"}
        headers.update(kwargs.pop("headers", {}))
        return httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(http.timeout_seconds),
            headers=headers,
            follow_redirects=True,
            **kwargs,
        )

    async def request_json(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        limiter_key: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """One rate-limited, retried HTTP call returning parsed JSON.

        Retries only transient failures. A 404 raises immediately, because a 404
        means the URL is wrong and five more attempts will not fix it.
        """
        http = self.config.settings.http

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(http.max_attempts),
            wait=wait_exponential(
                multiplier=http.backoff_initial_seconds, max=http.backoff_max_seconds
            ),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        ):
            with attempt:
                await self._acquire(limiter_key)
                response = await client.request(method, url, **kwargs)
                if response.status_code in RETRYABLE_STATUS:
                    raise RetryableHTTPError(response.status_code, str(response.url), response.text)
                if response.is_error:
                    # NOT raise_for_status(): httpx builds its message from the
                    # full request URL, query string included, and a credential
                    # passed as a query parameter would ride that message into
                    # any `error=str(exc)` log field. Raise our own instead.
                    raise PermanentHTTPError(response.status_code, str(response.url))
                return response.json()
        raise RuntimeError("unreachable: AsyncRetrying exhausted without raising")

    def warn(self, message: str, **context: Any) -> None:
        """Record a non-fatal problem. Enough of these downgrade the run to 'partial'."""
        self._warnings.append(message)
        self.log.warning(message, **context)

    # -- the three methods a subclass implements -----------------------------
    @abstractmethod
    async def fetch(self, as_of: datetime) -> Any:
        """Talk to the API. Return raw payloads. Do not touch the database."""

    @abstractmethod
    def transform(self, raw: Any, as_of: datetime) -> list[dict[str, Any]]:
        """Shape raw payloads into DB rows. Pure function: no I/O, no clock.

        Purity is the point -- it lets tests drive this from a saved fixture in
        tests/fixtures/ with no network, which is an acceptance criterion.
        """

    @abstractmethod
    def write(self, rows: list[dict[str, Any]]) -> int:
        """Persist rows idempotently. Return the number written."""

    # -- the wrapper that makes it all uniform -------------------------------
    async def run(self, as_of: datetime | None = None) -> CollectorRunResult:
        """Execute one collection cycle.

        `as_of` is the logical timestamp of the snapshot (which day/interval the
        rows belong to). It is NOT the same as fetched_at_utc, which is always
        the real wall-clock moment the data arrived. Conflating the two is how
        every downstream time calculation goes quietly wrong: a job that fires
        at 03:10 and starts at 03:12 must record 03:12.
        """
        run_id = uuid.uuid4().hex
        stamp = as_of or utc_now()
        started = utc_now_iso()
        self._warnings = []
        log = self.log.bind(run_id=run_id, as_of=stamp.isoformat())
        log.info("collector_start", tier=self.tier)

        rows_written = 0
        status = "failed"
        error_message: str | None = None
        try:
            raw = await self.fetch(stamp)
            rows = self.transform(raw, stamp)
            rows_written = self.write(rows)
            status = "partial" if self._warnings else "success"
            log.info(
                "collector_done",
                status=status,
                rows_written=rows_written,
                warnings=len(self._warnings),
            )
        # Deliberately swallowed, and NOT re-raised. One collector failing must
        # not take down the rest of its tier: the tier runner reads
        # CollectorRunResult.status, and a `failed` result is data the health
        # page and the alerting both depend on. Re-raising here would turn a
        # single dead endpoint into a total collection outage.
        except Exception as exc:  # noqa: BLE001 - see comment above
            # Scrubbed before it is recorded, not just before it is logged.
            # This string is persisted to collector_run and republished in
            # data/public/health.json, so an httpx error built from a URL with
            # a token in the query string would otherwise land on a public
            # page having never passed through the log redactor.
            error_message = scrub_secrets(f"{type(exc).__name__}: {exc}")
            # Record the failed run before returning. A collector that dies
            # without leaving a row is indistinguishable from one that never
            # ran at all.
            log.error("collector_failed", error=error_message, exc_info=True)
        finally:
            ended = utc_now_iso()
            try:
                with get_db() as db:
                    record_collector_run(
                        db=db,
                        run_id=run_id,
                        collector_name=self.name,
                        started_at_utc=started,
                        ended_at_utc=ended,
                        status=status,
                        rows_written=rows_written,
                        error_message=error_message,
                    )
            except Exception as bookkeeping_error:  # pragma: no cover
                # Never let ops bookkeeping mask the real failure.
                log.error("collector_run_record_failed", error=str(bookkeeping_error))

        return CollectorRunResult(
            collector=self.name,
            run_id=run_id,
            status=status,
            rows_written=rows_written,
            started_at_utc=started,
            ended_at_utc=ended,
            error_message=error_message,
            warnings=list(self._warnings),
        )


__all__ = [
    "PermanentHTTPError",
    "redact_url",
    "RETRYABLE_STATUS",
    "BaseCollector",
    "CollectorRunResult",
    "RetryableHTTPError",
]
