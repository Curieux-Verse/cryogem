"""
# WHY: ------------------------------------------------------------------------
# Exchange announcements: listings, delistings, and monitoring tags.
#
# READ THIS BEFORE USING IT FOR ANYTHING: this collector feeds the CALENDAR and
# the JOURNAL. It is not, and must never become, a reaction trigger.
#
# The latency argument, which is settled: Binance's own announcement API has
# shown 15s+ country-dependent lag, Telegram adds at least 150ms, Twitter is
# minutes late, and asset prices on other venues can move 20-100% within
# SECONDS of a listing notice. Commercial low-latency WebSocket services exist
# for exactly this race. A polling collector will always lose it, and
# automating the attempt converts a research system into a fast way to lose
# money.
#
# What it IS for:
#   * `listing` events, so L1_AGE can be checked against a real date. Across
#     389 tokens on 6 CEXs in 2024, Binance listings pumped ~87% at listing but
#     98% eventually dumped, losing ~70% from the listing price, and 37% hit
#     their all-time high on listing day and never reclaimed it. A perp within
#     60 days of listing is disqualified on that base rate alone.
#   * `monitoring_tag_add` / `_remove`. The tag is a soft exchange warning for
#     elevated-volatility assets and is often a structural precursor to either
#     delisting or recovery. Scored as a strong negative in L2.
#   * MEASURING OUR OWN LAG. Every item stores published_at and fetched_at, so
#     after a month the distribution answers "can I trade news?" from the
#     user's own data rather than from an assertion. Expect a median well above
#     15 seconds.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from src.collectors.base import BaseCollector
from src.db.connection import get_db
from src.db.writes import deterministic_id, upsert
from src.timeutil import format_instant, from_millis, parse_instant, utc_now, utc_now_iso

#: Announcement catalogue id for new listings on Binance's public CMS endpoint.
CATALOG_NEW_LISTINGS = 48

#: Title patterns mapped to event_type. Ordered: first match wins, so more
#: specific patterns come first. A monitoring-tag removal notice also matches
#: the add pattern, and a delisting notice also looks like a listing notice.
TITLE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"remov.{0,30}monitoring tag|monitoring tag.{0,30}remov", "monitoring_tag_remove"),
    (r"monitoring tag", "monitoring_tag_add"),
    (r"delist|will remove|cease trading|terminat", "delisting"),
    # "Will Add <X> on Earn / Convert / VIP Loan" is NOT a listing -- the asset
    # is already listed and is merely reaching another product. Matching a bare
    # "will add" produced false listing events for assets listed years ago.
    (r"will list|listing of|launchpool|launchpad|megadrop", "listing"),
    (r"will launch.{0,40}perpetual contract", "listing"),
)

#: Titles that look like listings but are product additions for an asset that
#: is already trading. Checked before the listing patterns.
NOT_A_LISTING_RE = re.compile(
    r"on earn|buy crypto|convert|vip loan|collateral|trading bots|simple earn",
    re.IGNORECASE,
)

#: A genuine new-contract launch. Exempt from NOT_A_LISTING_RE because
#: "USD-Margined ... Perpetual Contract" would otherwise be excluded by any
#: pattern mentioning margin.
IS_PERP_LAUNCH_RE = re.compile(r"perpetual contract", re.IGNORECASE)

#: Tickers are announced inside parentheses: "Binance Will List Foo (FOO)".
TICKER_RE = re.compile(r"\(([A-Z0-9]{2,15})\)")


#: Binance embeds the date in many titles: "... Perpetual Contract (2026-09-08)"
#: or "... Collateral Asset - 2026-09-08". As of 2026-09-09 the CMS response
#: carries NO date field at all, so the title is the only source available.
DATE_IN_TITLE_RE = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")


def extract_published_date(title: str) -> str | None:
    """Pull a YYYY-MM-DD out of an announcement title, or None.

    None is a real answer and must NOT be replaced with today's date. Stamping
    an undated announcement with `fetched_at` fabricates a publication time,
    which corrupts two things at once: the listing date that L1_AGE checks, and
    the lag distribution that is supposed to answer "can I trade news?" from
    honest measurement.
    """
    match = DATE_IN_TITLE_RE.search(title or "")
    if not match:
        return None
    year, month, day = match.groups()
    if not (1 <= int(month) <= 12 and 1 <= int(day) <= 31):
        return None
    return f"{year}-{month}-{day}"


def classify_title(title: str) -> str | None:
    """Map an announcement title to an event_type, or None if it is neither."""
    lowered = (title or "").lower()
    for pattern, event_type in TITLE_PATTERNS:
        if re.search(pattern, lowered):
            if (
                event_type == "listing"
                and not IS_PERP_LAUNCH_RE.search(lowered)
                and NOT_A_LISTING_RE.search(lowered)
            ):
                return None
            return event_type
    return None


def extract_tickers(title: str) -> list[str]:
    """Pull ticker symbols out of a title. Empty list when none are certain.

    Deliberately conservative: only parenthesised all-caps tokens count. Free
    text is full of words that look like tickers, and a wrong attribution
    writes another asset's listing date onto this one.
    """
    return [t for t in TICKER_RE.findall(title or "") if not t.isdigit()]


class BinanceAnnouncementCollector(BaseCollector):
    """Polls Binance's public announcement catalogue for calendar events."""

    name = "announcements"
    rate_limit_key = "binance_spot"
    tier = "B"

    async def fetch(self, as_of: datetime) -> list[dict]:
        url = self.config.settings.endpoints["binance_announcements"]
        try:
            async with self.client() as client:
                payload = await self.request_json(
                    client,
                    "GET",
                    url,
                    params={"catalogId": CATALOG_NEW_LISTINGS, "pageNo": 1, "pageSize": 50},
                )
        except Exception as exc:  # noqa: BLE001
            # An undocumented CMS route, and geo-throttled. Losing it costs
            # listing dates, not the whole screen.
            self.warn(
                "announcement_endpoint_unavailable",
                error=str(exc)[:150],
                effect="listing and monitoring-tag events will not update this run",
            )
            return []

        data = payload.get("data") or {}
        # Two shapes observed. As of 2026-09-09 the live endpoint returns
        # data.articles directly; the older documented shape nested them under
        # data.catalogs[0].articles. Both are accepted so a revert upstream
        # does not silently empty the calendar.
        articles = data.get("articles")
        if articles is None:
            catalogs = data.get("catalogs") or []
            articles = catalogs[0].get("articles") if catalogs else None
        if not articles:
            self.warn("announcement_payload_empty", note="CMS response shape may have changed")
            return []
        return articles

    def transform(self, raw: list[dict], as_of: datetime) -> list[dict[str, Any]]:
        fetched_at = utc_now_iso()
        now = utc_now()
        rows: list[dict[str, Any]] = []
        lags: list[float] = []
        undated = 0

        for article in raw:
            title = article.get("title") or ""
            event_type = classify_title(title)
            if not event_type:
                continue
            tickers = extract_tickers(title)
            if not tickers:
                continue

            # Date resolution, in order of trustworthiness. As of 2026-09-09
            # the CMS response carries no date field whatsoever, so the title
            # is usually the only source. See docs/API_DEVIATIONS.md.
            release = article.get("publishDate") or article.get("releaseDate")
            if release:
                published = format_instant(from_millis(release))
                confidence = "confirmed"
                lags.append((now - parse_instant(published)).total_seconds())
            else:
                title_date = extract_published_date(title)
                if title_date:
                    published = f"{title_date}T00:00:00Z"
                    confidence = "confirmed"
                    # No lag recorded: a date without a time cannot measure
                    # seconds of latency, and a fabricated one is worse than
                    # a missing one.
                else:
                    # Undated. Record it as OUR DISCOVERY, flagged 'expected',
                    # never dressed up as a publication time.
                    published = fetched_at
                    confidence = "expected"
                    undated += 1

            article_code = article.get("code") or article.get("id") or published

            for ticker in tickers:
                rows.append(
                    {
                        "event_id": deterministic_id(
                            "binance_announcement", ticker, event_type, article_code
                        ),
                        "base_asset": ticker.upper(),
                        "event_type": event_type,
                        "event_date_utc": published[:10],
                        "recipient_type": None,
                        "magnitude_tokens": None,
                        "magnitude_usd": None,
                        "pct_of_circulating": None,
                        "description": title[:500],
                        "source": "binance_announcements",
                        "confidence": confidence,
                        "first_seen_utc": fetched_at,
                        "fetched_at_utc": fetched_at,
                    }
                )

        if undated:
            self.warn(
                "announcements_without_a_date",
                count=undated,
                effect="stored with confidence='expected' and our discovery time",
                note="never backfilled with today's date -- that would fabricate a publish time",
            )

        if lags:
            ordered = sorted(lags)
            # Our OWN lag, logged every run. After a month this becomes the
            # honest, personal answer to "can I trade news?" rather than an
            # assertion someone made on the internet.
            self.log.info(
                "announcement_lag_seconds",
                n=len(ordered),
                p50=round(ordered[len(ordered) // 2], 1),
                worst=round(ordered[-1], 1),
                note="this is why announcements feed the calendar, never a trigger",
            )
        return rows

    def write(self, rows: list[dict[str, Any]]) -> int:
        """Insert only genuinely new events, preserving the original first_seen_utc."""
        if not rows:
            return 0
        with get_db() as db:
            existing = {
                r["event_id"]
                for r in db.query(
                    "SELECT event_id FROM scheduled_event WHERE event_id IN "
                    f"({','.join('?' for _ in rows)})",
                    [r["event_id"] for r in rows],
                )
            }
            fresh = [r for r in rows if r["event_id"] not in existing]
            return upsert(db, "scheduled_event", fresh) if fresh else 0


__all__ = [
    "CATALOG_NEW_LISTINGS",
    "BinanceAnnouncementCollector",
    "classify_title",
    "extract_published_date",
    "extract_tickers",
]
