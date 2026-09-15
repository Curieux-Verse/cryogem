"""
D-052: the HTTP layer obeys rate-limit guidance instead of hammering through it.

No network: every response comes from an httpx.MockTransport.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import httpx
import pytest

from src.collectors.base import (
    MAX_RETRY_AFTER_SECONDS,
    IPBannedError,
    RetryableHTTPError,
    retry_after_seconds,
    server_hinted_wait,
)
from src.collectors.klines import BinanceKlinesCollector


def _client(collector, handler):
    return collector.client("https://example.invalid", transport=httpx.MockTransport(handler))


async def test_a_418_stops_every_later_call_without_touching_the_network():
    """Each call made during a Binance IP ban extends the ban."""
    collector = BinanceKlinesCollector()
    calls: list[str] = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(418, text="banned")

    async with _client(collector, handler) as client:
        with pytest.raises(IPBannedError):
            await collector.request_json(client, "GET", "/a")
        with pytest.raises(IPBannedError):
            await collector.request_json(client, "GET", "/b")
    assert calls == ["/a"]


async def test_retry_after_is_honoured_over_the_exponential_backoff():
    collector = BinanceKlinesCollector()
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "0"}, text="slow down"),
            httpx.Response(200, json={"ok": True}),
        ]
    )

    async with _client(collector, lambda request: next(responses)) as client:
        started = time.monotonic()
        assert await collector.request_json(client, "GET", "/a") == {"ok": True}
    # The exponential fallback's first wait is a full second.
    assert time.monotonic() - started < 0.5


def test_a_huge_retry_after_is_capped():
    exc = RetryableHTTPError(429, "https://example.invalid/a", retry_after=10_000)
    state = SimpleNamespace(outcome=SimpleNamespace(exception=lambda: exc))
    assert server_hinted_wait(lambda s: 1.0)(state) == MAX_RETRY_AFTER_SECONDS


def test_no_hint_falls_back_to_the_exponential_wait():
    exc = RetryableHTTPError(503, "https://example.invalid/a")
    state = SimpleNamespace(outcome=SimpleNamespace(exception=lambda: exc))
    assert server_hinted_wait(lambda s: 7.0)(state) == 7.0


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("5", 5.0),
        ("0", 0.0),
        ("-3", 0.0),
        (None, None),
        ("", None),
        ("Wed, 21 Oct 2026 07:28:00 GMT", None),
    ],
)
def test_retry_after_parsing(header, expected):
    assert retry_after_seconds(header) == expected
