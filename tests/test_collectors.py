"""
Phase 1 collector tests.

Every test drives transform() from a saved fixture. NO NETWORK: a suite that
calls Binance fails when Binance is slow, and it is not testing our code anyway.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.collectors.binance import (
    BinanceDerivativesCollector,
    BinanceSpotCollector,
    BinanceUniverseCollector,
    derive_funding_interval_hours,
    is_screenable_perp,
)
from src.collectors.coinalyze import CoinalyzeLiquidationCollector, previous_day_window
from src.collectors.coingecko import CoinGeckoCollector, coingecko_endpoint
from src.collectors.registry import refuse_as_of, resolve
from src.collectors.defillama import DefiLlamaCollector
from src.collectors.hyperliquid import HyperliquidCollector
from src.config import get_config
from src.symbols import funding_apr, parse_symbol, parse_universe
from tests.conftest import load_fixture

AS_OF = datetime(2026, 9, 9, 3, 12, 0, tzinfo=timezone.utc)


class TestSymbolNormalisation:
    """The 1000-prefix trap: missing it silently zeroes market cap."""

    @pytest.mark.parametrize(
        ("symbol", "base", "multiplier"),
        [
            ("1000PEPEUSDT", "PEPE", 1000),
            ("1000SHIBUSDT", "SHIB", 1000),
            ("1000000MOGUSDT", "MOG", 1000000),
            ("1MBABYDOGEUSDT", "BABYDOGE", 1000000),
            ("BTCUSDT", "BTC", 1),
            ("ETHUSDT", "ETH", 1),
        ],
    )
    def test_multiplier_prefix_stripped(self, symbol, base, multiplier):
        parsed = parse_symbol(symbol, "USDT")
        assert parsed.base_asset == base
        assert parsed.price_multiplier == multiplier

    def test_empty_base_rejected(self):
        with pytest.raises(ValueError):
            parse_symbol("USDT", "USDT")

    def test_garbage_rejected(self):
        with pytest.raises(ValueError):
            parse_symbol("not a symbol!")


class TestBaseAssetCollisions:
    """D-046. BOBUSDT and 1000000BOBUSDT are different tokens that both reduced
    to BOB, mixing two price series and two market caps under one key."""

    def test_multiplied_contract_keeps_its_prefix_when_the_plain_name_is_taken(self):
        resolved = parse_universe(["BOBUSDT", "1000000BOBUSDT"], "USDT")
        assert resolved["BOBUSDT"].base_asset == "BOB"
        assert resolved["1000000BOBUSDT"].base_asset == "1000000BOB"
        assert resolved["1000000BOBUSDT"].price_multiplier == 1

    def test_no_collision_leaves_the_prefix_stripped(self):
        resolved = parse_universe(["1000PEPEUSDT", "BTCUSDT"], "USDT")
        assert resolved["1000PEPEUSDT"].base_asset == "PEPE"
        assert resolved["1000PEPEUSDT"].price_multiplier == 1000

    def test_two_multiplied_contracts_both_keep_their_prefix(self):
        resolved = parse_universe(["1000XUSDT", "1000000XUSDT"], "USDT")
        assert {p.base_asset for p in resolved.values()} == {"1000X", "1000000X"}

    def test_unparseable_symbols_are_left_out(self):
        assert parse_universe(["USDT", "BTCUSDT"], "USDT").keys() == {"BTCUSDT"}

    def test_index_perps_are_not_screenable(self):
        dom = {"contractType": "PERPETUAL", "quoteAsset": "USDT", "underlyingType": "INDEX"}
        coin = {"contractType": "PERPETUAL", "quoteAsset": "USDT", "underlyingType": "COIN"}
        assert not is_screenable_perp(dom, "USDT")
        assert is_screenable_perp(coin, "USDT")


class TestPastAsOf:
    """D-048. Every collector but klines fetches live data, so a past --as-of
    stamped today's values onto that day and overwrote what was recorded."""

    def test_a_live_only_collector_refuses_a_past_date(self):
        refused = refuse_as_of(resolve("daily"), "2026-09-01", "2026-09-15")
        assert any(r.startswith("binance_universe:") for r in refused)
        assert not any(r.startswith("binance_klines:") for r in refused)

    def test_klines_accepts_a_past_date(self):
        assert refuse_as_of(resolve("binance_klines"), "2026-09-01", "2026-09-15") == []

    def test_today_is_always_accepted(self):
        assert refuse_as_of(resolve("daily"), "2026-09-15", "2026-09-15") == []

    def test_a_future_date_is_refused_for_everything(self):
        assert refuse_as_of(resolve("binance_klines"), "2026-09-16", "2026-09-15")


class TestCoinalyzeWindow:
    """D-049. The window was sent in milliseconds and ended at run time, so it
    asked for today's partial bar; an empty history then summed to 0.0."""

    def test_window_is_the_previous_complete_utc_day_in_seconds(self):
        start, end = previous_day_window(AS_OF)  # 2026-09-09 03:12 UTC
        assert start == int(datetime(2026, 9, 8, tzinfo=timezone.utc).timestamp())
        assert end == int(datetime(2026, 9, 9, tzinfo=timezone.utc).timestamp()) - 1

    def test_only_the_window_bar_is_counted(self):
        start, _ = previous_day_window(AS_OF)
        raw = {
            "BTCUSDT_PERP.A": {
                "symbol": "BTCUSDT_PERP.A",
                "history": [
                    {"t": start, "l": 1_000.0, "s": 500.0},
                    {"t": start + 86_400, "l": 9_999.0, "s": 9_999.0},  # today, partial
                ],
            }
        }
        rows = CoinalyzeLiquidationCollector().transform(raw, AS_OF)
        assert len(rows) == 1
        assert rows[0]["liq_total_usd_24h"] == pytest.approx(1_500.0)

    def test_an_empty_history_writes_no_reading_not_a_zero(self):
        raw = {"BTCUSDT_PERP.A": {"symbol": "BTCUSDT_PERP.A", "history": []}}
        assert CoinalyzeLiquidationCollector().transform(raw, AS_OF) == []


class TestCoinGeckoPlan:
    """D-054. Demo and Pro keys look alike, but each works only on its own host."""

    @staticmethod
    def _settings(plan: str):
        settings = get_config().settings
        universe = settings.universe.model_copy(update={"coingecko_plan": plan})
        return settings.model_copy(update={"universe": universe})

    def test_a_demo_key_goes_to_the_public_host_with_the_demo_header(self):
        base, headers = coingecko_endpoint(self._settings("demo"), "CG-example")
        assert base == "https://api.coingecko.com/api/v3"
        assert headers == {"x-cg-demo-api-key": "CG-example"}

    def test_a_pro_key_goes_to_the_pro_host_with_the_pro_header(self):
        base, headers = coingecko_endpoint(self._settings("pro"), "CG-example")
        assert base == "https://pro-api.coingecko.com/api/v3"
        assert headers == {"x-cg-pro-api-key": "CG-example"}

    def test_no_key_is_keyless_on_the_public_host_whatever_the_plan(self):
        assert coingecko_endpoint(self._settings("pro"), None) == (
            "https://api.coingecko.com/api/v3",
            {},
        )

    def test_the_shipped_setting_is_the_free_demo_plan(self):
        assert get_config().settings.universe.coingecko_plan == "demo"


class TestFundingNormalisation:
    """Funding interval is per-symbol. Assuming 8h mis-ranks the universe."""

    def test_four_hour_symbol_annualises_to_double_an_eight_hour_symbol(self):
        assert funding_apr(0.0001, 4) == pytest.approx(funding_apr(0.0001, 8) * 2)

    def test_naive_formula_only_matches_eight_hour_symbols(self):
        naive = 0.0001 * 3 * 365
        assert funding_apr(0.0001, 8) == pytest.approx(naive)
        assert funding_apr(0.0001, 4) != pytest.approx(naive)

    def test_zero_interval_refuses_to_guess(self):
        with pytest.raises(ValueError, match="positive"):
            funding_apr(0.0001, 0)

    def test_interval_derived_from_funding_times(self):
        history = load_fixture("binance_funding_4h.json")
        assert derive_funding_interval_hours(history) == 4.0

    def test_interval_none_when_history_too_short(self):
        # None, never a fallback of 8.0 -- inventing 8h for a 4h symbol is the
        # precise error the derivation exists to prevent.
        assert derive_funding_interval_hours([{"fundingTime": 1}]) is None

    def test_interval_none_when_history_is_ambiguous(self):
        mixed = [{"fundingTime": t} for t in (0, 4 * 3600000, 12 * 3600000, 16 * 3600000)]
        assert derive_funding_interval_hours(mixed) in (None, 4.0)


class TestBinanceUniverse:
    def test_only_usdt_perpetuals_are_kept(self):
        collector = BinanceUniverseCollector()
        info = load_fixture("binance_exchangeinfo.json")
        symbols = [
            s
            for s in info["symbols"]
            if s["contractType"] == "PERPETUAL" and s["quoteAsset"] == "USDT"
        ]
        rows = collector.transform({"symbols": symbols, "intervals": {}}, AS_OF)
        names = {r["symbol"] for r in rows}
        assert "ETHUSDC" not in names, "wrong quote asset must be excluded"
        assert "OLDCOINUSDT" not in names, "non-perpetual contract must be excluded"
        assert names == {"BTCUSDT", "1000PEPEUSDT", "ORPHANUSDT", "NEWCOINUSDT"}

    def test_base_asset_normalised_for_cross_source_lookup(self):
        collector = BinanceUniverseCollector()
        info = load_fixture("binance_exchangeinfo.json")
        rows = collector.transform(
            {"symbols": info["symbols"][:2], "intervals": {}}, AS_OF
        )
        pepe = next(r for r in rows if r["symbol"] == "1000PEPEUSDT")
        assert pepe["base_asset"] == "PEPE"
        assert pepe["price_multiplier"] == 1000

    def test_colliding_contracts_get_distinct_base_assets(self):
        collector = BinanceUniverseCollector()
        symbols = [
            {"symbol": s, "quoteAsset": "USDT", "contractType": "PERPETUAL", "status": "TRADING"}
            for s in ("BOBUSDT", "1000000BOBUSDT")
        ]
        rows = collector.transform({"symbols": symbols, "intervals": {}}, AS_OF)
        assert sorted(r["base_asset"] for r in rows) == ["1000000BOB", "BOB"]

    def test_onboard_date_is_iso_not_epoch_millis(self):
        collector = BinanceUniverseCollector()
        info = load_fixture("binance_exchangeinfo.json")
        rows = collector.transform({"symbols": info["symbols"][:1], "intervals": {}}, AS_OF)
        assert rows[0]["onboard_date"].startswith("2019-")

    def test_snapshot_date_comes_from_as_of_but_fetched_at_is_real_time(self):
        collector = BinanceUniverseCollector()
        info = load_fixture("binance_exchangeinfo.json")
        rows = collector.transform({"symbols": info["symbols"][:1], "intervals": {}}, AS_OF)
        assert rows[0]["snapshot_date"] == "2026-09-09"
        # fetched_at_utc must be the ACTUAL moment, not the nominal slot.
        assert rows[0]["fetched_at_utc"] != "2026-09-09T03:12:00Z"


class TestBinanceDerivatives:
    def _raw(self, intervals=None):
        return {
            "premium": load_fixture("binance_premium_index.json"),
            "tickers": load_fixture("binance_ticker24.json"),
            "open_interest": {
                "BTCUSDT": 85000.0,
                "1000PEPEUSDT": 40000000.0,
                "ORPHANUSDT": 3000000.0,
                "NEWCOINUSDT": 900000.0,
            },
            "intervals": intervals
            if intervals is not None
            else {
                "BTCUSDT": 8.0,
                "1000PEPEUSDT": 8.0,
                "ORPHANUSDT": 4.0,
                "NEWCOINUSDT": 1.0,
            },
            "symbols": {"BTCUSDT", "1000PEPEUSDT", "ORPHANUSDT", "NEWCOINUSDT"},
        }

    def test_open_interest_stored_in_contracts_as_primary_field(self):
        rows = BinanceDerivativesCollector().transform(self._raw(), AS_OF)
        btc = next(r for r in rows if r["symbol"] == "BTCUSDT")
        assert btc["open_interest_base"] == 85000.0
        # USD OI is contracts x price -- derived, and contaminated by price.
        assert btc["open_interest_usd"] == pytest.approx(85000.0 * 62000.0)

    def test_funding_apr_uses_each_symbols_own_interval(self):
        rows = BinanceDerivativesCollector().transform(self._raw(), AS_OF)
        by_symbol = {r["symbol"]: r for r in rows}
        # Same raw rate direction, different intervals -> different APR scaling.
        assert by_symbol["ORPHANUSDT"]["funding_interval_hours"] == 4.0
        assert by_symbol["ORPHANUSDT"]["funding_apr"] == pytest.approx(0.0025 * (8760 / 4))
        assert by_symbol["NEWCOINUSDT"]["funding_apr"] == pytest.approx(0.0030 * 8760)

    def test_unknown_interval_yields_null_apr_never_an_assumed_eight_hours(self):
        rows = BinanceDerivativesCollector().transform(self._raw(intervals={}), AS_OF)
        assert all(r["funding_apr"] is None for r in rows)
        assert all(r["funding_rate"] is not None for r in rows), "raw rate still recorded"

    def test_negative_funding_is_preserved_not_absolute(self):
        rows = BinanceDerivativesCollector().transform(self._raw(), AS_OF)
        pepe = next(r for r in rows if r["symbol"] == "1000PEPEUSDT")
        assert pepe["funding_rate"] < 0
        assert pepe["funding_apr"] < 0

    def test_premium_computed_from_mark_and_index(self):
        rows = BinanceDerivativesCollector().transform(self._raw(), AS_OF)
        btc = next(r for r in rows if r["symbol"] == "BTCUSDT")
        assert btc["premium"] == pytest.approx((62000.0 - 61990.0) / 61990.0)

    def test_symbols_outside_the_universe_are_ignored(self):
        raw = self._raw()
        raw["symbols"] = {"BTCUSDT"}
        rows = BinanceDerivativesCollector().transform(raw, AS_OF)
        assert {r["symbol"] for r in rows} == {"BTCUSDT"}


class TestBinanceSpot:
    def test_orphan_perp_has_no_spot_row(self):
        """ORPHANUSDT trades as a perp but has no Binance spot pair.

        Absence here is the signal. The screener turns it into a ratio of
        infinity, never None -- None would silently pass a '> 40' filter.
        """
        rows = BinanceSpotCollector().transform(
            load_fixture("binance_spot_ticker24.json"), AS_OF
        )
        assets = {r["base_asset"] for r in rows}
        assert "ORPHAN" not in assets
        assert {"BTC", "PEPE", "NEWCOIN"} <= assets

    def test_non_usdt_pairs_excluded(self):
        rows = BinanceSpotCollector().transform(
            load_fixture("binance_spot_ticker24.json"), AS_OF
        )
        assert all(r["symbol"].endswith("USDT") for r in rows)


class TestHyperliquid:
    def test_hourly_funding_annualised_by_8760(self):
        rows = HyperliquidCollector().transform(load_fixture("hyperliquid_meta.json"), AS_OF)
        btc = next(r for r in rows if r["symbol"] == "BTC")
        assert btc["funding_interval_hours"] == 1.0
        assert btc["funding_apr"] == pytest.approx(0.0000125 * 8760)

    def test_universe_and_context_arrays_paired_positionally(self):
        rows = HyperliquidCollector().transform(load_fixture("hyperliquid_meta.json"), AS_OF)
        by_symbol = {r["symbol"]: r for r in rows}
        assert by_symbol["ETH"]["mark_price"] == 2450.0
        assert by_symbol["HYPE"]["funding_rate"] < 0

    def test_mismatched_array_lengths_warn_and_truncate(self):
        collector = HyperliquidCollector()
        meta, contexts = load_fixture("hyperliquid_meta.json")
        rows = collector.transform([meta, contexts[:1]], AS_OF)
        assert len(rows) == 1
        assert any("mismatch" in w for w in collector._warnings)


class TestCoinGeckoTickerCollision:
    """The collision rule decides whether a micro-cap inherits a large-cap's mcap."""

    def test_largest_market_cap_wins_for_a_colliding_ticker(self):
        rows = CoinGeckoCollector().transform(load_fixture("coingecko_markets.json"), AS_OF)
        orphan = [r for r in rows if r["base_asset"] == "ORPHAN"]
        assert len(orphan) == 1, "exactly one row per ticker"
        # The fixture has a $2B and a $20M asset sharing the ticker ORPHAN, with
        # the $2B one listed first (market_cap_desc). The larger must win.
        assert orphan[0]["market_cap_usd"] == 2000000000
        assert orphan[0]["coingecko_id"] == "big-collider"

    def test_assets_without_market_cap_are_skipped_not_zeroed(self):
        rows = CoinGeckoCollector().transform(load_fixture("coingecko_markets.json"), AS_OF)
        assert "NOMCAP" not in {r["base_asset"] for r in rows}
        # It is skipped so L1_NO_MCAP can fail it for unresolvable data. A
        # market_cap of 0.0 would instead trip the "< $30M" check for the
        # wrong reason and report a misleading number.

    def test_pct_below_ath_is_negative_for_a_drawn_down_asset(self):
        rows = CoinGeckoCollector().transform(load_fixture("coingecko_markets.json"), AS_OF)
        pepe = next(r for r in rows if r["base_asset"] == "PEPE")
        assert pepe["pct_below_ath"] < 0
        assert pepe["pct_below_ath"] == pytest.approx((0.0000085 - 0.00001718) / 0.00001718)

    def test_ath_date_truncated_to_a_day(self):
        rows = CoinGeckoCollector().transform(load_fixture("coingecko_markets.json"), AS_OF)
        btc = next(r for r in rows if r["base_asset"] == "BTC")
        assert btc["ath_date"] == "2024-03-14"

    def test_symbols_uppercased_for_joining(self):
        rows = CoinGeckoCollector().transform(load_fixture("coingecko_markets.json"), AS_OF)
        assert all(r["base_asset"] == r["base_asset"].upper() for r in rows)


class TestDefiLlamaMapping:
    def _raw(self):
        return {
            "protocols": load_fixture("defillama_protocols.json"),
            "fees": load_fixture("defillama_fees.json"),
            "revenue": load_fixture("defillama_revenue.json"),
        }

    def test_mapped_asset_gets_fundamentals(self, db, monkeypatch):
        from src.db.writes import upsert

        upsert(
            db,
            "universe_snapshot",
            [
                {
                    "snapshot_date": "2026-09-09",
                    "exchange": "binance",
                    "symbol": "AEROUSDT",
                    "base_asset": "AERO",
                    "quote_asset": "USDT",
                    "fetched_at_utc": "2026-09-09T03:12:00Z",
                }
            ],
        )
        db.commit()
        collector = DefiLlamaCollector()
        monkeypatch.setattr(collector, "_universe_assets", lambda: ["AERO", "TAO"])
        rows = {r["base_asset"]: r for r in collector.transform(self._raw(), AS_OF)}

        assert rows["AERO"]["has_fundamentals"] == 1
        assert rows["AERO"]["revenue_30d_usd"] == 24000000
        assert rows["AERO"]["revenue_annualised"] == pytest.approx(24000000 * 365 / 30)

    def test_unmapped_asset_marked_without_fundamentals_not_zeroed(self, monkeypatch):
        collector = DefiLlamaCollector()
        monkeypatch.setattr(collector, "_universe_assets", lambda: ["TAO"])
        row = collector.transform(self._raw(), AS_OF)[0]
        assert row["has_fundamentals"] == 0
        # Nulls, not zeros: "no revenue model" must score the block None and
        # redistribute its weight, not score it a genuine-looking zero.
        assert row["revenue_30d_usd"] is None
        assert row["tvl_usd"] is None
