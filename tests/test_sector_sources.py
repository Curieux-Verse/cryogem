"""Sector mappings come from CoinGecko categories, by a fixed rule (D-073)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from src.config import get_config

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "propose_sectors.py"
_spec = importlib.util.spec_from_file_location("propose_sectors", _SCRIPT)
propose_sectors = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(propose_sectors)


def members(**coins: list[str]) -> dict[str, list[str]]:
    """{coin id: [categories]} turned into CoinGecko's {category: [coin ids]}."""
    out: dict[str, list[str]] = {}
    for coin, categories in coins.items():
        for category in categories:
            out.setdefault(category, []).append(coin)
    return out


class TestPrecedence:
    def _one(self, categories: list[str], asset: str = "X") -> str | None:
        proposals, _ = propose_sectors.propose({asset: "coin"}, members(coin=categories), set())
        return proposals.get(asset)

    def test_a_memecoin_on_its_own_chain_is_a_memecoin(self):
        assert self._one(["layer-1", "meme-token", "smart-contract-platform"]) == "memecoin"

    def test_an_oracle_is_infrastructure_not_defi_or_rwa(self):
        assert self._one(["decentralized-finance-defi", "oracle"]) == "infrastructure"
        assert self._one(["real-world-assets-rwa", "oracle"]) == "infrastructure"

    def test_dex_tokens_are_not_the_exchange_sector(self):
        """CoinGecko's exchange-based-tokens includes DEX tokens (BNT, ZRX)."""
        assert self._one(["exchange-based-tokens", "decentralized-exchange"]) == "defi"
        assert self._one(["centralized-exchange-token-cex"]) == "exchange"

    def test_an_explicit_layer1_tag_beats_generic_infrastructure(self):
        assert self._one(["infrastructure", "layer-1"]) == "l1"
        assert self._one(["infrastructure", "smart-contract-platform"]) == "infrastructure"

    def test_a_category_outside_the_taxonomy_leaves_it_unclassified(self):
        proposals, unclassified = propose_sectors.propose(
            {"SANTOS": "santos"}, members(santos=["fan-token", "sports"]), set()
        )
        assert proposals == {}
        assert unclassified == ["SANTOS"]

    def test_already_mapped_assets_are_left_alone(self):
        proposals, _ = propose_sectors.propose(
            {"BTC": "bitcoin"}, members(bitcoin=["layer-1"]), {"BTC"}
        )
        assert proposals == {}


class TestOverrides:
    def test_an_override_needs_a_category_coingecko_lists(self):
        """RVN -> l1 only because CoinGecko lists it as a smart-contract platform."""
        ok, _ = propose_sectors.propose(
            {"RVN": "rvn"}, members(rvn=["real-world-assets-rwa", "smart-contract-platform"]), set()
        )
        assert ok == {"RVN": "l1"}
        refused, _ = propose_sectors.propose(
            {"RVN": "rvn"}, members(rvn=["real-world-assets-rwa"]), set()
        )
        assert refused == {"RVN": "rwa"}, "an override must not invent a label"


class TestTheMappedFile:
    @pytest.mark.parametrize("ticker", ["ONT", "KAVA", "MINA", "THETA", "HOT", "CHZ", "LPT", "XEC"])
    def test_the_top_ranked_unclassified_coins_have_a_sector(self, ticker):
        assert get_config().sectors.sector_of(ticker) != "unclassified"

    def test_mask_stays_unclassified_because_its_source_has_no_sector(self):
        """CoinGecko lists only ecosystems and portfolios for MASK (2026-09-19)."""
        assert get_config().sectors.sector_of("MASK") == "unclassified"

    def test_numeric_looking_tickers_load_as_strings(self):
        sectors = get_config().sectors
        assert sectors.sector_of("0G") == "ai"
        assert sectors.sector_of("4") == "memecoin"
