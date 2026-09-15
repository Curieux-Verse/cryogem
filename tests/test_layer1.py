"""Layer 1 behaviour: the binary kill switch, and how it handles missing data."""

from __future__ import annotations

import math

import pytest

from src.events.features import compute_features
from src.screening.layer1_kill import CHECK_IDS, AssetSnapshot, Layer1Screener


@pytest.fixture
def screener():
    return Layer1Screener()


def clean(**overrides) -> AssetSnapshot:
    base = dict(
        base_asset="CLEAN",
        market_cap_usd=500_000_000,
        circulating_supply=80_000_000,
        total_supply=100_000_000,
        price_change_24h_pct=2.0,
        open_interest_usd=50_000_000,
        perp_volume_24h_usd=200_000_000,
        spot_volume_24h_usd=40_000_000,
        has_spot_pair=True,
        contract_age_days=400,
        top10_holder_share=0.25,
        has_event_data=True,
        has_unlock_record=True,
        days_to_next_major_unlock=None,
    )
    base.update(overrides)
    return AssetSnapshot(**base)


class TestVerdict:
    def test_clean_asset_passes(self, screener):
        assert screener.run(clean(), "2026-09-09").passed

    def test_all_nine_checks_always_reported(self, screener):
        result = screener.run(clean(), "2026-09-09")
        assert set(result.checks) == set(CHECK_IDS)
        assert len(CHECK_IDS) == 9

    def test_check_values_json_has_an_entry_for_every_check(self, screener):
        values = screener.run(clean(), "2026-09-09").check_values()
        assert set(values) == set(CHECK_IDS)
        for entry in values.values():
            assert "passed" in entry and "reason" in entry and "threshold" in entry

    def test_a_single_failure_disqualifies(self, screener):
        """No partial credit. One FAIL removes the asset."""
        result = screener.run(clean(top10_holder_share=0.99), "2026-09-09")
        assert not result.passed
        assert result.failed_checks == ["L1_HOLDER_CONC"]

    def test_reasons_carry_the_number_not_just_a_verdict(self, screener):
        """A failing check must state the value: the number is the argument."""
        result = screener.run(clean(top10_holder_share=0.81), "2026-09-09")
        assert "81" in result.checks["L1_HOLDER_CONC"].reason


class TestOrphanPerps:
    def test_missing_spot_pair_is_infinity_not_none(self, screener):
        """None would silently PASS a `> 40` comparison. Infinity fails it."""
        result = screener.run(clean(has_spot_pair=False, spot_volume_24h_usd=None), "2026-09-09")
        check = result.checks["L1_PERP_SPOT"]
        assert check.value == math.inf
        assert not check.passed
        assert "L1_PERP_SPOT" in result.failed_checks

    def test_infinity_renders_as_inf_not_as_a_crash(self, screener):
        result = screener.run(clean(has_spot_pair=False), "2026-09-09")
        assert result.checks["L1_PERP_SPOT"].as_dict()["value_display"] == "inf"

    def test_thin_but_present_spot_market_is_a_ratio(self, screener):
        result = screener.run(clean(spot_volume_24h_usd=1_000_000), "2026-09-09")
        check = result.checks["L1_PERP_SPOT"]
        assert check.value == pytest.approx(200.0)
        assert not check.passed


class TestMissingDataFailsClosed:
    @pytest.mark.parametrize(
        ("field", "check_id"),
        [
            ("top10_holder_share", "L1_HOLDER_CONC"),
            ("circulating_supply", "L1_FLOAT"),
            ("market_cap_usd", "L1_MCAP"),
            ("contract_age_days", "L1_AGE"),
        ],
    )
    def test_missing_input_fails_that_check(self, screener, field, check_id):
        """A screener that cannot see should say no."""
        result = screener.run(clean(**{field: None}), "2026-09-09")
        assert not result.checks[check_id].passed
        assert "data_unavailable" in result.checks[check_id].reason

    def test_unresolvable_market_cap_fails_two_checks(self, screener):
        result = screener.run(clean(market_cap_usd=None), "2026-09-09")
        assert "L1_MCAP" in result.failed_checks
        assert "L1_NO_MCAP" in result.failed_checks

    def test_no_unlock_schedule_is_not_assessed_rather_than_failed(self, screener):
        """D-041: unlock data covers ~23% of the universe; its absence is not a verdict."""
        result = screener.run(clean(has_unlock_record=False), "2026-09-09")
        assert "L1_UNLOCK" not in result.failed_checks
        assert result.checks["L1_UNLOCK"].reason.startswith("not assessed")


class TestSourceOutageIsDifferentFromAssetGap:
    """DECISIONS.md D-007. A source outage must not disqualify the market."""

    def test_dark_check_does_not_contribute_to_the_verdict(self):
        screener = Layer1Screener(dark_checks=frozenset({"L1_UNLOCK"}))
        result = screener.run(clean(has_unlock_record=False), "2026-09-09")
        assert result.passed, "a dark check must not fail the asset"

    def test_dark_check_is_still_reported_never_hidden(self):
        screener = Layer1Screener(dark_checks=frozenset({"L1_UNLOCK"}))
        result = screener.run(clean(has_unlock_record=False), "2026-09-09")
        assert result.dark_checks == ["L1_UNLOCK"]
        assert result.checks["L1_UNLOCK"].source_unavailable is True
        assert "disabled" in result.checks["L1_UNLOCK"].reason

    def test_dark_check_is_excluded_from_failed_checks(self):
        screener = Layer1Screener(dark_checks=frozenset({"L1_UNLOCK"}))
        result = screener.run(clean(has_unlock_record=False), "2026-09-09")
        assert "L1_UNLOCK" not in result.failed_checks

    def test_other_checks_still_fail_normally_while_one_is_dark(self):
        screener = Layer1Screener(dark_checks=frozenset({"L1_UNLOCK"}))
        result = screener.run(
            clean(has_unlock_record=False, top10_holder_share=0.99), "2026-09-09"
        )
        assert not result.passed
        assert result.failed_checks == ["L1_HOLDER_CONC"]

    def test_a_measured_failure_is_not_pardoned_by_a_dark_source(self):
        """D-058. Holder coverage fell under the floor, but THIS asset was measured at 99%."""
        screener = Layer1Screener(dark_checks=frozenset({"L1_HOLDER_CONC"}))
        result = screener.run(clean(top10_holder_share=0.99), "2026-09-09")
        assert not result.passed
        assert result.failed_checks == ["L1_HOLDER_CONC"]

    def test_an_unmeasured_asset_still_passes_a_dark_check(self):
        screener = Layer1Screener(dark_checks=frozenset({"L1_HOLDER_CONC"}))
        result = screener.run(clean(top10_holder_share=None), "2026-09-09")
        assert result.passed
        assert result.dark_checks == ["L1_HOLDER_CONC"]


class TestUnlockMaskingEndToEnd:
    """D-057, through the real feature code rather than hand-set snapshot fields."""

    def test_a_team_cliff_behind_an_ecosystem_unlock_fails_layer1(self, screener):
        events = [
            {"event_type": "unlock_cliff", "event_date_utc": "2026-09-14",
             "recipient_type": "ecosystem", "pct_of_circulating": 0.08},
            {"event_type": "unlock_cliff", "event_date_utc": "2026-09-29",
             "recipient_type": "team", "pct_of_circulating": 0.20},
        ]
        f = compute_features("X", events, "2026-09-09")
        result = screener.run(
            clean(
                days_to_next_major_unlock=f.days_to_next_major_unlock,
                next_unlock_pct_circulating=f.next_unlock_pct_circulating,
                next_unlock_recipient_type=f.next_unlock_recipient_type,
            ),
            "2026-09-09",
        )
        assert "L1_UNLOCK" in result.failed_checks


class TestLiquidationTriggerSymmetry:
    """D-009: a crash cannot exceed -100%, so the trigger is a ratio move."""

    def test_crash_triggers_the_check(self, screener):
        result = screener.run(
            clean(
                price_change_24h_pct=-96.0,
                market_cap_change_24h_usd=6_000_000_000,
                liquidations_24h_usd=52_000_000,
            ),
            "2026-09-09",
        )
        check = result.checks["L1_MCAP_LIQ"]
        assert "not applicable" not in check.reason, "a -96% crash must trigger the check"
        assert not check.passed

    def test_doubling_triggers_the_check(self, screener):
        result = screener.run(
            clean(
                price_change_24h_pct=150.0,
                market_cap_change_24h_usd=1_000_000_000,
                liquidations_24h_usd=5_000_000,
            ),
            "2026-09-09",
        )
        assert not result.checks["L1_MCAP_LIQ"].passed

    def test_ordinary_move_is_not_applicable(self, screener):
        result = screener.run(clean(price_change_24h_pct=30.0), "2026-09-09")
        check = result.checks["L1_MCAP_LIQ"]
        assert check.passed
        assert "not applicable" in check.reason

    def test_a_healthy_ratio_passes_but_says_it_is_a_lower_bound(self, screener):
        result = screener.run(
            clean(
                price_change_24h_pct=120.0,
                market_cap_change_24h_usd=100_000_000,
                liquidations_24h_usd=40_000_000,
            ),
            "2026-09-09",
        )
        check = result.checks["L1_MCAP_LIQ"]
        assert check.passed
        # Liquidation feeds are throttled, so a PASS is not evidence.
        assert "lower bound" in check.reason
