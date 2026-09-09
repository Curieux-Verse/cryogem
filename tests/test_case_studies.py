"""
THE regression tests. These are the system's unit tests with known answers.

Spec: "If test_rave_fails_layer1_before_peak or test_trb_fails_layer1_before_peak
does not pass, the thresholds are wrong and Phase 4 is not done."

Each case is reconstructed from the published facts of the event. Where a figure
is not publicly established, it is marked and the test does not depend on it --
a regression test that quietly invents its inputs proves nothing.
"""

from __future__ import annotations

import pytest

from src.screening.layer1_kill import AssetSnapshot, Layer1Screener


@pytest.fixture
def screener():
    return Layer1Screener()


# =============================================================================
# RAVE -- April 2026
# =============================================================================
def rave_snapshot(days_before_peak: int) -> AssetSnapshot:
    """RAVE as it stood before the 2026-04-18 peak.

    Established facts used here:
      * ~9 wallets held ~95% of the 1B total supply; 3 team-linked wallets
        held 89.74%.
      * Launched on Binance Alpha in late 2025 with a low float.
      * Peaked at $27.88 on 2026-04-18, then fell to roughly $1 within 24h.
      * Roughly $6B of market cap evaporated on roughly $52M of liquidations.
    """
    price = 27.88 / max(1, days_before_peak / 3)
    circulating = 50_000_000  # low float against a 1B total supply
    return AssetSnapshot(
        base_asset="RAVE",
        market_cap_usd=circulating * price,
        circulating_supply=circulating,
        total_supply=1_000_000_000,
        price_change_24h_pct=25.0,
        open_interest_usd=circulating * price * 0.4,
        perp_volume_24h_usd=800_000_000,
        spot_volume_24h_usd=None,
        has_spot_pair=False,
        contract_age_days=120,          # deliberately OLD, so age cannot carry the test
        top10_holder_share=0.95,
        has_event_data=True,
        days_to_next_major_unlock=400,  # deliberately far, so unlock cannot carry it
    )


@pytest.mark.casestudy
@pytest.mark.parametrize("days_before_peak", [90, 60, 30, 14, 7, 3, 1])
def test_rave_fails_layer1_before_peak(screener, days_before_peak):
    """RAVE must FAIL at every date from launch to peak, on supply integrity."""
    result = screener.run(rave_snapshot(days_before_peak), "2026-04-18")
    assert not result.passed, f"RAVE passed L1 {days_before_peak}d before its peak"
    assert "L1_HOLDER_CONC" in result.failed_checks, (
        "the holder-concentration check must fire: 9 wallets held ~95% of supply"
    )
    assert "L1_FLOAT" in result.failed_checks, (
        "the float check must fire: 50M circulating against a 1B total supply"
    )


@pytest.mark.casestudy
def test_rave_liquidation_ratio_is_the_signature(screener):
    """~$6B of market cap destroyed on ~$52M of liquidations is ~115:1.

    That ratio is arithmetically impossible in an organic market: it means the
    market cap was a small float multiplied by a controlled price.
    """
    collapse = AssetSnapshot(
        base_asset="RAVE",
        market_cap_usd=6_000_000_000,
        circulating_supply=50_000_000,
        total_supply=1_000_000_000,
        price_change_24h_pct=-96.0,          # $27.88 -> ~$1
        market_cap_change_24h_usd=6_000_000_000,
        liquidations_24h_usd=52_000_000,
        top10_holder_share=0.95,
        has_spot_pair=False,
        contract_age_days=120,
        has_event_data=True,
        days_to_next_major_unlock=400,
    )
    result = screener.run(collapse, "2026-04-18")
    check = result.checks["L1_MCAP_LIQ"]
    assert not check.passed
    assert check.value == pytest.approx(6_000_000_000 / 52_000_000, rel=0.01)
    assert check.value > 50, "the 50:1 threshold must fire on a ~115:1 reading"


# =============================================================================
# TRB -- December 2023
# =============================================================================
def trb_snapshot(price: float) -> AssetSnapshot:
    """TRB before the December 2023 peak.

    Established facts used here:
      * ~2.75M tokens circulating.
      * ~20 whales holding ~95% of that float.
      * Thin order books.
      * Ran to $619, then fell to $136 in 13 hours.

    Note on which check fires: at $100 the market cap is ~$275M, comfortably
    above the $30M floor, so L1_MCAP does NOT catch TRB. Holder concentration
    and OI-to-market-cap do. That is the spec's "L1_MCAP (or L1_OI_MCAP)".
    """
    circulating = 2_750_000
    market_cap = circulating * price
    return AssetSnapshot(
        base_asset="TRB",
        market_cap_usd=market_cap,
        circulating_supply=circulating,
        total_supply=circulating,       # TRB float was ~the whole supply: float check will NOT fire
        price_change_24h_pct=30.0,
        # OI exceeded the circulating market cap -- the fragility signature.
        open_interest_usd=market_cap * 1.2,
        perp_volume_24h_usd=market_cap * 8,
        spot_volume_24h_usd=market_cap * 0.05,   # thin books
        has_spot_pair=True,
        contract_age_days=800,          # long-listed, so age cannot carry the test
        top10_holder_share=0.95,
        has_event_data=True,
        days_to_next_major_unlock=None,
    )


@pytest.mark.casestudy
@pytest.mark.parametrize("price", [40.0, 100.0, 250.0, 400.0, 600.0])
def test_trb_fails_layer1_before_peak(screener, price):
    """TRB must FAIL before the peak, on concentration and positioning."""
    result = screener.run(trb_snapshot(price), "2023-12-01")
    assert not result.passed, f"TRB passed L1 at ${price}"
    assert "L1_HOLDER_CONC" in result.failed_checks, (
        "~20 whales holding ~95% of a 2.75M float must fire the concentration check"
    )
    assert "L1_OI_MCAP" in result.failed_checks, (
        "open interest above circulating market cap must fire the positioning check"
    )


@pytest.mark.casestudy
def test_trb_float_check_does_not_fire_and_that_is_correct(screener):
    """TRB's float was most of its supply, so L1_FLOAT should NOT catch it.

    Asserted explicitly so nobody later 'fixes' the float check to make TRB
    fail for a second reason. TRB's problem was WHO held the float, not how
    much of the supply was circulating. A check that fires for the wrong reason
    is a check that will fire wrongly elsewhere.
    """
    result = screener.run(trb_snapshot(100.0), "2023-12-01")
    assert result.checks["L1_FLOAT"].passed


# =============================================================================
# VVV -- December 2025 to September 2026
# =============================================================================
def vvv_snapshot(top10_holder_share: float | None) -> AssetSnapshot:
    """VVV during its run.

    Established facts used here:
      * Real protocol revenue, roughly $70M to $100M annualised inside a month.
      * About 33.87M tokens burned, ~41.85% of total supply.
      * Emissions cut repeatedly: 10M -> 8M -> 6M -> 3M -> 2.5M -> 2M per year.
      * Rose more than 1500% over the period; ATH $25.96 on 2026-09-08.

    `top10_holder_share` is a PARAMETER, not a constant, because it is the one
    number that is not publicly established. Reports of top-100 holders
    controlling ~98% of supply say nothing directly about the top TEN.
    """
    circulating = 40_000_000
    price = 20.0
    return AssetSnapshot(
        base_asset="VVV",
        market_cap_usd=circulating * price,
        circulating_supply=circulating,
        total_supply=80_930_000,        # ~41.85% of it burned
        price_change_24h_pct=6.0,
        open_interest_usd=circulating * price * 0.15,
        perp_volume_24h_usd=250_000_000,
        spot_volume_24h_usd=45_000_000,
        has_spot_pair=True,
        contract_age_days=200,
        top10_holder_share=top10_holder_share,
        has_event_data=True,
        days_to_next_major_unlock=None,  # emissions falling, no cliff ahead
    )


@pytest.mark.casestudy
def test_vvv_passes_every_check_that_does_not_depend_on_holder_data(screener):
    """With holder data unavailable, VVV must fail ONLY on that check."""
    result = screener.run(vvv_snapshot(None), "2026-09-01")
    assert result.failed_checks == ["L1_HOLDER_CONC"], (
        "VVV should clear float, market cap, age, OI/mcap, perp/spot and unlock; "
        f"instead it failed {result.failed_checks}"
    )


@pytest.mark.casestudy
def test_vvv_passes_layer1_when_top10_concentration_is_moderate(screener):
    result = screener.run(vvv_snapshot(0.45), "2026-09-01")
    assert result.passed, f"VVV failed on {result.failed_checks}"


@pytest.mark.casestudy
def test_vvv_would_fail_if_top10_concentration_were_high(screener):
    """If VVV's top-10 share really is above 60%, the check FIRES -- by design.

    Spec guardrail 18.4: a threshold that disqualifies an asset the user
    believes in is a finding to record, not a bug to fix by loosening it.

    So this test asserts the check fires rather than asserting VVV passes. If
    real holder data later shows VVV above 0.60, the correct response is a
    DECISIONS.md entry about threshold calibration -- and an honest note that
    the screener would have missed a +1500% move. Not a threshold change.
    """
    result = screener.run(vvv_snapshot(0.85), "2026-09-01")
    assert not result.passed
    assert "L1_HOLDER_CONC" in result.failed_checks


# =============================================================================
# Base rates: listing day, and unlocks
# =============================================================================
def _baseline(**overrides) -> AssetSnapshot:
    """An otherwise-clean asset, so a test isolates the one check it is about."""
    base = dict(
        base_asset="TEST",
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
        days_to_next_major_unlock=None,
    )
    base.update(overrides)
    return AssetSnapshot(**base)


@pytest.mark.casestudy
@pytest.mark.parametrize("age_days", [0, 1, 14, 30, 59])
def test_listing_day_entry_is_penalised(screener, age_days):
    """Across 389 tokens on 6 CEXs in 2024: listings pumped ~87%, but 98%
    eventually dumped (~-70% from listing) and 37% hit their ATH on listing day
    and never reclaimed it. A perp inside 60 days of listing is disqualified.
    """
    result = screener.run(_baseline(contract_age_days=age_days), "2026-09-09")
    assert not result.passed
    assert "L1_AGE" in result.failed_checks


@pytest.mark.casestudy
def test_asset_just_past_the_age_window_passes(screener):
    """The boundary must be an inequality, not an off-by-one."""
    assert screener.run(_baseline(contract_age_days=60), "2026-09-09").passed


@pytest.mark.casestudy
@pytest.mark.parametrize("recipient", ["team", "investor"])
@pytest.mark.parametrize("days_out", [0, 7, 15, 29])
def test_unlock_30d_ahead_fails(screener, recipient, days_out):
    """Keyrock, 16,000+ events: ~90% negative, team worst, impact begins ~30d
    before the date. So the window is 30 days, not 7.
    """
    result = screener.run(
        _baseline(
            days_to_next_major_unlock=days_out,
            next_unlock_pct_circulating=0.08,
            next_unlock_recipient_type=recipient,
        ),
        "2026-09-09",
    )
    assert not result.passed
    assert "L1_UNLOCK" in result.failed_checks


@pytest.mark.casestudy
def test_ecosystem_unlock_does_not_fail(screener):
    """Ecosystem unlocks average slightly POSITIVE: they fund grants and
    liquidity rather than being sold. Failing them alongside team cliffs would
    discard a distinction the data supports.
    """
    result = screener.run(
        _baseline(
            days_to_next_major_unlock=10,
            next_unlock_pct_circulating=0.08,
            next_unlock_recipient_type="ecosystem",
        ),
        "2026-09-09",
    )
    assert result.passed, f"failed on {result.failed_checks}"


@pytest.mark.casestudy
def test_unlock_just_outside_the_window_passes(screener):
    result = screener.run(
        _baseline(
            days_to_next_major_unlock=31,
            next_unlock_pct_circulating=0.20,
            next_unlock_recipient_type="team",
        ),
        "2026-09-09",
    )
    assert result.passed


@pytest.mark.casestudy
def test_unlock_of_unknown_size_inside_the_window_fails(screener):
    """An unsized cliff is not a small one. Missing size must not read as safe."""
    result = screener.run(
        _baseline(
            days_to_next_major_unlock=10,
            next_unlock_pct_circulating=None,
            next_unlock_recipient_type="team",
        ),
        "2026-09-09",
    )
    assert "L1_UNLOCK" in result.failed_checks
