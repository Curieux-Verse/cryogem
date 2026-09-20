"""
# WHY: ------------------------------------------------------------------------
# Tier-3 news. READ THE HARD RULE BEFORE CHANGING ANYTHING HERE:
#
#     NOTHING IN THIS MODULE MAY FEED LAYER 1 OR LAYER 2.
#     It writes to `news_item` and nowhere else.
#
# That rule is enforced mechanically by tests/test_layer_isolation.py, which
# walks the import graph and fails CI if a path ever appears from this module
# into the screening layer. It is a rule about design, not about willpower:
# news is the most tempting thing in the system to trade on and the least
# tradeable.
#
# Why it is not a trigger: Binance's own announcement API has shown 15s+
# country-dependent lag, Telegram adds at least 150ms, Twitter is minutes late,
# and prices on other venues can move 20-100% within seconds of a listing
# notice. Commercial low-latency services exist for exactly this race. A
# polling collector loses it every time.
#
# What it IS for -- and this is genuinely valuable:
#   Every journal entry gets a `news_context` field listing headlines about that
#   ticker in the +/-24h window. After six months that answers a question with
#   real strategic value: DID THE NEWS PRECEDE THE SIGNAL, OR FOLLOW IT? If the
#   system's signals consistently follow news, it is a lagging indicator and the
#   design needs rethinking. That finding would be worth more than any alert,
#   and it costs nothing extra to collect.
#
# Sentiment: rule-based over a crypto lexicon, deliberately. A transformer is
# only justified if the rule-based version fails on a hand-labelled sample of
# 200 headlines. Do not start with the heavy option.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any

from src.collectors.base import BaseCollector, PermanentHTTPError, redact_url
from src.db.connection import get_db
from src.db.writes import json_dump, upsert
from src.logging_setup import scrub_secrets
from src.timeutil import parse_instant, utc_now, utc_now_iso

#: Free RSS feeds. No key, and they carry a real published date -- which the
#: Binance CMS endpoint no longer does, making these the only source that can
#: still measure our own news lag.
RSS_FEEDS: tuple[tuple[str, str], ...] = (
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("The Block", "https://www.theblock.co/rss.xml"),
)

#: Crypto-specific sentiment lexicon. Weighted by how strongly the term moves
#: a headline's meaning, not by how common it is.
POSITIVE_TERMS: dict[str, float] = {
    "surge": 1.0, "soar": 1.0, "rally": 0.8, "gain": 0.6, "jump": 0.8,
    "record high": 1.0, "all-time high": 1.0, "breakout": 0.8, "adoption": 0.7,
    "partnership": 0.6, "integration": 0.5, "upgrade": 0.6, "mainnet": 0.6,
    "approval": 0.9, "approved": 0.9, "buyback": 0.8, "burn": 0.7,
    "listing": 0.7, "launches": 0.4, "inflow": 0.7, "bullish": 0.8,
}
NEGATIVE_TERMS: dict[str, float] = {
    "crash": 1.0, "plunge": 1.0, "collapse": 1.0, "slump": 0.8, "tumble": 0.8,
    "hack": 1.0, "exploit": 1.0, "rug": 1.0, "scam": 1.0, "fraud": 1.0,
    "lawsuit": 0.8, "sec charges": 0.9, "investigation": 0.7, "ban": 0.8,
    "delist": 0.9, "delisting": 0.9, "liquidated": 0.8, "liquidation": 0.7,
    "outflow": 0.7, "bearish": 0.8, "unlock": 0.5, "dump": 0.9,
    "halt": 0.7, "insolvency": 1.0, "bankrupt": 1.0, "downtime": 0.5,
}

#: Tickers must appear parenthesised or $-prefixed to count. Bare uppercase
#: words in a headline are far too noisy: "US", "CEO", "ETF" are not assets.
TICKER_RE = re.compile(r"\$([A-Z]{2,10})\b|\(([A-Z]{2,10})\)")


def score_sentiment(text: str) -> tuple[str, float]:
    """Rule-based sentiment. Returns (label, score in -1..+1).

    Deliberately simple and auditable: you can read a headline and predict the
    output. A transformer would be more accurate and completely opaque, and
    this feeds labelling rather than any decision.
    """
    lowered = (text or "").lower()
    positive = sum(w for term, w in POSITIVE_TERMS.items() if term in lowered)
    negative = sum(w for term, w in NEGATIVE_TERMS.items() if term in lowered)
    total = positive + negative
    if total == 0:
        return "neutral", 0.0
    score = (positive - negative) / total
    if score > 0.2:
        return "positive", round(score, 3)
    if score < -0.2:
        return "negative", round(score, 3)
    return "neutral", round(score, 3)


def extract_assets(text: str, known_assets: set[str] | None = None) -> list[str]:
    """Tickers mentioned in a headline. Conservative by design."""
    found: set[str] = set()
    for dollar, paren in TICKER_RE.findall(text or ""):
        ticker = dollar or paren
        if ticker and (known_assets is None or ticker in known_assets):
            found.add(ticker)
    return sorted(found)


def _parse_rss(xml_text: str, source_name: str) -> list[dict[str, Any]]:
    """Parse an RSS feed into items. Tolerates the usual namespace variations."""
    items: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items

    for node in root.iter():
        if not node.tag.endswith("item"):
            continue
        fields = {child.tag.split("}")[-1]: (child.text or "").strip() for child in node}
        title = fields.get("title")
        if not title:
            continue
        items.append(
            {
                "title": title,
                "url": fields.get("link"),
                "published_raw": fields.get("pubDate") or fields.get("date"),
                "source_name": source_name,
            }
        )
    return items


def _parse_rfc822(value: str | None) -> str | None:
    """RSS dates are RFC-822. Returns our ISO instant format, or None."""
    if not value:
        return None
    from email.utils import parsedate_to_datetime

    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    from src.timeutil import format_instant

    return format_instant(parsed)


class NewsCollector(BaseCollector):
    """Tier-3 headlines for journal labelling. Never a trigger. Never scored."""

    name = "news"
    rate_limit_key = "cryptopanic"
    tier = "B"

    async def fetch(self, as_of: datetime) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []

        token = self.config.secrets.cryptopanic_auth_token
        if token:
            items.extend(await self._fetch_cryptopanic(token))
        else:
            self.log.info("cryptopanic_skipped", reason="CRYPTOPANIC_AUTH_TOKEN not set")

        # RSS is the fallback AND the lag instrument: unlike the Binance CMS
        # endpoint, these feeds still publish a real timestamp.
        async with self.client() as client:
            for source_name, url in RSS_FEEDS:
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    items.extend(_parse_rss(response.text, source_name))
                except Exception as exc:  # noqa: BLE001
                    self.warn("rss_feed_unavailable", source=source_name, error=str(exc)[:120])
        return items

    async def _fetch_cryptopanic(self, token: str) -> list[dict[str, Any]]:
        """One page of CryptoPanic posts, or [] with a warning that says why.

        The API is plan-scoped: `https://cryptopanic.com/api/<plan>/v2/posts/`.
        The free Developer plan was discontinued in early 2026 and its route
        removed, so `/api/developer/v2/posts/` answers 404 WITH OR WITHOUT a
        token -- a missing route, not a bad key (D-082). Each failure gets its
        own event so the log says what to do, instead of one generic warning
        that reads like a flaky source.
        """
        base = self.config.settings.endpoints["cryptopanic"]
        endpoint = redact_url(f"{base.rstrip('/')}/posts/")
        try:
            async with self.client(base) as client:
                payload = await self.request_json(
                    client,
                    "GET",
                    "posts/",
                    # public=true: the non-personalised feed, the documented
                    # mode for applications. The token is a query parameter by
                    # the API's design; base.redact_url keeps it out of logs.
                    params={"auth_token": token, "public": "true"},
                )
        except PermanentHTTPError as exc:
            if exc.status_code == 404:
                self.warn(
                    "cryptopanic_endpoint_not_found",
                    status=404,
                    endpoint=endpoint,
                    action=(
                        "the plan segment in endpoints.cryptopanic has no route. The free "
                        "Developer plan was discontinued (early 2026); set the base to the "
                        "account's paid plan (/api/growth/v2 or /api/enterprise/v2), or unset "
                        "CRYPTOPANIC_AUTH_TOKEN to run on RSS only. See D-082."
                    ),
                )
            elif exc.status_code in (400, 401, 403):
                self.warn(
                    "cryptopanic_auth_rejected",
                    status=exc.status_code,
                    endpoint=endpoint,
                    action=(
                        "the route exists but refused the token (CryptoPanic answers 400 "
                        "'Token not found' for an unknown key). Check that the key belongs to "
                        "an active plan matching endpoints.cryptopanic. See D-082."
                    ),
                )
            else:
                self.warn("cryptopanic_unavailable", status=exc.status_code, endpoint=endpoint)
            return []
        except Exception as exc:  # noqa: BLE001 - one source must not sink the RSS fallback
            # scrub_secrets as well as the redacted URLs: a network error from
            # httpx can carry the full request URL, token included.
            self.warn(
                "cryptopanic_unavailable",
                endpoint=endpoint,
                error=scrub_secrets(f"{type(exc).__name__}: {exc}")[:150],
            )
            return []

        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            self.warn("cryptopanic_unexpected_payload", endpoint=endpoint)
            return []

        items: list[dict[str, Any]] = []
        for post in results:
            if not isinstance(post, dict):
                continue
            items.append(
                {
                    "title": post.get("title"),
                    "url": post.get("url"),
                    "published_raw": post.get("published_at"),
                    "source_name": (post.get("source") or {}).get("title", "CryptoPanic"),
                    "iso_date": True,
                }
            )
        self.log.info("cryptopanic_fetched", posts=len(items))
        return items

    def transform(self, raw: list[dict[str, Any]], as_of: datetime) -> list[dict[str, Any]]:
        fetched_at = utc_now_iso()
        now = utc_now()
        known = self._known_assets()
        rows: list[dict[str, Any]] = []
        lags: list[float] = []

        for item in raw:
            title = item.get("title")
            url = item.get("url")
            if not title:
                continue

            published = (
                item["published_raw"]
                if item.get("iso_date")
                else _parse_rfc822(item.get("published_raw"))
            )
            if not published:
                # No publication time means no lag measurement and no window
                # membership. Skip rather than invent one.
                continue
            try:
                lag = (now - parse_instant(published)).total_seconds()
            except (ValueError, TypeError):
                continue
            lags.append(lag)

            label, score = score_sentiment(title)
            rows.append(
                {
                    "news_id": hashlib.sha256((url or title).encode("utf-8")).hexdigest()[:32],
                    "published_at_utc": published,
                    "fetched_at_utc": fetched_at,
                    # OUR lag, on every row. This is the measurement that
                    # answers "can I trade news?" from real data.
                    "lag_seconds": round(lag, 1),
                    "title": title[:500],
                    "url": url,
                    "source_name": item.get("source_name"),
                    "assets": json_dump(extract_assets(title, known)),
                    "sentiment_label": label,
                    "sentiment_score": score,
                    "event_type_guess": None,
                    "raw": None,
                }
            )

        if lags:
            ordered = sorted(lags)
            self.log.info(
                "news_lag_seconds",
                n=len(ordered),
                p50=round(ordered[len(ordered) // 2], 1),
                p95=round(ordered[int(len(ordered) * 0.95)], 1),
                note="report this distribution in RESEARCH_LOG.md after 14 days",
            )
        return rows

    def _known_assets(self) -> set[str]:
        """Restrict ticker extraction to assets we actually track."""
        with get_db() as db:
            rows = db.query(
                "SELECT DISTINCT base_asset FROM universe_snapshot WHERE exchange = 'binance' "
                "AND snapshot_date = (SELECT MAX(snapshot_date) FROM universe_snapshot "
                "WHERE exchange = 'binance')"
            )
        return {r["base_asset"] for r in rows}

    def write(self, rows: list[dict[str, Any]]) -> int:
        with get_db() as db:
            return upsert(db, "news_item", rows)


__all__ = [
    "NEGATIVE_TERMS",
    "POSITIVE_TERMS",
    "RSS_FEEDS",
    "NewsCollector",
    "extract_assets",
    "score_sentiment",
]
