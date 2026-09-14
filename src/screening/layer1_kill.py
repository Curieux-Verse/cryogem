"""
# WHY: ------------------------------------------------------------------------
# Layer 1: the kill switch. The most important module in the system.
#
# BINARY. Any single FAIL removes the asset for that day. No score, no partial
# credit, no override, and no "give it a chance because the chart looks good".
# The moment a strong score can rescue a failed supply check, this stops being
# a disqualification-first system and becomes the thing it was built against.
#
# Every threshold traces to a case study or a published dataset:
#
#   L1_HOLDER_CONC  RAVE: 9 wallets held ~95% of a 1B supply, 3 team-linked
#                   wallets held 89.74%. TRB: ~20 whales, ~95% of a 2.75M float.
#   L1_FLOAT        Low-float / high-FDV traps: a small float is what makes a
#                   price controllable at all.
#   L1_OI_MCAP      Published funding-arb risk screener: warn 0.5, danger 1.0.
#   L1_PERP_SPOT    Same screener: warn 15x, danger 40x. No spot pair at all is
#                   worse than a bad ratio, so it is infinity, never null.
#   L1_AGE          389 tokens across 6 CEXs in 2024: Binance listings pumped
#                   ~87% at listing but 98% eventually dumped, ~-70% from the
#                   listing price, and 37% hit their ATH on listing day and
#                   never reclaimed it.
#   L1_MCAP         Below $30M the book cannot absorb a real position.
#   L1_UNLOCK       Keyrock, 16,000+ events: ~90% negative, team worst (toward
#                   -25%), and the decline BEGINS about 30 days before the date.
#                   Hence a 30-day lookahead rather than 7.
#   L1_NO_MCAP      If we cannot resolve what it is worth, we do not screen it.
#   L1_MCAP_LIQ     RAVE: ~$6B of market cap destroyed on ~$52M of liquidations.
#                   That ratio is arithmetically impossible in an organic
#                   market -- it means the cap was a small float times a
#                   controlled price.
#
# TWO ASYMMETRIES THAT MUST STAY IN THE READER'S HEAD:
#
#   1. L1_MCAP_LIQ is computed from THROTTLED liquidation feeds (roughly one
#      print per second per symbol). The denominator is understated, so the true
#      ratio is always HIGHER than the computed one. A FAIL is high-confidence;
#      a PASS is not evidence of anything.
#   2. Missing data for ONE asset FAILS that asset -- a screener that cannot see
#      should say no. But a source outage affecting EVERY asset disables the
#      check for the run instead, loudly. See DECISIONS.md D-007: disqualifying
#      the whole market for our own outage is not a judgement about the market.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from src.config import get_config

#: Every check the screener knows about, in report order.
CHECK_IDS = (
    "L1_HOLDER_CONC",
    "L1_FLOAT",
    "L1_OI_MCAP",
    "L1_PERP_SPOT",
    "L1_AGE",
    "L1_MCAP",
    "L1_UNLOCK",
    "L1_NO_MCAP",
    "L1_MCAP_LIQ",
)

#: Human-readable one-liners, shown on the asset page and the rejection wall.
CHECK_DESCRIPTIONS = {
    "L1_HOLDER_CONC": "Top-10 holder share, excluding known locked/bridge/staking contracts",
    "L1_FLOAT": "Circulating supply as a share of total supply",
    "L1_OI_MCAP": "Perp open-interest notional divided by circulating market cap",
    "L1_PERP_SPOT": "Perp 24h volume divided by same-venue spot 24h volume",
    "L1_AGE": "Days since the perpetual contract was listed",
    "L1_MCAP": "Circulating market cap",
    "L1_UNLOCK": "Team/investor unlock above 5% of circulating within 30 days",
    "L1_NO_MCAP": "Market cap resolvable from a market-data source",
    "L1_MCAP_LIQ": "Market-cap change divided by liquidations, on a 24h move above 100%",
}


@dataclass
class CheckResult:
    """One check's verdict for one asset."""

    check_id: str
    passed: bool
    value: float | None
    threshold: float | None
    reason: str
    #: True when this check was dark for the whole run (source outage, D-007).
    #: Such a check does NOT contribute to the verdict, and says so.
    source_unavailable: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "value": None if self.value is None or _is_nan(self.value) else self.value,
            "value_display": _display(self.value),
            "threshold": self.threshold,
            "reason": self.reason,
            "source_unavailable": self.source_unavailable,
            "description": CHECK_DESCRIPTIONS.get(self.check_id, ""),
        }


@dataclass
class Layer1Result:
    """The verdict for one asset on one day."""

    base_asset: str
    run_date: str
    passed: bool
    checks: dict[str, CheckResult] = field(default_factory=dict)

    @property
    def failed_checks(self) -> list[str]:
        return [cid for cid, c in self.checks.items() if not c.passed and not c.source_unavailable]

    @property
    def dark_checks(self) -> list[str]:
        """Checks that could not run at all this cycle. Reported, never hidden."""
        return [cid for cid, c in self.checks.items() if c.source_unavailable]

    def check_values(self) -> dict[str, Any]:
        return {cid: c.as_dict() for cid, c in self.checks.items()}


def _is_nan(value: float) -> bool:
    return isinstance(value, float) and math.isnan(value)


def _display(value: float | None) -> str:
    """Render a value for a human, including infinity, which is meaningful here."""
    if value is None:
        return "n/a"
    if isinstance(value, float):
        if math.isinf(value):
            return "inf"
        if math.isnan(value):
            return "n/a"
    return f"{value:,.4g}"


@dataclass
class AssetSnapshot:
    """Every input Layer 1 needs for one asset, assembled from the database.

    Every field is Optional on purpose. A None means "we do not know", which is
    a different state from zero and is handled differently by every check.
    """

    base_asset: str
    # -- market ---------------------------------------------------------------
    market_cap_usd: float | None = None
    circulating_supply: float | None = None
    total_supply: float | None = None
    price_change_24h_pct: float | None = None
    # -- derivatives ----------------------------------------------------------
    open_interest_usd: float | None = None
    perp_volume_24h_usd: float | None = None
    spot_volume_24h_usd: float | None = None
    has_spot_pair: bool = False
    contract_age_days: int | None = None
    # -- supply / holders -----------------------------------------------------
    top10_holder_share: float | None = None
    holder_data_quality: str | None = None
    #: measured | native_coin | unsupported_chain, from asset_contract. None
    #: means no holder row at all -- an unmeasured asset, not a native one.
    holder_applicability: str | None = None
    # -- events ---------------------------------------------------------------
    days_to_next_major_unlock: int | None = None
    next_unlock_pct_circulating: float | None = None
    next_unlock_recipient_type: str | None = None
    has_event_data: bool = False
    #: True only when an unlock SCHEDULE is on file. An asset whose only event
    #: is a listing has event data and still nothing to say about its next
    #: cliff -- see check_unlock.
    has_unlock_record: bool = False
    # -- liquidations ---------------------------------------------------------
    liquidations_24h_usd: float | None = None
    market_cap_change_24h_usd: float | None = None

    @property
    def holder_check_resolvable(self) -> bool:
        # Whether L1_HOLDER_CONC can reach a verdict: measured, or not
        # applicable. This is the input to source coverage (D-007), so the
        # native coins in the universe do not read as missing data and drag
        # coverage under the floor that switches the whole check off.
        return self.top10_holder_share is not None or self.holder_applicability == "native_coin"


class Layer1Screener:
    """Runs the nine binary checks.

    Construct once per run with the coverage map, then call `run()` per asset.
    Coverage is a run-level property (D-007), so it cannot be computed inside a
    per-asset call.
    """

    def __init__(self, dark_checks: frozenset[str] | None = None) -> None:
        self.t = get_config().thresholds.layer1
        #: Checks disabled for this run because their SOURCE is unavailable.
        self.dark = dark_checks or frozenset()

    # -- the nine checks -----------------------------------------------------
    def check_holder_concentration(self, a: AssetSnapshot) -> CheckResult:
        threshold = self.t.top10_holder_share_fail
        if a.holder_applicability == "native_coin":
            # D-035. A chain's own coin has no token contract to read holders
            # from. That is not missing data about the asset -- the check does
            # not apply -- and failing it would disqualify BTC, ETH and SOL for
            # being native.
            return CheckResult(
                "L1_HOLDER_CONC",
                True,
                None,
                threshold,
                "not applicable: a chain's native coin has no token contract to read holders from",
            )
        if a.top10_holder_share is None:
            if a.holder_applicability == "unsupported_chain":
                return self._unknown(
                    "L1_HOLDER_CONC",
                    threshold,
                    "the token's origin chain is not covered by any holder source",
                )
            return self._unknown("L1_HOLDER_CONC", threshold, "holder data unavailable")
        passed = a.top10_holder_share <= threshold
        return CheckResult(
            "L1_HOLDER_CONC",
            passed,
            a.top10_holder_share,
            threshold,
            "ok" if passed else f"top-10 hold {a.top10_holder_share:.1%}, above {threshold:.0%}",
        )

    def check_float(self, a: AssetSnapshot) -> CheckResult:
        threshold = self.t.min_circulating_ratio
        if not a.circulating_supply or not a.total_supply:
            return self._unknown("L1_FLOAT", threshold, "supply figures unavailable")
        ratio = a.circulating_supply / a.total_supply
        passed = ratio >= threshold
        return CheckResult(
            "L1_FLOAT",
            passed,
            ratio,
            threshold,
            "ok" if passed else f"only {ratio:.1%} of supply circulating, below {threshold:.0%}",
        )

    def check_oi_to_mcap(self, a: AssetSnapshot) -> CheckResult:
        threshold = self.t.oi_to_mcap_fail
        if a.open_interest_usd is None or not a.market_cap_usd:
            return self._unknown("L1_OI_MCAP", threshold, "OI or market cap unavailable")
        ratio = a.open_interest_usd / a.market_cap_usd
        passed = ratio <= threshold
        return CheckResult(
            "L1_OI_MCAP",
            passed,
            ratio,
            threshold,
            "ok" if passed else f"OI is {ratio:.2f}x market cap, above {threshold:.1f}x",
        )

    def check_perp_to_spot(self, a: AssetSnapshot) -> CheckResult:
        """No spot pair is INFINITY, never None.

        A perp whose hedge would have to live on another exchange is materially
        worse than one with a thin local spot market. None would silently pass
        a `> 40` comparison; infinity fails it, which is the honest answer.
        """
        threshold = self.t.perp_to_spot_vol_fail
        if not a.has_spot_pair or not a.spot_volume_24h_usd:
            if self.t.orphan_perp_fails:
                return CheckResult(
                    "L1_PERP_SPOT",
                    False,
                    float("inf"),
                    threshold,
                    "no same-venue spot pair: the hedge would live on another exchange",
                )
            return self._unknown("L1_PERP_SPOT", threshold, "spot volume unavailable")
        if a.perp_volume_24h_usd is None:
            return self._unknown("L1_PERP_SPOT", threshold, "perp volume unavailable")
        ratio = a.perp_volume_24h_usd / a.spot_volume_24h_usd
        passed = ratio <= threshold
        return CheckResult(
            "L1_PERP_SPOT",
            passed,
            ratio,
            threshold,
            "ok" if passed else f"perp volume is {ratio:.1f}x spot, above {threshold:.0f}x",
        )

    def check_contract_age(self, a: AssetSnapshot) -> CheckResult:
        threshold = float(self.t.min_contract_age_days)
        if a.contract_age_days is None:
            return self._unknown("L1_AGE", threshold, "listing date unavailable")
        passed = a.contract_age_days >= threshold
        return CheckResult(
            "L1_AGE",
            passed,
            float(a.contract_age_days),
            threshold,
            "ok"
            if passed
            else f"listed {a.contract_age_days}d ago, under {threshold:.0f}d",
        )

    def check_market_cap(self, a: AssetSnapshot) -> CheckResult:
        threshold = self.t.min_mcap_usd
        if a.market_cap_usd is None:
            return self._unknown("L1_MCAP", threshold, "market cap unavailable")
        passed = a.market_cap_usd >= threshold
        return CheckResult(
            "L1_MCAP",
            passed,
            a.market_cap_usd,
            threshold,
            "ok" if passed else f"${a.market_cap_usd:,.0f} is below ${threshold:,.0f}",
        )

    def check_unlock(self, a: AssetSnapshot) -> CheckResult:
        """Team/investor unlock above 5% of circulating, within 30 days.

        The 30-day window is not arbitrary: across 16,000+ events the decline
        typically BEGINS about a month before the date and stabilises within
        two weeks after. Screening 7 days out would be screening after the
        repricing has already happened.

        Recipient type is honoured because it is the most predictive field:
        ecosystem unlocks average slightly POSITIVE (grants and liquidity, not
        sales), so failing them alongside team cliffs would discard a
        distinction the data actually supports.
        """
        threshold = self.t.unlock_pct_circulating_fail
        # has_unlock_record, NOT has_event_data. An asset whose only event on
        # file is a listing has event data but no vesting schedule, and the
        # branches below would pass it as "no major unlock scheduled ahead" --
        # ignorance reported as a clean schedule. D-022 fixed this in Layer 2;
        # it survived here until 2026-09-13 (D-038).
        #
        # And no schedule on file is NOT a failure (D-041, the user's call).
        # DefiLlama covers ~23% of the universe; failing the other 77% for an
        # adapter nobody wrote would let one data source decide the whole
        # screen. The asset passes, and the reason says it was not assessed --
        # never "no major unlock scheduled ahead", which only a schedule can say.
        if not a.has_unlock_record:
            return CheckResult(
                "L1_UNLOCK",
                True,
                None,
                threshold,
                "not assessed: no unlock schedule on file for this asset",
            )
        if a.days_to_next_major_unlock is None:
            return CheckResult(
                "L1_UNLOCK", True, None, threshold, "no major unlock scheduled ahead"
            )
        if a.days_to_next_major_unlock > self.t.unlock_lookahead_days:
            return CheckResult(
                "L1_UNLOCK",
                True,
                float(a.days_to_next_major_unlock),
                threshold,
                f"next major unlock is {a.days_to_next_major_unlock}d away, outside the window",
            )

        recipient = a.next_unlock_recipient_type
        if recipient is not None and recipient not in self.t.unlock_fail_recipient_types:
            return CheckResult(
                "L1_UNLOCK",
                True,
                a.next_unlock_pct_circulating,
                threshold,
                f"unlock in {a.days_to_next_major_unlock}d but recipient is '{recipient}'",
            )

        pct = a.next_unlock_pct_circulating
        if pct is None:
            # Size unknown, inside the window, recipient team/investor or
            # unknown. Fail: an unsized cliff is not a small one.
            return CheckResult(
                "L1_UNLOCK",
                False,
                None,
                threshold,
                f"unlock in {a.days_to_next_major_unlock}d of unknown size",
            )
        passed = pct < threshold
        reason = "ok"
        if not passed:
            who = recipient or "an unknown recipient"
            reason = (
                f"{pct:.1%} of circulating unlocks in "
                f"{a.days_to_next_major_unlock}d to {who}"
            )
        return CheckResult("L1_UNLOCK", passed, pct, threshold, reason)

    def check_mcap_resolvable(self, a: AssetSnapshot) -> CheckResult:
        """If we cannot say what it is worth, we do not screen it."""
        resolvable = a.market_cap_usd is not None
        return CheckResult(
            "L1_NO_MCAP",
            resolvable or not self.t.require_mcap_data,
            1.0 if resolvable else 0.0,
            1.0,
            "resolvable" if resolvable else "not resolvable in any market-data source",
        )

    def check_mcap_to_liquidation(self, a: AssetSnapshot) -> CheckResult:
        """The RAVE detector. Only evaluated on a 24h move above 100%.

        ASYMMETRY, and it must be stated wherever this number is shown:
        liquidation feeds are throttled at source to roughly one print per
        second per symbol, so the denominator is a FLOOR. The true ratio is
        always at least the computed one. A FAIL is therefore high-confidence,
        and a PASS is not evidence of anything.
        """
        threshold = self.t.mcap_to_liquidation_ratio_fail
        trigger = self.t.mcap_to_liq_trigger_move_pct

        # SYMMETRY, and this is not a nitpick. A naive `abs(pct) > 100` trigger
        # can NEVER fire on a crash: a price cannot fall more than 100%. RAVE
        # fell ~96% -- the very event this check exists to detect -- and would
        # have been skipped as "not applicable".
        #
        # So the trigger is expressed as a RATIO move. With trigger = 1.0, it
        # fires on a doubling (ratio >= 2.0) or a halving (ratio <= 0.5), which
        # are the same size of move in opposite directions.
        if a.price_change_24h_pct is None:
            return CheckResult(
                "L1_MCAP_LIQ", True, None, threshold, "not applicable: no 24h price change"
            )
        ratio_move = 1.0 + (a.price_change_24h_pct / 100.0)
        up_trigger = 1.0 + trigger
        down_trigger = 1.0 / up_trigger
        if down_trigger < ratio_move < up_trigger:
            return CheckResult(
                "L1_MCAP_LIQ",
                True,
                None,
                threshold,
                f"not applicable: 24h move of {a.price_change_24h_pct:+.1f}% is inside "
                f"{(down_trigger - 1) * 100:+.0f}%/{(up_trigger - 1) * 100:+.0f}%",
            )
        if not a.liquidations_24h_usd or a.market_cap_change_24h_usd is None:
            return self._unknown(
                "L1_MCAP_LIQ", threshold, "liquidation data unavailable during a >100% move"
            )

        ratio = abs(a.market_cap_change_24h_usd) / a.liquidations_24h_usd
        passed = ratio <= threshold
        reason = "ok (note: liquidations are a floor, so this ratio is a lower bound)"
        if not passed:
            reason = (
                f"{abs(a.market_cap_change_24h_usd):,.0f} USD of market cap moved on "
                f"{a.liquidations_24h_usd:,.0f} USD of liquidations ({ratio:.0f}:1)"
            )
        return CheckResult("L1_MCAP_LIQ", passed, ratio, threshold, reason)

    # -- orchestration -------------------------------------------------------
    def run(self, snapshot: AssetSnapshot, run_date: str) -> Layer1Result:
        """Run all nine checks. Any single FAIL disqualifies the asset."""
        checks = {
            "L1_HOLDER_CONC": self.check_holder_concentration(snapshot),
            "L1_FLOAT": self.check_float(snapshot),
            "L1_OI_MCAP": self.check_oi_to_mcap(snapshot),
            "L1_PERP_SPOT": self.check_perp_to_spot(snapshot),
            "L1_AGE": self.check_contract_age(snapshot),
            "L1_MCAP": self.check_market_cap(snapshot),
            "L1_UNLOCK": self.check_unlock(snapshot),
            "L1_NO_MCAP": self.check_mcap_resolvable(snapshot),
            "L1_MCAP_LIQ": self.check_mcap_to_liquidation(snapshot),
        }

        # A check whose SOURCE is dark for the whole run is excluded from the
        # verdict but still reported, never hidden. See DECISIONS.md D-007.
        for check_id in self.dark:
            if check_id in checks:
                original = checks[check_id]
                checks[check_id] = CheckResult(
                    check_id,
                    passed=True,
                    value=original.value,
                    threshold=original.threshold,
                    reason="check disabled: its data source was unavailable for this run",
                    source_unavailable=True,
                )

        passed = all(c.passed for c in checks.values())
        return Layer1Result(
            base_asset=snapshot.base_asset, run_date=run_date, passed=passed, checks=checks
        )

    def _unknown(self, check_id: str, threshold: float | None, reason: str) -> CheckResult:
        """Missing input for ONE asset is a FAIL.

        Not a silent pass. A screener that cannot see should say no -- and the
        asset lands on the rejection wall with `data_unavailable` stated as the
        reason, so the gap stays visible rather than being absorbed.
        """
        return CheckResult(check_id, False, None, threshold, f"data_unavailable: {reason}")


__all__ = [
    "CHECK_DESCRIPTIONS",
    "CHECK_IDS",
    "AssetSnapshot",
    "CheckResult",
    "Layer1Result",
    "Layer1Screener",
]
