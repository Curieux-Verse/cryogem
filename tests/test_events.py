"""Phase 2: the event calendar and its derived features."""

from __future__ import annotations

import pytest

from src.collectors.announcements import (
    classify_title,
    extract_published_date,
    extract_tickers,
)
from src.collectors.unlocks import _normalise_recipient
from src.db.writes import upsert
from src.events.features import compute_features, load_known_events

RUN_DATE = "2026-09-09"


def _event(**overrides):
    base = {
        "event_type": "unlock_cliff",
        "event_date_utc": "2026-10-01",
        "recipient_type": "team",
        "pct_of_circulating": 0.08,
        "magnitude_tokens": 1000.0,
        "confidence": "confirmed",
        "first_seen_utc": "2026-08-01T00:00:00Z",
    }
    base.update(overrides)
    return base


class TestUnlockFeatures:
    def test_days_to_next_unlock(self):
        f = compute_features("X", [_event(event_date_utc="2026-10-01")], RUN_DATE)
        assert f.days_to_next_major_unlock == 22

    def test_recipient_type_carried_through(self):
        f = compute_features("X", [_event(recipient_type="team")], RUN_DATE)
        assert f.next_unlock_recipient_type == "team"

    def test_past_unlock_measured_backwards(self):
        f = compute_features("X", [_event(event_date_utc="2026-06-01")], RUN_DATE)
        assert f.days_since_last_major_unlock == 100
        assert f.days_to_next_major_unlock is None

    def test_overhang_cleared_when_all_cliffs_are_past(self):
        """The inverse signal: supply pressure structurally ends."""
        f = compute_features("X", [_event(event_date_utc="2026-06-01")], RUN_DATE)
        assert f.unlock_overhang_cleared is True

    def test_overhang_not_cleared_when_a_cliff_is_ahead(self):
        f = compute_features("X", [_event(event_date_utc="2026-10-01")], RUN_DATE)
        assert f.unlock_overhang_cleared is False

    def test_no_data_is_not_overhang_cleared(self):
        """An asset we know nothing about is NOT one whose overhang cleared.

        Without this distinction, absence of evidence becomes the system's
        strongest positive signal.
        """
        f = compute_features("X", [], RUN_DATE)
        assert f.unlock_overhang_cleared is False
        assert f.has_event_data is False

    def test_unsized_unlock_counts_as_major(self):
        # Missing size must not read as "small". A 20% team cliff with an
        # unfilled field would otherwise sail through.
        f = compute_features("X", [_event(pct_of_circulating=None)], RUN_DATE)
        assert f.days_to_next_major_unlock == 22

    def test_small_unlock_is_not_major(self):
        f = compute_features("X", [_event(pct_of_circulating=0.001)], RUN_DATE)
        assert f.days_to_next_major_unlock is None

    def test_event_density_counts_only_the_next_30_days(self):
        events = [
            _event(event_date_utc="2026-09-15"),
            _event(event_date_utc="2026-09-30"),
            _event(event_date_utc="2027-01-01"),
        ]
        assert compute_features("X", events, RUN_DATE).event_density_30d == 2

    def test_positive_catalyst_detected(self):
        events = [_event(event_type="mainnet", event_date_utc="2026-09-20")]
        assert compute_features("X", events, RUN_DATE).positive_catalyst_30d is True

    def test_an_ecosystem_unlock_does_not_hide_a_later_team_cliff(self):
        """D-057. The nearest major unlock was an 8% ecosystem one; Layer 1 saw
        'ecosystem' and passed, and the 20% team cliff behind it went unseen."""
        events = [
            _event(event_date_utc="2026-09-14", recipient_type="ecosystem", pct_of_circulating=0.08),
            _event(event_date_utc="2026-09-29", recipient_type="team", pct_of_circulating=0.20),
        ]
        f = compute_features("X", events, RUN_DATE)
        assert f.next_unlock_recipient_type == "team"
        assert f.next_unlock_pct_circulating == pytest.approx(0.20)
        assert f.days_to_next_major_unlock == 20

    def test_small_unlocks_to_one_recipient_add_up(self):
        """D-057. Two 3% team unlocks inside the window are one 6% event to the market."""
        events = [
            _event(event_date_utc="2026-09-19", pct_of_circulating=0.03),
            _event(event_date_utc="2026-09-29", pct_of_circulating=0.03),
        ]
        f = compute_features("X", events, RUN_DATE)
        assert f.next_unlock_pct_circulating == pytest.approx(0.06)
        assert f.days_to_next_major_unlock == 10

    def test_a_past_linear_stream_is_not_a_cleared_overhang(self):
        """D-057. A stream's end is not recorded, so it may still be vesting."""
        events = [_event(event_type="unlock_linear", event_date_utc="2026-06-01")]
        assert compute_features("X", events, RUN_DATE).unlock_overhang_cleared is False


class TestPointInTimeHonesty:
    def test_events_first_seen_after_as_of_are_invisible(self, db):
        """An unlock schedule published in June cannot inform a May decision."""
        rows = [
            {
                "event_id": "known",
                "base_asset": "X",
                "event_type": "unlock_cliff",
                "event_date_utc": "2026-10-01",
                "source": "test",
                "confidence": "confirmed",
                "first_seen_utc": "2026-08-01T00:00:00Z",
                "fetched_at_utc": "2026-08-01T00:00:00Z",
            },
            {
                "event_id": "future_knowledge",
                "base_asset": "X",
                "event_type": "unlock_cliff",
                "event_date_utc": "2026-10-15",
                "source": "test",
                "confidence": "confirmed",
                # Learned AFTER the run date -- must be invisible.
                "first_seen_utc": "2026-09-20T00:00:00Z",
                "fetched_at_utc": "2026-09-20T00:00:00Z",
            },
        ]
        upsert(db, "scheduled_event", rows)
        db.commit()

        known = load_known_events(db, as_of="2026-09-09")
        dates = {e["event_date_utc"] for e in known.get("X", [])}
        assert dates == {"2026-10-01"}, "an event learned later must not leak backwards"

    def test_later_as_of_sees_the_newer_event(self, db):
        upsert(
            db,
            "scheduled_event",
            [
                {
                    "event_id": "later",
                    "base_asset": "X",
                    "event_type": "unlock_cliff",
                    "event_date_utc": "2026-10-15",
                    "source": "test",
                    "confidence": "confirmed",
                    "first_seen_utc": "2026-09-20T00:00:00Z",
                    "fetched_at_utc": "2026-09-20T00:00:00Z",
                }
            ],
        )
        db.commit()
        assert len(load_known_events(db, as_of="2026-09-30").get("X", [])) == 1


class TestRecipientNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("team", "team"),
            ("Insiders", "team"),
            ("VC", "investor"),
            ("foundation", "ecosystem"),
            ("airdrop", "community"),
            ("something nobody has seen", None),
            (None, None),
        ],
    )
    def test_normalisation_never_guesses(self, raw, expected):
        assert _normalise_recipient(raw) == expected


class TestAnnouncementClassification:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("Binance Will List MarsCoin (MARSCOIN) with Seed Tag Applied", "listing"),
            ("Binance Futures Will Launch USDT-Margined GPROUSDT Perpetual Contract", "listing"),
            ("Binance Will Delist FOO/USDT", "delisting"),
            ("Binance Adds Monitoring Tag to Qux (QUX)", "monitoring_tag_add"),
            ("Notice on the Removal of the Monitoring Tag for Quux (QUUX)", "monitoring_tag_remove"),
            ("Weekly market wrap", None),
        ],
    )
    def test_titles_classified(self, title, expected):
        assert classify_title(title) == expected

    def test_product_addition_is_not_a_listing(self):
        """AERO listed years ago; 'Will Add ... on Earn' is not a listing."""
        title = "Binance Will Add Aerodrome (AERO) on Earn, Buy Crypto, Convert, VIP Loan & Margin"
        assert classify_title(title) is None

    def test_date_extracted_from_title_when_present(self):
        title = "Binance Futures Will Launch X Perpetual Contract (2026-09-08)"
        assert extract_published_date(title) == "2026-09-08"

    def test_undated_title_returns_none_not_today(self):
        # None must never become today's date: that fabricates a publication
        # time and corrupts both listing dates and the lag distribution.
        assert extract_published_date("Binance Will List MarsCoin (MARSCOIN)") is None

    def test_tickers_only_from_parentheses(self):
        assert extract_tickers("Binance Will List Foo (FOO) and Bar") == ["FOO"]
        assert extract_tickers("Binance Will Delist FOO/USDT") == []
