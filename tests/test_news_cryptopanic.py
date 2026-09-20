"""
D-082: CryptoPanic failures say what is wrong, and never leak the token.

No network: every response comes from an httpx.MockTransport. The response
shapes are the ones the live API returned on 2026-09-19:
  /api/developer/v2/posts/  -> 404 HTML page (route removed with the plan)
  /api/growth/v2/posts/     -> 400 {"status":"api_error","info":"Token not found"}
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from src.collectors.news import NewsCollector
from src.config import get_config

TOKEN = "cp0123456789abcdef0123456789abcdef012345"


def _collector(monkeypatch, handler) -> tuple[NewsCollector, list[tuple[str, dict[str, Any]]]]:
    collector = NewsCollector()
    original_client = collector.client

    def client(base_url: str = "", **kwargs: Any) -> httpx.AsyncClient:
        return original_client(base_url, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(collector, "client", client)
    warned: list[tuple[str, dict[str, Any]]] = []
    original_warn = collector.warn

    def warn(message: str, **context: Any) -> None:
        warned.append((message, context))
        original_warn(message, **context)

    monkeypatch.setattr(collector, "warn", warn)
    return collector, warned


def _no_token_anywhere(warned) -> None:
    assert TOKEN not in repr(warned)


def test_the_shipped_endpoint_is_not_the_removed_developer_route():
    base = get_config().settings.endpoints["cryptopanic"]
    assert "/api/developer/" not in base
    assert base.rstrip("/").endswith("/v2")


async def test_success_parses_posts_and_sends_the_documented_params(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "next": None,
                "results": [
                    {
                        "title": "$ETH (ETH) upgrade ships",
                        "url": "https://cryptopanic.com/news/1/x",
                        "published_at": "2026-09-19T08:00:00Z",
                        "source": {"title": "CoinDesk"},
                    },
                    {"title": "no source", "url": "u2", "published_at": "2026-09-19T07:00:00Z"},
                ],
            },
        )

    collector, warned = _collector(monkeypatch, handler)
    items = await collector._fetch_cryptopanic(TOKEN)

    assert warned == []
    assert [i["title"] for i in items] == ["$ETH (ETH) upgrade ships", "no source"]
    assert items[0]["source_name"] == "CoinDesk"
    assert items[1]["source_name"] == "CryptoPanic"
    assert all(i["iso_date"] for i in items)

    request = seen[0]
    base = get_config().settings.endpoints["cryptopanic"].rstrip("/")
    assert str(request.url).split("?", 1)[0] == f"{base}/posts/"
    assert request.url.params["auth_token"] == TOKEN
    assert request.url.params["public"] == "true"


async def test_a_404_is_an_actionable_endpoint_warning_not_a_silent_empty_source(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="<!DOCTYPE html><html>Not found</html>")

    collector, warned = _collector(monkeypatch, handler)
    assert await collector._fetch_cryptopanic(TOKEN) == []

    assert [m for m, _ in warned] == ["cryptopanic_endpoint_not_found"]
    context = warned[0][1]
    assert context["status"] == 404
    assert "?" not in context["endpoint"]
    assert "Developer plan was discontinued" in context["action"]
    _no_token_anywhere(warned)


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (400, {"status": "api_error", "info": "Token not found"}),
        (401, {"status": "api_error", "info": "Unauthorized"}),
        (403, {"status": "api_error", "info": "Forbidden"}),
    ],
)
async def test_a_refused_token_is_its_own_warning(monkeypatch, status, body):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    collector, warned = _collector(monkeypatch, handler)
    assert await collector._fetch_cryptopanic(TOKEN) == []

    assert [m for m, _ in warned] == ["cryptopanic_auth_rejected"]
    assert warned[0][1]["status"] == status
    _no_token_anywhere(warned)


async def test_a_permanent_error_is_not_retried(monkeypatch):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(404)

    collector, _ = _collector(monkeypatch, handler)
    await collector._fetch_cryptopanic(TOKEN)
    assert len(calls) == 1


async def test_a_network_error_carrying_the_url_is_scrubbed(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"connection refused for {request.url}", request=request)

    collector, warned = _collector(monkeypatch, handler)
    monkeypatch.setattr(collector.config.settings.http, "max_attempts", 1)
    assert await collector._fetch_cryptopanic(TOKEN) == []

    assert [m for m, _ in warned] == ["cryptopanic_unavailable"]
    _no_token_anywhere(warned)


async def test_an_unexpected_payload_warns_instead_of_raising(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "a", "dict"])

    collector, warned = _collector(monkeypatch, handler)
    assert await collector._fetch_cryptopanic(TOKEN) == []
    assert [m for m, _ in warned] == ["cryptopanic_unexpected_payload"]
