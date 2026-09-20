"""
D-083: the holder-concentration audit of 2026-09-19.

The holder lists below are trimmed copies of live GoPlus token_security reads
taken that day. No network.

Two findings are pinned here:
  1. The effective share K / (1 - E) equals K / (K + T), where T is the supply
     held outside the top 10. It reaches 100% only when T is ~0. Exclusions can
     only lower it, so the formula is not what drove the 99-100% readings.
  2. All 12 of the exact-100% readings came from FROZEN GoPlus holder lists
     (1-6 holders, against 1,459-25,761 on Blockscout). These are now recorded
     as unmeasured. That is still a Layer 1 FAIL, but with the true reason.
"""

from __future__ import annotations

import pytest
import yaml

from src.collectors.holders import load_exclusions, measure
from src.config import REPO_ROOT, get_config
from src.screening.layer1_kill import AssetSnapshot, Layer1Screener

EXCLUSIONS = load_exclusions(REPO_ROOT / "config" / "excluded_addresses.yaml")
THRESHOLD = get_config().thresholds.layer1.top10_holder_share_fail
HOLDERS_CFG = get_config().settings.holders
GUARDS = {
    "min_holder_count": HOLDERS_CFG.min_goplus_holder_count,
    "min_float": HOLDERS_CFG.min_measurable_float,
}

#: AZTEC on Ethereum, as GoPlus served it: 2 holders. Blockscout: 14,913.
AZTEC = {
    "holder_count": "2",
    "holders": [
        {"address": "0x92ba0fd39658105fac4df2b9bade998b5816b350", "is_contract": 0, "is_locked": 0, "percent": "0.5"},
        {"address": "0x13620833364653fa125ccdd7cf54b9e4a22ab6d9", "is_contract": 0, "is_locked": 0, "percent": "0.5"},
    ],
}

#: SFP on Ethereum: 831 holders, and GoPlus agrees with Blockscout (827).
SFP = {
    "holder_count": "831",
    "holders": [
        {"address": "0x5a52e96bacdabb82fd05763e25335261b270efcb", "is_contract": 0, "percent": "0.457827"},  # Binance 28
        {"address": "0x7c7dd26c3fd211b53888daf7f8cf0ade9be2ef3f", "is_contract": 0, "percent": "0.347582"},  # untagged EOA
        {"address": "0xf977814e90da44bfa03b6295a0616a897441acec", "is_contract": 0, "percent": "0.150039"},  # Binance HW 20
        {"address": "0xe49f4bbb9c6319df390f1070a3895fcc99b1f244", "is_contract": 0, "percent": "0.043001"},  # untagged EOA
        {"address": "0xcffad3200574698b78f32232aa9d63eabd290703", "is_contract": 0, "percent": "0.000253"},  # Crypto.com 16
        {"address": "0xbe2b44ea304fbf0f02a0bfcd75b6628152e89e10", "is_contract": 0, "percent": "0.000219"},
        {"address": "0x98adef6f2ac8572ec48965509d69a8dd5e8bba9d", "is_contract": 0, "percent": "0.000146"},
        {"address": "0x7c8afbbd9ce244a3792c1f0a67f6c0ee7228c762", "is_contract": 0, "percent": "0.000116"},
        {"address": "0x0c3a20a90e7361f200703af34b98869b11495deb", "is_contract": 0, "percent": "0.000087"},
        {"address": "0xe68aac04f6ce33391056f76522f999be9af85eac", "is_contract": 0, "percent": "0.000040"},
    ],
}


def _measure(entry, chain="1", **guards):
    return measure(entry, chain, {}, EXCLUSIONS, [], True, **guards)


class TestTheFormula:
    def test_effective_share_is_kept_over_kept_plus_tail(self):
        m = _measure(SFP, **GUARDS)
        kept = sum(h["percent"] for h in m["holders"] if not h["excluded"])
        tail = 1.0 - m["top10_share_raw"]
        assert m["top10_share"] == pytest.approx(kept / (kept + tail), rel=1e-6)

    def test_sfp_is_a_real_reading_and_still_fails(self):
        """The top 10 hold 99.93% of Ethereum SFP, so T is ~0.07%. That is a
        measurement, not an artefact: one untagged EOA holds 35% of this chain's
        supply. Neither guard fires, and the kill switch still fires."""
        m = _measure(SFP, **GUARDS)
        assert m["data_quality"] == "complete"
        assert m["excluded_share"] == pytest.approx(0.608119, abs=1e-6)
        assert m["top10_share"] == pytest.approx(0.99824, abs=1e-4)
        assert m["top10_share"] > THRESHOLD

    def test_an_exclusion_can_only_lower_the_share(self):
        before = _measure(SFP)["top10_share"]
        extra = {
            "burn": EXCLUSIONS["burn"],
            "curated": {"1": {**EXCLUSIONS["curated"]["1"], "0x7c7dd26c3fd211b53888daf7f8cf0ade9be2ef3f": "test"}},
        }
        after = measure(SFP, "1", {}, extra, [], True)["top10_share"]
        assert after < before


class TestStaleHolderList:
    def test_without_the_guard_a_frozen_list_reads_exactly_100_percent(self):
        m = _measure(AZTEC)
        assert m["top10_share"] == pytest.approx(1.0)

    def test_with_the_guard_it_is_unmeasured_not_100_percent(self):
        m = _measure(AZTEC, **GUARDS)
        assert m["top10_share"] is None
        assert m["top1_share"] is None
        assert m["data_quality"] == "stale_holder_list"
        # The raw evidence is kept for the audit trail.
        assert m["top10_share_raw"] == pytest.approx(1.0)
        assert len(m["holders"]) == 2

    def test_a_missing_holder_count_does_not_trigger_the_guard(self):
        entry = {k: v for k, v in AZTEC.items() if k != "holder_count"}
        assert _measure(entry, **GUARDS)["data_quality"] == "complete"

    def test_the_shipped_floor_sits_between_frozen_and_accurate_lists(self):
        # Frozen lists seen: 1-6, and SENT at 29. Accurate: Q 203, JCT 520.
        assert 29 < HOLDERS_CFG.min_goplus_holder_count <= 203


def test_the_collector_applies_the_configured_guard(as_of):
    """End to end through transform(): the row carries no share, and says why."""
    from src.collectors.holders import HolderCollector

    raw = {
        "targets": [
            {"base_asset": "AZTEC", "applicability": "measurable", "goplus_chain": "1",
             "platform": "ethereum", "contract_address": "0xa27ec0006e59f245217ff08cd52a7e8b169e62d2"},
        ],
        "payloads": {"AZTEC": AZTEC},
        "labels": {},
        "new_labels": {},
        "exclusions": EXCLUSIONS,
        "snapshot_date": "2026-09-19",
        "fetched_at": "2026-09-19T03:00:00Z",
    }
    (row,) = HolderCollector().transform(raw, as_of)
    assert row["top10_share"] is None
    assert row["data_quality"] == "stale_holder_list"
    assert row["top10_share_raw"] == pytest.approx(1.0)
    assert row["holder_count"] == 2


class TestTinyFloat:
    @staticmethod
    def _entry(excluded: float, kept: float) -> dict:
        return {
            "holder_count": "5000",
            "holders": [
                {"address": "0x000000000000000000000000000000000000dead", "percent": str(excluded)},
                {"address": "0xwhale", "is_contract": 0, "percent": str(kept)},
            ],
        }

    def test_a_sliver_of_float_is_not_judged(self):
        m = _measure(self._entry(0.97, 0.029), **GUARDS)
        assert m["top10_share"] is None
        assert m["data_quality"] == "insufficient_float"

    def test_a_normal_float_is_judged(self):
        m = _measure(self._entry(0.50, 0.40), **GUARDS)
        assert m["top10_share"] == pytest.approx(0.40 / 0.50)


class TestTheKillSwitchStillKills:
    def test_rave_shape_is_measured_and_fails(self):
        """RAVE, the case the check exists for: 13,559 holders, one contract at 78%."""
        entry = {
            "holder_count": "13559",
            "holders": [
                {"address": "0x9831156f1a6e506fca41503590b42f07c2e80f54", "is_contract": 1, "percent": "0.783963"},
                {"address": "0x6020656d1ef182173e45d4fc375bdd5a48c674b0", "is_contract": 1, "percent": "0.093472"},
                {"address": "0xeoa", "is_contract": 0, "percent": "0.05"},
            ],
        }
        m = _measure(entry, **GUARDS)
        assert m["top10_share"] > THRESHOLD

    @pytest.mark.parametrize(
        ("quality", "phrase"),
        [
            ("stale_holder_list", "frozen, too-short holder list"),
            ("insufficient_float", "too little supply remains"),
        ],
    )
    def test_an_unmeasured_asset_fails_with_the_reason_stated(self, quality, phrase):
        snap = AssetSnapshot(
            base_asset="AZTEC",
            top10_holder_share=None,
            holder_data_quality=quality,
            holder_applicability="measurable",
        )
        result = Layer1Screener().check_holder_concentration(snap)
        assert result.passed is False
        assert result.reason.startswith("data_unavailable")
        assert phrase in result.reason


class TestExcludedAddressFile:
    RAW = yaml.safe_load((REPO_ROOT / "config" / "excluded_addresses.yaml").read_text(encoding="utf-8"))

    def test_every_curated_entry_has_a_label_a_source_and_a_date(self):
        for chain, entries in self.RAW["addresses"].items():
            for e in entries:
                assert e.get("label"), (chain, e)
                assert str(e.get("source", "")).startswith("https://"), (chain, e)
                assert e.get("added"), (chain, e)
                assert e.get("category"), (chain, e)

    @pytest.mark.parametrize(
        ("chain", "address"),
        [
            ("1", "0xffa8db7b38579e6a2d14f9b347a9ace4d044cd54"),  # Bitget 35
            ("8453", "0x3304e22ddaa22bcdc5fca2269b418046ae7b566a"),  # Binance 73
            ("56", "0xe2fc31f816a9b94326492132018c3aecc4a93ae1"),  # Binance: Withdrawals 7
            ("56", "0x91dca37856240e5e1906222ec79278b16420dc92"),  # Indodax 3
            ("200901", "0xc882b111a75c0c657fc507c04fbfcd2cc984f071"),  # Gate Deposit
        ],
    )
    def test_the_audit_additions_load(self, chain, address):
        assert address in EXCLUSIONS["curated"][chain]

    def test_no_address_is_listed_twice_on_one_chain(self):
        for chain, entries in self.RAW["addresses"].items():
            addresses = [str(e["address"]).lower() for e in entries]
            assert len(addresses) == len(set(addresses)), chain
