"""
The klines collector: the only collector that can make the dataset older than
the project, and the only source of the highs and lows the journal's excursions
need.

These tests are all about the transform, because that is where a positional
array and a contract multiplier can quietly corrupt a price series.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.collectors.klines import BinanceKlinesCollector

AS_OF = datetime(2026, 6, 10, 3, 10, tzinfo=timezone.utc)


def bar(day: str, o=1.0, h=2.0, low=0.5, c=1.5, quote_volume=1_000.0) -> list:
    """One Binance kline array, in the exchange's positional order."""
    ts = int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    return [ts, str(o), str(h), str(low), str(c), "100", ts + 86_399_999, str(quote_volume), 50, "0", "0", "0"]


@pytest.fixture
def collector():
    return BinanceKlinesCollector()


class TestDeMultiplication:
    def test_a_1000_prefixed_contract_is_divided_back_to_per_token(self, collector):
        """1000PEPEUSDT quotes a THOUSAND PEPE, and parse_symbol normalises its
        base_asset to PEPE -- so the raw close lands in price_daily under PEPE
        at 1000x, beside a CoinGecko row for the same asset at 1x."""
        rows = collector.transform(
            {"1000PEPEUSDT": [bar("2026-06-01", o=3.6, h=3.8, low=3.4, c=3.5)]}, AS_OF
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["base_asset"] == "PEPE"
        assert row["close_usd"] == pytest.approx(0.0035)
        assert row["high_usd"] == pytest.approx(0.0038)
        assert row["low_usd"] == pytest.approx(0.0034)

    def test_quote_volume_is_not_divided(self, collector):
        """Quote volume is already USD notional and is not per-token."""
        rows = collector.transform(
            {"1000PEPEUSDT": [bar("2026-06-01", quote_volume=5_000_000.0)]}, AS_OF
        )
        assert rows[0]["volume_usd"] == pytest.approx(5_000_000.0)

    def test_an_unmultiplied_contract_is_untouched(self, collector):
        rows = collector.transform({"BTCUSDT": [bar("2026-06-01", c=78_000.0)]}, AS_OF)
        assert rows[0]["close_usd"] == pytest.approx(78_000.0)

    def test_colliding_contracts_are_kept_apart(self, collector):
        """D-046. A plain and a multiplied contract sharing a stripped base are
        different tokens (BOB and 1000000BOB were), so neither may overwrite
        the other in price_daily."""
        rows = collector.transform(
            {
                "1000000BOBUSDT": [bar("2026-06-01", c=0.018, quote_volume=100.0)],
                "BOBUSDT": [bar("2026-06-01", c=0.02, quote_volume=9_000.0)],
            },
            AS_OF,
        )
        by_asset = {r["base_asset"]: r for r in rows}
        assert set(by_asset) == {"BOB", "1000000BOB"}
        assert by_asset["1000000BOB"]["close_usd"] == pytest.approx(0.018)
        assert by_asset["BOB"]["close_usd"] == pytest.approx(0.02)


class TestPartialBars:
    def test_todays_forming_bar_is_skipped(self, collector):
        """Writing it would record a partial high and low, and the journal
        would later read that as the day's true excursion."""
        rows = collector.transform(
            {"BTCUSDT": [bar("2026-06-09"), bar("2026-06-10")]}, AS_OF
        )
        assert [r["snapshot_date"] for r in rows] == ["2026-06-09"]

    def test_a_short_array_is_refused(self, collector):
        """A positional array is exactly where a silent column shift attaches
        one asset's high to another's low."""
        assert collector.transform({"BTCUSDT": [[1, "2", "3"]]}, AS_OF) == []

    def test_a_bar_with_no_close_is_skipped(self, collector):
        broken = bar("2026-06-01")
        broken[4] = None
        assert collector.transform({"BTCUSDT": [broken]}, AS_OF) == []


class TestShape:
    def test_rows_carry_full_ohlc_and_their_source(self, collector):
        """CoinGecko writes close only. The high and low are why this collector
        exists, and `source` is how a mixed table stays auditable."""
        row = collector.transform({"BTCUSDT": [bar("2026-06-01")]}, AS_OF)[0]
        assert row["source"] == "binance_klines"
        assert all(row[k] is not None for k in ("open_usd", "high_usd", "low_usd", "close_usd"))

    def test_an_unparseable_symbol_is_dropped_not_guessed(self, collector):
        assert collector.transform({"": [bar("2026-06-01")]}, AS_OF) == []
