"""
R1 and R4: holder concentration and the unlock calendar, and the Layer 1
behaviour that depends on them.

Every test drives a pure function or a transform() from fixtures captured from
the live APIs on 2026-09-13, or a per-test sqlite file. NO NETWORK.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src.collectors import contracts as ct
from src.collectors.holders import HolderCollector, load_exclusions, measure
from src.collectors.unlocks import (
    UnlockCollector,
    merge_events,
    plan_fetches,
    resolve_gecko_id,
    retract_missing,
    unlock_rows,
)
from src.config import get_config
from src.db import connection as connection_module
from src.db.writes import upsert
from src.events.features import load_known_events
from src.report import daily
from src.screening.layer1_kill import AssetSnapshot, Layer1Screener
from src.screening.pipeline import (
    StaleDataError,
    _latest_derivatives,
    _latest_holders,
    _one_contract_per_asset,
    assert_fresh,
    compute_dark_checks,
)
from src.timeutil import format_instant, today_utc, utc_now
from tests.conftest import load_fixture

AS_OF = datetime(2026, 9, 13, 3, 0, 0, tzinfo=timezone.utc)
CONFIG = get_config()
PATTERNS = CONFIG.settings.holders.exclude_contract_name_patterns
EXCLUSIONS = load_exclusions(CONFIG.repo_root / "config/excluded_addresses.yaml")
CATEGORY_MAP = CONFIG.settings.unlocks.category_map
THRESHOLD = CONFIG.thresholds.layer1.top10_holder_share_fail

AERO_VOTING_ESCROW = "0xebf418fe2512e7e6bd9b87a8f0f294acdc67e6b4"
DEAD = "0x000000000000000000000000000000000000dead"


class TestOneContractPerAsset:
    """D-046. A universe snapshot written before the collision fix holds
    BOBUSDT and 1000000BOBUSDT both as BOB. The screen keys every lookup on
    base_asset, so one contract's verdict would overwrite the other's."""

    def test_the_unmultiplied_contract_is_kept(self):
        rows = [
            {"symbol": "1000000BOBUSDT", "base_asset": "BOB", "price_multiplier": 1000000},
            {"symbol": "BOBUSDT", "base_asset": "BOB", "price_multiplier": 1},
            {"symbol": "BTCUSDT", "base_asset": "BTC", "price_multiplier": 1},
        ]
        kept = _one_contract_per_asset(rows)
        assert sorted(r["symbol"] for r in kept) == ["BOBUSDT", "BTCUSDT"]

    def test_a_clean_universe_is_untouched(self):
        rows = [
            {"symbol": "BTCUSDT", "base_asset": "BTC", "price_multiplier": 1},
            {"symbol": "1000PEPEUSDT", "base_asset": "PEPE", "price_multiplier": 1000},
        ]
        assert _one_contract_per_asset(rows) == rows


class TestBinanceFreshness:
    """D-050. Hyperliquid writes derivatives and universe rows every hour. An
    unfiltered MAX() read that as fresh while the Binance feed the screen uses
    had been dead for days."""

    @staticmethod
    def _seed(db, binance_ts: str, hyperliquid_ts: str) -> None:
        for exchange, ts, symbol in (
            ("binance", binance_ts, "BTCUSDT"),
            ("hyperliquid", hyperliquid_ts, "BTC"),
        ):
            upsert(
                db,
                "derivatives_snapshot",
                [
                    {
                        "ts_utc": ts,
                        "exchange": exchange,
                        "symbol": symbol,
                        "base_asset": "BTC",
                        "open_interest_usd": 1.0,
                        "fetched_at_utc": ts,
                    }
                ],
            )
            upsert(
                db,
                "universe_snapshot",
                [
                    {
                        "snapshot_date": ts[:10],
                        "exchange": exchange,
                        "symbol": symbol,
                        "base_asset": "BTC",
                        "quote_asset": "USDT",
                        "status": "TRADING",
                        "fetched_at_utc": ts,
                    }
                ],
            )
        upsert(
            db,
            "market_snapshot",
            [
                {
                    "snapshot_date": hyperliquid_ts[:10],
                    "base_asset": "BTC",
                    "fetched_at_utc": hyperliquid_ts,
                }
            ],
        )
        db.commit()

    def test_a_dead_binance_feed_is_stale_even_when_hyperliquid_is_live(self, db):
        now = utc_now()
        self._seed(db, format_instant(now - timedelta(days=5)), format_instant(now))
        with pytest.raises(StaleDataError, match="derivatives_snapshot"):
            assert_fresh(db, today_utc())

    def test_a_live_binance_feed_passes(self, db):
        now = format_instant(utc_now())
        self._seed(db, now, now)
        assert_fresh(db, today_utc())

    def test_doctor_reports_the_dead_binance_feed(self, db):
        from src.ops.doctor import run_diagnostics

        now = utc_now()
        self._seed(db, format_instant(now - timedelta(days=5)), format_instant(now))
        problems = run_diagnostics()["problems"]
        assert any(p.startswith("derivatives_snapshot") for p in problems)

    def test_latest_derivatives_are_binance_even_when_hyperliquid_is_newer(self, db):
        self._seed(db, "2026-09-14T03:00:00Z", "2026-09-14T04:00:00Z")
        assert set(_latest_derivatives(db, "2026-09-14")) == {"BTCUSDT"}


def goplus_entry(name: str) -> dict:
    return next(iter(load_fixture(name)["result"].values()))


# =============================================================================
# R1, part 1: which contract to read
# =============================================================================
@pytest.fixture(scope="module")
def targets():
    return ct.platform_targets(
        load_fixture("coingecko_asset_platforms.json"),
        [c["id"] for c in load_fixture("goplus_supported_chains.json")["result"]],
        CONFIG.settings.holders.non_evm_goplus_ids,
    )


@pytest.fixture(scope="module")
def natives():
    return ct.native_coin_ids(load_fixture("coingecko_asset_platforms.json"))


class TestChainsAreData:
    def test_evm_platforms_map_through_their_chain_identifier(self, targets):
        assert targets["ethereum"] == "1"
        assert targets["binance-smart-chain"] == "56"
        assert targets["base"] == "8453"
        assert targets["arbitrum-one"] == "42161"
        assert targets["polygon-pos"] == "137"

    def test_solana_is_read_through_its_own_endpoint(self, targets):
        assert targets["solana"] == "solana"

    def test_a_chain_goplus_does_not_serve_is_absent(self, targets):
        assert "the-open-network" not in targets

    def test_native_coins_come_from_coingecko_not_a_hand_kept_list(self, natives):
        assert {"ethereum", "binancecoin", "solana"} <= natives


class TestClassify:
    def test_a_chains_own_coin_is_native_not_missing(self, natives, targets):
        decided = ct.classify("ethereum", {"base": "0xbridged"}, natives, targets)
        assert decided["applicability"] == ct.NATIVE_COIN
        assert decided["resolved_via"] == "native_coin_id"

    def test_a_coin_with_no_contract_anywhere_is_native(self, natives, targets):
        decided = ct.classify("ripple", {"": ""}, natives, targets)
        assert decided["applicability"] == ct.NATIVE_COIN
        assert decided["resolved_via"] == "no_platforms"

    def test_a_single_supported_platform_is_read_directly(self, natives, targets):
        decided = ct.classify("aerodrome-finance", {"base": "0x9401"}, natives, targets)
        assert (decided["applicability"], decided["goplus_chain"], decided["resolved_via"]) == (
            ct.MEASURABLE,
            "8453",
            "single_platform",
        )

    def test_a_token_only_on_an_unread_chain_is_a_gap(self, natives, targets):
        decided = ct.classify("ton-token", {"the-open-network": "EQabc"}, natives, targets)
        assert decided["applicability"] == ct.UNSUPPORTED_CHAIN

    def test_multi_chain_is_provisional_until_the_origin_is_confirmed(self, natives, targets):
        decided = ct.classify(
            "pancakeswap-token", {"binance-smart-chain": "0x0e09", "ethereum": "0x1526"}, natives, targets
        )
        assert decided["goplus_chain"] == "56"
        assert decided["resolved_via"] == ct.PROVISIONAL

    def test_a_confirmed_origin_overrides_list_order(self, natives, targets):
        decided = ct.classify(
            "aave", {"solana": "AavE1", "ethereum": "0x7fc6"}, natives, targets, origin="ethereum"
        )
        assert (decided["goplus_chain"], decided["contract_address"], decided["resolved_via"]) == (
            "1",
            "0x7fc6",
            "asset_platform_id",
        )

    def test_an_unread_origin_never_falls_back_to_a_bridge_copy(self, natives, targets):
        """A bridged copy's top holder is the bridge: measuring it would be wrong."""
        decided = ct.classify(
            "sui-token", {"sui": "0x2::a", "ethereum": "0xbridge"}, natives, targets, origin="sui"
        )
        assert decided["applicability"] == ct.UNSUPPORTED_CHAIN
        assert decided["contract_address"] is None

    def test_a_lookup_naming_no_origin_platform_means_native(self, natives, targets):
        decided = ct.classify(
            "some-l1", {"ethereum": "0xwrapped", "base": "0xw2"}, natives, targets, origin=None
        )
        assert decided["applicability"] == ct.NATIVE_COIN

    def test_transform_resolves_every_screened_coin(self):
        raw = {
            "coins": {c["id"]: c for c in load_fixture("coingecko_coins_list_platforms.json")},
            "asset_platforms": load_fixture("coingecko_asset_platforms.json"),
            "supported_chains": [c["id"] for c in load_fixture("goplus_supported_chains.json")["result"]],
            "origins": {"aave": "ethereum"},
            "fetched_at": "2026-09-13T03:00:00Z",
        }
        rows = {r["coingecko_id"]: r for r in ct.AssetContractCollector().transform(raw, AS_OF)}
        assert rows["aave"]["goplus_chain"] == "1"
        assert rows["jupiter-exchange-solana"]["goplus_chain"] == "solana"
        assert rows["ethereum"]["applicability"] == ct.NATIVE_COIN


# =============================================================================
# R1, part 2: what counts as concentration
# =============================================================================
class TestHolderExclusions:
    def test_the_raw_sum_fails_a_healthy_token(self):
        """The trap, from the live AERO payload: no exclusions reads 67%."""
        m = measure(
            goplus_entry("goplus_token_security_aero_base.json"), "8453", {}, EXCLUSIONS, PATTERNS, True
        )
        assert m["top10_share"] == pytest.approx(m["top10_share_raw"])
        assert m["top10_share"] > THRESHOLD

    def test_a_voting_escrow_is_excluded_and_leaves_the_denominator(self):
        labels = {("8453", AERO_VOTING_ESCROW): {"name": "VotingEscrow", "implementation_name": None, "is_contract": 1}}
        m = measure(
            goplus_entry("goplus_token_security_aero_base.json"), "8453", labels, EXCLUSIONS, PATTERNS, True
        )
        escrow = next(h for h in m["holders"] if h["address"] == AERO_VOTING_ESCROW)
        assert escrow["excluded"] == "contract_name:votingescrow"
        assert m["excluded_share"] == pytest.approx(0.500226, abs=1e-6)
        assert m["top10_share_raw"] == pytest.approx(0.6679367, rel=1e-6)
        assert m["top10_share"] == pytest.approx(0.1677102 / 0.4997735, rel=1e-5)
        assert m["top10_share"] < THRESHOLD

    def test_a_multisig_is_one_party_and_is_counted(self):
        entry = {
            "holders": [
                {"address": "0xsafe", "is_contract": 1, "is_locked": 0, "percent": "0.70"},
                {"address": "0xeoa", "is_contract": 0, "is_locked": 0, "percent": "0.05"},
            ]
        }
        labels = {("1", "0xsafe"): {"name": "SafeProxy", "implementation_name": "Safe", "is_contract": 1}}
        m = measure(entry, "1", labels, EXCLUSIONS, PATTERNS, True)
        assert m["top10_share"] == pytest.approx(0.75)

    def test_a_proxy_is_judged_by_its_implementation(self):
        entry = {
            "holders": [
                {"address": "0xatoken", "is_contract": 1, "is_locked": 0, "percent": "0.40"},
                {"address": "0xeoa", "is_contract": 0, "is_locked": 0, "percent": "0.10"},
            ]
        }
        labels = {
            ("1", "0xatoken"): {
                "name": "InitializableImmutableAdminUpgradeabilityProxy",
                "implementation_name": "ATokenWithDelegationInstance",
                "is_contract": 1,
            }
        }
        m = measure(entry, "1", labels, EXCLUSIONS, PATTERNS, True)
        assert m["holders"][0]["excluded"] == "contract_name:atoken"
        assert m["top10_share"] == pytest.approx(0.10 / 0.60)

    def test_burned_supply_is_excluded(self):
        # Burn addresses only. The live curated list also excludes the Binance
        # wallets in CAKE's top 10, which would test the list's contents rather
        # than the burn rule this test is about.
        burn_only = {"burn": EXCLUSIONS["burn"], "curated": {}}
        m = measure(
            goplus_entry("goplus_token_security_cake_bsc.json"), "56", {}, burn_only, PATTERNS, False
        )
        dead = next(h for h in m["holders"] if h["address"] == DEAD)
        assert dead["excluded"] == "burn_address"
        assert m["top10_share"] == pytest.approx(0.0339757385 / 0.0736881483, rel=1e-6)
        assert m["data_quality"] == "names_unavailable"

    def test_solana_readings_are_flagged_as_token_accounts(self):
        m = measure(
            goplus_entry("goplus_token_security_jup_solana.json"), "solana", {}, EXCLUSIONS, PATTERNS, False
        )
        assert m["data_quality"] == "token_accounts"
        assert all(h["address"] for h in m["holders"])

    def test_curated_wallets_and_dex_pairs_are_excluded(self):
        entry = {
            "holders": [
                {"address": "0xCEX", "is_contract": 0, "percent": "0.30"},
                {"address": "0xpool", "is_contract": 1, "percent": "0.20"},
                {"address": "0xeoa", "is_contract": 0, "percent": "0.10"},
            ],
            "dex": [{"pair": "0xPOOL"}],
        }
        exclusions = {"burn": EXCLUSIONS["burn"], "curated": {"1": {"0xcex": "cex"}}}
        m = measure(entry, "1", {}, exclusions, PATTERNS, True)
        assert [h["excluded"] for h in m["holders"]] == ["curated:cex", "dex_pair", None]
        assert m["top10_share"] == pytest.approx(0.10 / 0.50)

    def test_nothing_left_to_measure_is_unmeasured_not_zero(self):
        entry = {"holders": [{"address": DEAD, "percent": "1.0"}]}
        m = measure(entry, "1", {}, EXCLUSIONS, PATTERNS, True)
        assert m["top10_share"] is None
        assert m["data_quality"] == "all_top_holders_excluded"

    def test_transform_marks_natives_and_never_overwrites_with_nothing(self):
        raw = {
            "targets": [
                {"base_asset": "BTC", "applicability": ct.NATIVE_COIN, "goplus_chain": None, "platform": None, "contract_address": None},
                {"base_asset": "AERO", "applicability": ct.MEASURABLE, "goplus_chain": "8453", "platform": "base", "contract_address": "0x9401"},
                {"base_asset": "UNREAD", "applicability": ct.MEASURABLE, "goplus_chain": "1", "platform": "ethereum", "contract_address": "0xabc"},
            ],
            "payloads": {"AERO": goplus_entry("goplus_token_security_aero_base.json")},
            "labels": {},
            "new_labels": {("8453", AERO_VOTING_ESCROW): {"name": "VotingEscrow", "implementation_name": None, "is_contract": 1}},
            "exclusions": EXCLUSIONS,
            "snapshot_date": "2026-09-13",
            "fetched_at": "2026-09-13T03:00:00Z",
        }
        rows = HolderCollector().transform(raw, AS_OF)
        holders = {r["base_asset"]: r for r in rows if r["_kind"] == "holder"}
        assert holders["BTC"]["applicability"] == ct.NATIVE_COIN
        assert holders["BTC"]["top10_share"] is None
        assert "UNREAD" not in holders, "an unread token must not overwrite its last measurement"
        assert holders["AERO"]["top10_share"] is not None
        assert [r["address"] for r in rows if r["_kind"] == "label"] == [AERO_VOTING_ESCROW]


# =============================================================================
# Layer 1: native coins, and an unlock check that needs a schedule
# =============================================================================
def _asset(**overrides) -> AssetSnapshot:
    base = dict(
        base_asset="X",
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
        holder_applicability=ct.MEASURABLE,
        has_event_data=True,
        has_unlock_record=True,
    )
    base.update(overrides)
    return AssetSnapshot(**base)


class TestNativeCoinsAreNotDisqualified:
    def test_a_native_coin_passes_the_holder_check_as_not_applicable(self):
        result = Layer1Screener().run(
            _asset(top10_holder_share=None, holder_applicability=ct.NATIVE_COIN), "2026-09-13"
        )
        check = result.checks["L1_HOLDER_CONC"]
        assert check.passed
        assert "not applicable" in check.reason
        assert result.passed

    def test_an_unsupported_chain_still_fails_closed(self):
        result = Layer1Screener().run(
            _asset(top10_holder_share=None, holder_applicability=ct.UNSUPPORTED_CHAIN), "2026-09-13"
        )
        assert "L1_HOLDER_CONC" in result.failed_checks
        assert "origin chain" in result.checks["L1_HOLDER_CONC"].reason

    def test_measured_concentration_still_fails(self):
        result = Layer1Screener().run(_asset(top10_holder_share=0.95), "2026-09-13")
        assert "L1_HOLDER_CONC" in result.failed_checks

    def test_native_coins_count_toward_source_coverage(self):
        snapshots = [
            _asset(top10_holder_share=None, holder_applicability=ct.NATIVE_COIN) for _ in range(3)
        ] + [_asset(top10_holder_share=None, holder_applicability=None) for _ in range(7)]
        dark, coverage = compute_dark_checks(snapshots)
        assert coverage["L1_HOLDER_CONC"] == pytest.approx(0.3)
        assert "L1_HOLDER_CONC" not in dark


class TestUnlockCheckNeedsASchedule:
    def test_a_listing_on_file_is_not_reported_as_a_clean_schedule(self):
        """D-038/D-041: no schedule passes, and says it was not assessed --
        never 'no major unlock scheduled ahead', which only a schedule can say."""
        result = Layer1Screener().run(
            _asset(has_event_data=True, has_unlock_record=False), "2026-09-13"
        )
        check = result.checks["L1_UNLOCK"]
        assert check.passed
        assert check.reason.startswith("not assessed")
        assert "scheduled ahead" not in check.reason

    def test_a_real_team_unlock_inside_the_window_still_fails(self):
        result = Layer1Screener().run(
            _asset(
                has_unlock_record=True,
                days_to_next_major_unlock=11,
                next_unlock_pct_circulating=0.325,
                next_unlock_recipient_type="team",
            ),
            "2026-09-13",
        )
        assert "L1_UNLOCK" in result.failed_checks

    def test_unlock_coverage_counts_schedules_not_any_event(self):
        snapshots = [_asset(has_event_data=True, has_unlock_record=False) for _ in range(9)] + [
            _asset(has_unlock_record=True)
        ]
        _, coverage = compute_dark_checks(snapshots)
        assert coverage["L1_UNLOCK"] == pytest.approx(0.1)


# =============================================================================
# R4: the unlock calendar
# =============================================================================
ONDO_FUTURE_TS = 1800206748
ONDO_DAY = datetime.fromtimestamp(ONDO_FUTURE_TS, tz=timezone.utc).date().isoformat()


def _ondo_rows(circulating=3_000_000_000.0, cutoff="2000-01-01"):
    fixture = load_fixture("defillama_emissions_sample.json")
    return unlock_rows(
        "ondo-finance", "ONDO", fixture["metadata"]["unlockEvents"], circulating,
        CATEGORY_MAP, cutoff, "2026-09-13", "2026-09-13T03:00:00Z", 30.0,
    )


def _rows(events, circulating=1_000_000.0):
    return unlock_rows(
        "p", "P", events, circulating, CATEGORY_MAP, "2000-01-01", "2026-09-13", "2026-09-13T03:00:00Z", 30.0
    )


class TestUnlockRows:
    def test_categories_become_recipient_types(self):
        day = {r["recipient_label"]: r for r in _ondo_rows() if r["event_date_utc"] == ONDO_DAY}
        assert day["Protocol Development"]["recipient_type"] == "team"
        assert day["Private Sales"]["recipient_type"] == "investor"
        assert day["Ecosystem Growth"]["recipient_type"] == "ecosystem"
        assert day["Protocol Development"]["recipient_category"] == "insiders"

    def test_size_is_a_share_of_circulating(self):
        row = next(
            r for r in _ondo_rows(circulating=3e9)
            if r["recipient_label"] == "Protocol Development" and r["event_date_utc"] == ONDO_DAY
        )
        assert row["magnitude_tokens"] == pytest.approx(660_000_000)
        assert row["pct_of_circulating"] == pytest.approx(0.22)
        assert row["event_type"] == "unlock_cliff"
        assert row["confidence"] == "expected"

    def test_unknown_circulating_leaves_the_size_unknown_not_zero(self):
        assert all(r["pct_of_circulating"] is None for r in _ondo_rows(circulating=None))

    def test_identity_excludes_the_amount_so_a_revision_is_one_event(self):
        base = [{"timestamp": ONDO_FUTURE_TS, "cliffAllocations": [{"recipient": "Team", "category": "insiders", "amount": 100}]}]
        revised = [{"timestamp": ONDO_FUTURE_TS, "cliffAllocations": [{"recipient": "Team", "category": "insiders", "amount": 250}]}]
        assert _rows(base)[0]["event_id"] == _rows(revised)[0]["event_id"]

    def test_same_day_allocations_to_one_recipient_are_summed(self):
        events = [
            {
                "timestamp": ONDO_FUTURE_TS,
                "cliffAllocations": [
                    {"recipient": "Community", "category": "noncirculating", "amount": 125_000_000},
                    {"recipient": "Community", "category": "noncirculating", "amount": 3_210_144.66},
                ],
            }
        ]
        rows = _rows(events, circulating=1e9)
        assert len(rows) == 1
        assert rows[0]["magnitude_tokens"] == pytest.approx(128_210_144.66)

    def test_linear_vesting_is_sized_over_the_lookahead_window(self):
        events = [
            {
                "timestamp": ONDO_FUTURE_TS,
                "linearAllocations": [
                    {"recipient": "Investors", "category": "privateSale", "unlockType": "linear_rate_change",
                     "previousRatePerWeek": 0, "newRatePerWeek": 7000, "endTimestamp": ONDO_FUTURE_TS + 86400 * 365},
                    {"recipient": "Ended", "category": "insiders", "newRatePerWeek": 0},
                ],
            }
        ]
        rows = _rows(events)
        assert [r["recipient_label"] for r in rows] == ["Investors"]
        assert rows[0]["event_type"] == "unlock_linear"
        assert rows[0]["magnitude_tokens"] == pytest.approx(30_000)

    def test_uncategorized_is_stored_unlabelled(self):
        events = [{"timestamp": ONDO_FUTURE_TS, "cliffAllocations": [{"recipient": "Emissions", "category": "Uncategorized", "amount": 10}]}]
        assert _rows(events)[0]["recipient_type"] is None

    def test_history_older_than_the_cutoff_is_not_stored(self):
        assert all(r["event_date_utc"] >= "2026-01-01" for r in _ondo_rows(cutoff="2026-01-01"))


class TestPlanAndResolve:
    def test_due_matched_protocols_come_before_the_bootstrap(self):
        known = {
            "old": {"gecko_id": "ondo-finance", "last_fetched_utc": "2026-09-01T00:00:00Z"},
            "fresh": {"gecko_id": "ondo-finance", "last_fetched_utc": "2026-09-12T00:00:00Z"},
            "elsewhere": {"gecko_id": "not-screened", "last_fetched_utc": "2026-07-01T00:00:00Z"},
        }
        plan = plan_fetches(["new", "fresh", "old", "elsewhere"], known, {"ondo-finance"}, "2026-09-13", 7, 30)
        assert plan == ["old", "new", "elsewhere"]

    def test_a_gecko_id_resolves_from_a_token_reference(self):
        index = {"ethereum:0x6399c842dd2be3de30bf99bc7d1bbf6fa3650e70": "kyan"}
        assert resolve_gecko_id(None, "ethereum:0x6399C842dD2bE3dE30BF99Bc7D1bBF6Fa3650E70", index) == "kyan"
        assert resolve_gecko_id(None, "coingecko:aptos", {}) == "aptos"
        assert resolve_gecko_id("ondo-finance", "ignored:0x1", {}) == "ondo-finance"
        assert resolve_gecko_id(None, "hyperliquid:0xabc", {}) is None

    def test_two_protocols_for_one_asset_do_not_double_count(self):
        cliff = [{"timestamp": ONDO_FUTURE_TS, "cliffAllocations": [{"recipient": "Team", "category": "insiders", "amount": 100}]}]
        raw = {
            "manual": [],
            "protocols": [
                {"slug": "a-small", "gecko_id": "tok", "unlock_events": cliff},
                {"slug": "b-full", "gecko_id": "tok", "unlock_events": cliff * 2},
            ],
            "universe": {"tok": {"base_asset": "TOK", "circulating_supply": 1e6}},
            "contract_index": {},
            "fetched_at": "2026-09-13T03:00:00Z",
        }
        rows = UnlockCollector().transform(raw, AS_OF)
        assert {r["source_ref"] for r in rows if r["_kind"] == "event"} == {"b-full"}
        assert sum(1 for r in rows if r["_kind"] == "protocol") == 2


def _db_event(**overrides) -> dict:
    row = {
        "event_id": "e1",
        "base_asset": "TOK",
        "event_type": "unlock_cliff",
        "event_date_utc": "2026-10-01",
        "recipient_type": "team",
        "recipient_category": "insiders",
        "recipient_label": "Team",
        "magnitude_tokens": 100.0,
        "magnitude_usd": None,
        "pct_of_circulating": 0.08,
        "description": "Cliff",
        "source": "defillama_datasets",
        "source_ref": "slug",
        "confidence": "expected",
        "first_seen_utc": "2026-09-01T00:00:00Z",
        "fetched_at_utc": "2026-09-01T00:00:00Z",
    }
    row.update(overrides)
    return row


class TestPointInTime:
    def test_a_revision_updates_the_size_but_never_first_seen(self, db):
        merge_events(db, [_db_event()])
        merge_events(
            db,
            [_db_event(magnitude_tokens=250.0, first_seen_utc="2026-09-13T00:00:00Z", fetched_at_utc="2026-09-13T00:00:00Z")],
        )
        row = db.query_one("SELECT magnitude_tokens, first_seen_utc FROM scheduled_event WHERE event_id = 'e1'")
        assert row["magnitude_tokens"] == 250.0
        assert row["first_seen_utc"] == "2026-09-01T00:00:00Z"

    def test_a_vanished_future_event_is_retracted_not_deleted(self, db):
        merge_events(db, [_db_event(), _db_event(event_id="past", event_date_utc="2026-01-01")])
        marker = {"slug": "slug", "base_asset": "TOK", "event_ids": [], "today": "2026-09-13", "fetched_at": "2026-09-13T03:00:00Z"}
        assert retract_missing(db, [marker]) == 1
        rows = {r["event_id"]: r["retracted_utc"] for r in db.query("SELECT event_id, retracted_utc FROM scheduled_event")}
        assert rows == {"e1": "2026-09-13T03:00:00Z", "past": None}

    def test_a_retracted_event_is_visible_only_before_its_retraction(self, db):
        upsert(db, "scheduled_event", [_db_event(retracted_utc="2026-09-10T00:00:00Z")])
        assert load_known_events(db, "2026-09-09")["TOK"]
        assert "TOK" not in load_known_events(db, "2026-09-11")


# =============================================================================
# Rolling holder refresh, as the screen and the report read it
# =============================================================================
def _holder(date: str, asset: str, share: float | None, applicability: str = ct.MEASURABLE) -> dict:
    return {
        "snapshot_date": date,
        "base_asset": asset,
        "top10_share": share,
        "applicability": applicability,
        "fetched_at_utc": f"{date}T03:00:00Z",
    }


class TestRollingHolderSnapshots:
    def test_the_screen_reads_the_newest_row_per_asset_inside_the_window(self, db):
        upsert(db, "holder_snapshot", [
            _holder("2026-09-12", "A", 0.9),
            _holder("2026-09-13", "A", 0.3),
            _holder("2026-09-13", "B", None, ct.NATIVE_COIN),
            _holder("2026-09-10", "C", 0.5),
        ])
        latest = _latest_holders(db, "2026-09-13")
        assert latest["A"]["top10_share"] == 0.3
        assert latest["B"]["applicability"] == ct.NATIVE_COIN
        assert "C" not in latest, "a reading older than the age limit is not a measurement"

    def test_coverage_counts_every_batch_not_only_the_newest(self, db):
        upsert(db, "layer1_result", [
            {"run_date": "2026-09-13", "base_asset": a, "passed": 1, "failed_checks": "[]",
             "check_values": "{}", "fetched_at_utc": "2026-09-13T03:00:00Z"}
            for a in ("A", "B", "C")
        ])
        upsert(db, "holder_snapshot", [
            _holder("2026-09-12", "A", 0.3),
            _holder("2026-09-13", "B", 0.4),
            _holder("2026-09-10", "C", 0.5),
        ])
        coverage = daily.data_quality(db, "2026-09-13")["coverage"]["holder_snapshot"]
        assert coverage["assets"] == 2


# =============================================================================
# New columns must reach databases that already exist
# =============================================================================
class TestAddedColumnsReachExistingDatabases:
    def test_old_tables_gain_new_columns_and_keep_their_rows(self, tmp_path, monkeypatch):
        path = tmp_path / "old.db"
        monkeypatch.setattr(connection_module, "_sqlite_path", lambda: path)
        old = connection_module._open_sqlite()
        old.execute(
            "CREATE TABLE holder_snapshot (snapshot_date TEXT NOT NULL, base_asset TEXT NOT NULL, "
            "chain TEXT, contract_address TEXT, top10_share REAL, top50_share REAL, holder_count INTEGER, "
            "excluded_addresses TEXT, data_quality TEXT, fetched_at_utc TEXT NOT NULL, "
            "PRIMARY KEY (snapshot_date, base_asset))"
        )
        old.execute(
            "CREATE TABLE scheduled_event (event_id TEXT PRIMARY KEY, base_asset TEXT NOT NULL, "
            "event_type TEXT NOT NULL, event_date_utc TEXT NOT NULL, recipient_type TEXT, "
            "magnitude_tokens REAL, magnitude_usd REAL, pct_of_circulating REAL, description TEXT, "
            "source TEXT NOT NULL, confidence TEXT NOT NULL, first_seen_utc TEXT NOT NULL, "
            "fetched_at_utc TEXT NOT NULL)"
        )
        old.execute(
            "INSERT INTO scheduled_event (event_id, base_asset, event_type, event_date_utc, source, "
            "confidence, first_seen_utc, fetched_at_utc) VALUES ('keep', 'X', 'unlock_cliff', "
            "'2026-10-01', 'manual_calendar', 'expected', '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z')"
        )
        old.commit()

        old.apply_schema()
        old.apply_schema()  # idempotent: a second run adds nothing and raises nothing

        holder_cols = {r["name"] for r in old.query("SELECT name FROM pragma_table_info('holder_snapshot')")}
        event_cols = {r["name"] for r in old.query("SELECT name FROM pragma_table_info('scheduled_event')")}
        assert {"applicability", "top10_share_raw", "top1_share", "holders_json"} <= holder_cols
        assert {"recipient_category", "recipient_label", "source_ref", "retracted_utc"} <= event_cols
        assert old.scalar("SELECT COUNT(*) FROM scheduled_event") == 1
        assert old.scalar(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_event_source_ref'"
        ) == "idx_event_source_ref"
        old.close()


# =============================================================================
# GoPlus rate limiting arrives in the body, not the HTTP status
# =============================================================================
class TestGoPlusRateLimitIsRetried:
    """Code 4029 comes back with HTTP 200, so the base class never retried it.
    The first live run lost 136 of 315 holder reads that way."""

    TARGET = {"base_asset": "AERO", "goplus_chain": "8453", "contract_address": "0x9401"}

    def _collector(self, monkeypatch, responses):
        import src.collectors.holders as holders_module

        collector = HolderCollector()
        calls = []

        async def fake_request_json(client, method, url, **kwargs):
            calls.append(url)
            return responses.pop(0)

        async def no_sleep(seconds):
            return None

        monkeypatch.setattr(collector, "request_json", fake_request_json)
        monkeypatch.setattr(holders_module.asyncio, "sleep", no_sleep)
        return collector, calls

    def test_a_rate_limited_read_is_retried_until_it_succeeds(self, monkeypatch):
        ok = load_fixture("goplus_token_security_aero_base.json")
        collector, calls = self._collector(
            monkeypatch, [{"code": 4029, "message": "rate limit"}, {"code": 4029}, ok]
        )
        entry, code = asyncio.run(collector._read_token(None, self.TARGET))
        assert code == 1
        assert entry is not None
        assert len(calls) == 3

    def test_retries_are_bounded(self, monkeypatch):
        retries = CONFIG.settings.holders.goplus_rate_limit_retries
        collector, calls = self._collector(monkeypatch, [{"code": 4029}] * (retries + 1))
        entry, code = asyncio.run(collector._read_token(None, self.TARGET))
        assert entry is None
        assert code == 4029
        assert len(calls) == retries + 1

    def test_a_non_rate_limit_error_code_is_not_retried(self, monkeypatch):
        collector, calls = self._collector(monkeypatch, [{"code": 2, "message": "bad address"}])
        entry, code = asyncio.run(collector._read_token(None, self.TARGET))
        assert entry is None
        assert len(calls) == 1


# =============================================================================
# R5: a curated exclusion must be verifiable
# =============================================================================
class TestCuratedExclusionsAreVerifiable:
    """A wrong exclusion hides real concentration and nothing downstream will
    question it, so every curated row must say what it is and where that was
    checked. This guards the file against unverified additions."""

    def _entries(self):
        import yaml

        data = yaml.safe_load((CONFIG.repo_root / "config/excluded_addresses.yaml").read_text(encoding="utf-8"))
        for chain, rows in (data.get("addresses") or {}).items():
            for row in rows or []:
                yield str(chain), row

    def test_every_entry_has_a_label_a_source_and_a_date(self):
        entries = list(self._entries())
        assert entries, "the curated list should not be empty after R5"
        for chain, row in entries:
            assert row.get("label"), f"{chain} {row.get('address')}: no label"
            assert str(row.get("source", "")).startswith("https://"), f"{chain} {row.get('address')}: no source URL"
            assert row.get("added"), f"{chain} {row.get('address')}: no date"
            assert row.get("category") in {"cex", "bridge", "staking", "escrow", "dex", "vesting"}

    def test_no_address_is_listed_twice_on_one_chain(self):
        seen = set()
        for chain, row in self._entries():
            key = (chain, str(row["address"]).lower())
            assert key not in seen, f"duplicate exclusion {key}"
            seen.add(key)


# =============================================================================
# Even spacing: a sliding window lets the whole minute's budget go at once
# =============================================================================
class TestEvenSpacing:
    """GoPlus answers a burst with code 4029 even inside its per-minute budget.
    A source listed in rate_limit_spacing gets one call every 60/rate seconds."""

    def _collector(self, monkeypatch, spaced):
        # A frozen clock: every call arrives at the same instant, the worst burst.
        import src.collectors.base as base_module

        collector = HolderCollector()
        monkeypatch.setattr(collector.config.settings, "rate_limit_spacing", spaced)
        monkeypatch.setattr(base_module.time, "monotonic", lambda: 1000.0)
        return collector

    def test_calls_to_a_spaced_source_are_one_interval_apart(self, monkeypatch):
        collector = self._collector(monkeypatch, ["goplus"])
        interval = 60.0 / CONFIG.settings.rate_limits["goplus"]
        waits = [collector._reserve_slot("goplus") for _ in range(3)]
        assert waits == pytest.approx([0.0, interval, 2 * interval])

    def test_an_unlisted_source_is_not_slowed(self, monkeypatch):
        collector = self._collector(monkeypatch, ["goplus"])
        assert [collector._reserve_slot("blockscout") for _ in range(3)] == [0.0, 0.0, 0.0]

    def test_goplus_is_spaced_in_the_shipped_settings(self):
        assert "goplus" in get_config().settings.rate_limit_spacing
