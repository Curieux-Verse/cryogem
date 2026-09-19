"""
Pulse market data (D-078): the parse, the look-ahead cut, the cursor-based
incremental write, and the failure rules.

No network: every response comes from an httpx.MockTransport injected through
the collector's own client().
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pandas as pd
import pytest

from src.collectors import registry
from src.db.writes import upsert
from src.pulse.contract import BAR_COLUMNS, OI_COLUMNS, MarketWindow
from src.pulse.data import (
    BinancePulseCollector,
    bars_from_klines,
    funding_from_history,
    hour_floor,
    oi_from_hist,
    select_new_rows,
)

AS_OF = datetime(2026, 9, 19, 14, 0, tzinfo=timezone.utc)
HOUR_MS = 3_600_000


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def kline(open_dt: datetime, o=1.0, h=2.0, low=0.5, c=1.5, qv=1000.0, trades=42, tbq=600.0):
    t = ms(open_dt)
    return [t, str(o), str(h), str(low), str(c), "100", t + HOUR_MS - 1, str(qv), trades, "55", str(tbq), "0"]


def klines_until(end_open: datetime, n: int, **kw) -> list[list]:
    return [kline(end_open - timedelta(hours=n - 1 - i), **kw) for i in range(n)]


def oi_row(ts: datetime, contracts=1000.0, usd=1500.0, symbol="BTCUSDT"):
    return {
        "symbol": symbol,
        "sumOpenInterest": str(contracts),
        "sumOpenInterestValue": str(usd),
        "CMCCirculatingSupply": "1",
        "timestamp": ms(ts),
    }


# -- pure helpers -----------------------------------------------------------------
class TestHourFloor:
    def test_zeroes_minutes_and_keeps_utc(self):
        out = hour_floor(datetime(2026, 9, 19, 14, 37, 12, 999, tzinfo=timezone.utc))
        assert out == AS_OF and out.tzinfo is not None

    def test_naive_is_utc_and_other_zones_convert(self):
        assert hour_floor(datetime(2026, 9, 19, 14, 5)) == AS_OF
        ist = timezone(timedelta(hours=5, minutes=30))
        assert hour_floor(datetime(2026, 9, 19, 19, 45, tzinfo=ist)) == AS_OF


class TestBarsFromKlines:
    def test_column_positions(self):
        """0 open time, 1-4 OHLC, 7 quote volume, 8 trades, 10 taker-buy quote."""
        row = [ms(AS_OF - timedelta(hours=1)), "10", "12", "9", "11", "999", ms(AS_OF) - 1,
               "5000", 77, "888", "3000", "0"]
        bars = bars_from_klines([row], AS_OF)
        assert list(bars.columns) == list(BAR_COLUMNS)
        b = bars.iloc[0]
        assert (b["open"], b["high"], b["low"], b["close"]) == (10.0, 12.0, 9.0, 11.0)
        assert b["quote_volume"] == 5000.0  # not base volume (index 5)
        assert b["taker_buy_quote"] == 3000.0  # not taker-buy base (index 9)
        assert b["trades"] == 77
        assert bars.index[0] == pd.Timestamp(AS_OF - timedelta(hours=1))
        assert str(bars.index.tz) == "UTC"
        assert bars["trades"].dtype == "int64" and bars["close"].dtype == "float64"

    def test_in_progress_and_future_bars_are_dropped(self):
        payload = klines_until(AS_OF + timedelta(hours=1), 5)  # opens 10:00..15:00
        bars = bars_from_klines(payload, AS_OF)
        assert bars.index.max() == pd.Timestamp(AS_OF - timedelta(hours=1))
        assert len(bars) == 3
        assert (bars.index + pd.Timedelta(hours=1) <= pd.Timestamp(AS_OF)).all()

    def test_sorted_unique(self):
        a = kline(AS_OF - timedelta(hours=2), c=1.0)
        b = kline(AS_OF - timedelta(hours=1), c=2.0)
        b2 = kline(AS_OF - timedelta(hours=1), c=3.0)
        bars = bars_from_klines([b, a, b2], AS_OF)
        assert bars.index.is_monotonic_increasing and bars.index.is_unique
        assert bars["close"].tolist() == [1.0, 3.0]

    def test_multiplied_contract_prices_are_per_token_volumes_untouched(self):
        payload = [kline(AS_OF - timedelta(hours=1), o=3.6, h=3.8, low=3.4, c=3.5, qv=5e6, tbq=2e6)]
        b = bars_from_klines(payload, AS_OF, price_multiplier=1000).iloc[0]
        assert b["close"] == pytest.approx(0.0035)
        assert b["high"] == pytest.approx(0.0038)
        assert b["quote_volume"] == 5e6 and b["taker_buy_quote"] == 2e6

    def test_malformed_rows_are_refused(self):
        good = kline(AS_OF - timedelta(hours=1))
        short = good[:8]
        not_hourly = list(good)
        not_hourly[6] = not_hourly[0] + 4 * HOUR_MS - 1
        holed = kline(AS_OF - timedelta(hours=2))
        holed[10] = None
        bars = bars_from_klines([short, not_hourly, holed, good], AS_OF)
        assert len(bars) == 1

    def test_empty_payload_has_the_contract_shape(self):
        bars = bars_from_klines([], AS_OF)
        assert bars.empty and list(bars.columns) == list(BAR_COLUMNS)
        assert isinstance(bars.index, pd.DatetimeIndex) and str(bars.index.tz) == "UTC"


class TestOiFromHist:
    def test_mapping_and_future_rows_dropped(self):
        payload = [
            oi_row(AS_OF - timedelta(hours=1), 900, 1400),
            oi_row(AS_OF, 1000, 1500),  # the OI AT as_of: visible
            oi_row(AS_OF + timedelta(hours=1), 1100, 1600),  # future: dropped
        ]
        oi = oi_from_hist(payload, AS_OF)
        assert list(oi.columns) == list(OI_COLUMNS)
        assert oi.index.max() == pd.Timestamp(AS_OF)
        assert oi["oi_contracts"].tolist() == [900.0, 1000.0]
        assert oi["oi_usd"].tolist() == [1400.0, 1500.0]

    def test_multiplied_contracts_become_tokens(self):
        oi = oi_from_hist([oi_row(AS_OF, 2.0, 7.0)], AS_OF, contract_multiplier=1000)
        assert oi["oi_contracts"].iloc[0] == 2000.0 and oi["oi_usd"].iloc[0] == 7.0

    def test_empty(self):
        oi = oi_from_hist([], AS_OF)
        assert oi.empty and list(oi.columns) == list(OI_COLUMNS)


class TestFunding:
    def test_jitter_floored_and_future_dropped(self):
        payload = [
            {"fundingTime": ms(AS_OF - timedelta(hours=8)), "fundingRate": "0.0001"},
            {"fundingTime": ms(AS_OF) + 2, "fundingRate": "0.0002"},  # 14:00:00.002
            {"fundingTime": ms(AS_OF + timedelta(hours=8)), "fundingRate": "0.0003"},
        ]
        s = funding_from_history(payload, AS_OF)
        assert s.tolist() == [0.0001, 0.0002]
        assert s.index[-1] == pd.Timestamp(AS_OF)


class TestSelectNewRows:
    def _rows(self, n, symbol="BTCUSDT"):
        return [
            {"base_asset": "BTC", "symbol": symbol, "ts_open_utc": f"2026-09-19T{h:02d}:00:00Z"}
            for h in range(n)
        ]

    def test_no_cursor_writes_all(self):
        out, cur = select_new_rows(self._rows(3), "ts_open_utc", "bar_1h", {}, "now")
        assert len(out) == 3 and cur[0]["last_ts_utc"] == "2026-09-19T02:00:00Z"

    def test_cursor_filters_and_never_moves_back(self):
        cursors = {("bar_1h", "BTC"): {"symbol": "BTCUSDT", "last_ts_utc": "2026-09-19T05:00:00Z"}}
        out, cur = select_new_rows(self._rows(3), "ts_open_utc", "bar_1h", cursors, "now")
        assert out == [] and cur == []

    def test_symbol_change_resets(self):
        cursors = {("bar_1h", "BTC"): {"symbol": "OLDUSDT", "last_ts_utc": "2026-09-19T05:00:00Z"}}
        out, cur = select_new_rows(self._rows(3), "ts_open_utc", "bar_1h", cursors, "now")
        assert len(out) == 3 and cur[0]["symbol"] == "BTCUSDT"


# -- the collector, end to end on a local db and a mocked Binance --------------------
def seed(db, survivors=("ONT", "PEPE"), day="2026-09-19", extra=()):
    upsert(
        db,
        "layer1_result",
        [
            {"run_date": day, "base_asset": a, "passed": 1, "fetched_at_utc": "x"}
            for a in survivors
        ]
        + [{"run_date": day, "base_asset": "DEAD", "passed": 0, "fetched_at_utc": "x"}],
    )
    universe = [("BTCUSDT", "BTC", 1), ("ONTUSDT", "ONT", 1), ("1000PEPEUSDT", "PEPE", 1000),
                ("DEADUSDT", "DEAD", 1), *extra]
    upsert(
        db,
        "universe_snapshot",
        [
            {"snapshot_date": day, "exchange": "binance", "symbol": s, "base_asset": b,
             "quote_asset": "USDT", "status": "TRADING", "price_multiplier": m,
             "fetched_at_utc": "x"}
            for s, b, m in universe
        ],
    )
    db.commit()


class FakeBinance:
    """Serves 1H klines/OI/funding up to the request's endTime, like Binance."""

    def __init__(self, now: datetime, bars: int = 30, fail: dict | None = None):
        self.now = now
        self.bars = bars
        self.fail = fail or {}  # (path, symbol) -> status
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        path = request.url.path
        self.calls.append((path, params))
        symbol = params["symbol"]
        status = self.fail.get((path, symbol)) or self.fail.get((path, "*"))
        if status:
            return httpx.Response(status, text="nope")
        end = int(params["endTime"])
        live = ms(self.now)
        if path == "/fapi/v1/klines":
            # Includes the forming bar when endTime reaches it, as Binance does.
            opens = [ms(hour_floor(self.now)) - i * HOUR_MS for i in range(self.bars)]
            rows = [kline(datetime.fromtimestamp(t / 1000, timezone.utc), c=1 + t % 7)
                    for t in sorted(opens) if t <= min(end, live)]
            return httpx.Response(200, json=rows[-int(params["limit"]):])
        if path == "/futures/data/openInterestHist":
            stamps = [ms(hour_floor(self.now)) - i * HOUR_MS for i in range(self.bars)]
            rows = [oi_row(datetime.fromtimestamp(t / 1000, timezone.utc), 1000 + t % 11, 2000)
                    for t in sorted(stamps) if t <= min(end, live)]
            return httpx.Response(200, json=rows)
        if path == "/fapi/v1/fundingRate":
            return httpx.Response(200, json=[
                {"symbol": symbol, "fundingTime": ms(hour_floor(self.now)) - 8 * HOUR_MS + 2,
                 "fundingRate": "0.0001", "markPrice": "1"}
            ])
        return httpx.Response(404)


def make_collector(monkeypatch, fake: FakeBinance) -> BinancePulseCollector:
    collector = BinancePulseCollector()
    original = collector.client

    def client(base_url="", **kwargs):
        return original(base_url, transport=httpx.MockTransport(fake), **kwargs)

    monkeypatch.setattr(collector, "client", client)
    return collector


def count(db, table):
    return db.scalar(f"SELECT COUNT(*) FROM {table}")


class TestCollector:
    async def test_first_run_backfills_and_builds_the_window(self, db, monkeypatch):
        seed(db)
        fake = FakeBinance(AS_OF + timedelta(minutes=3))
        collector = make_collector(monkeypatch, fake)
        result = await collector.run(AS_OF + timedelta(minutes=3))
        assert result.status == "success", result.error_message
        w = collector.window
        assert isinstance(w, MarketWindow)
        assert w.as_of == AS_OF
        assert w.survivors == ["ONT", "PEPE"] and w.benchmark == "BTC"
        assert set(w.bars) == {"BTC", "ONT", "PEPE"}
        assert w.symbols == {"BTC": "BTCUSDT", "ONT": "ONTUSDT", "PEPE": "1000PEPEUSDT"}
        assert w.fetch_errors == {}
        for bars in w.bars.values():
            assert bars.index.max() == pd.Timestamp(AS_OF - timedelta(hours=1))
            assert len(bars) == 29  # 30 served, the forming one never requested/kept
        assert w.oi["BTC"].index.max() == pd.Timestamp(AS_OF)
        assert w.funding["ONT"].index[-1] == pd.Timestamp(AS_OF - timedelta(hours=8))
        # 1000PEPE: prices per token.
        assert w.bars["PEPE"]["close"].max() < 0.01
        # Backfill on first sight: the whole window, and cursors at its end.
        assert count(db, "bar_1h") == 3 * 29
        assert count(db, "oi_1h") == 3 * 30
        cur = db.query("SELECT * FROM series_cursor WHERE base_asset='ONT' ORDER BY series")
        assert [c["last_ts_utc"] for c in cur] == ["2026-09-19T13:00:00Z", "2026-09-19T14:00:00Z"]
        assert result.rows_written == 3 * 29 + 3 * 30
        # endTime honesty on the wire.
        kl = [p for path, p in fake.calls if path == "/fapi/v1/klines"]
        assert {int(p["endTime"]) for p in kl} == {ms(AS_OF) - 1}
        assert {p["limit"] for p in kl} == {"499"}
        oi = [p for path, p in fake.calls if path == "/futures/data/openInterestHist"]
        assert {int(p["endTime"]) for p in oi} == {ms(AS_OF)} and {p["period"] for p in oi} == {"1h"}

    async def test_second_run_writes_only_the_new_hour(self, db, monkeypatch):
        seed(db)
        await make_collector(monkeypatch, FakeBinance(AS_OF)).run(AS_OF)
        before = (count(db, "bar_1h"), count(db, "oi_1h"))
        nxt = AS_OF + timedelta(hours=1)
        collector = make_collector(monkeypatch, FakeBinance(nxt))
        result = await collector.run(nxt + timedelta(minutes=3))
        assert result.status == "success"
        assert result.rows_written == 3 + 3  # one bar + one OI row per asset
        assert (count(db, "bar_1h"), count(db, "oi_1h")) == (before[0] + 3, before[1] + 3)
        # The window is still the full fetched window, not the written slice.
        assert len(collector.window.bars["BTC"]) == 29

    async def test_replaying_a_past_hour_writes_nothing(self, db, monkeypatch):
        seed(db)
        await make_collector(monkeypatch, FakeBinance(AS_OF)).run(AS_OF)
        past = AS_OF - timedelta(hours=5)
        collector = make_collector(monkeypatch, FakeBinance(AS_OF))
        result = await collector.run(past)
        assert result.rows_written == 0
        assert collector.window.bars["BTC"].index.max() == pd.Timestamp(past - timedelta(hours=1))

    async def test_symbol_change_rewrites_the_window(self, db, monkeypatch):
        seed(db)
        await make_collector(monkeypatch, FakeBinance(AS_OF)).run(AS_OF)
        db.execute("UPDATE series_cursor SET symbol='ONTOLDUSDT' WHERE base_asset='ONT'")
        db.commit()
        result = await make_collector(monkeypatch, FakeBinance(AS_OF)).run(AS_OF)
        assert result.rows_written == 29 + 30  # only ONT, whole window
        assert db.scalar("SELECT symbol FROM series_cursor WHERE base_asset='ONT' LIMIT 1") == "ONTUSDT"

    async def test_one_failed_asset_is_partial_with_fetch_errors(self, db, monkeypatch):
        extra = [(f"A{i}USDT", f"A{i}", 1) for i in range(5)]
        seed(db, survivors=("ONT", "PEPE", *[f"A{i}" for i in range(5)]), extra=extra)
        fake = FakeBinance(AS_OF, fail={("/fapi/v1/klines", "ONTUSDT"): 400})
        collector = make_collector(monkeypatch, fake)
        result = await collector.run(AS_OF)
        assert result.status == "partial"
        w = collector.window
        assert "ONT" in w.fetch_errors and w.fetch_errors["ONT"].startswith("klines:")
        assert "ONT" not in w.bars and "ONT" not in w.oi
        assert "ONT" in w.survivors  # never silently dropped
        assert db.scalar("SELECT COUNT(*) FROM bar_1h WHERE base_asset='ONT'") == 0

    async def test_oi_failure_keeps_the_bars(self, db, monkeypatch):
        seed(db)
        fake = FakeBinance(AS_OF, fail={("/futures/data/openInterestHist", "ONTUSDT"): 400})
        collector = make_collector(monkeypatch, fake)
        result = await collector.run(AS_OF)
        assert result.status == "partial"
        assert "ONT" in collector.window.bars and "ONT" not in collector.window.oi
        assert "ONT" not in collector.window.fetch_errors

    async def test_too_many_failures_fail_the_run(self, db, monkeypatch):
        seed(db)
        fake = FakeBinance(AS_OF, fail={("/fapi/v1/klines", "*"): 400})
        collector = make_collector(monkeypatch, fake)
        result = await collector.run(AS_OF)
        assert result.status == "failed"
        assert "over the" in result.error_message
        assert collector.window is None
        assert count(db, "bar_1h") == 0

    async def test_a_418_stops_everything(self, db, monkeypatch):
        extra = [(f"A{i}USDT", f"A{i}", 1) for i in range(30)]
        seed(db, survivors=[f"A{i}" for i in range(30)], extra=extra)
        fake = FakeBinance(AS_OF, fail={("/fapi/v1/klines", "A3USDT"): 418})
        collector = make_collector(monkeypatch, fake)
        result = await collector.run(AS_OF)
        assert result.status == "failed"
        assert "IPBannedError" in result.error_message
        assert collector.window is None
        # Concurrency bounds what was already in flight; nothing near all 93.
        assert len(fake.calls) < 3 * 31
        assert count(db, "bar_1h") == 0

    async def test_unmapped_survivor_is_reported(self, db, monkeypatch):
        seed(db, survivors=("ONT", "HYPEONLY"))
        collector = make_collector(monkeypatch, FakeBinance(AS_OF))
        result = await collector.run(AS_OF)
        assert result.status == "partial"
        assert "no TRADING Binance" in collector.window.fetch_errors["HYPEONLY"]
        assert "DEAD" not in collector.window.bars  # failed L1: not fetched

    async def test_no_universe_fails(self, db, monkeypatch):
        collector = make_collector(monkeypatch, FakeBinance(AS_OF))
        result = await collector.run(AS_OF)
        assert result.status == "failed" and collector.window is None


def test_registered_but_in_no_tier():
    assert registry.COLLECTORS["binance_pulse"] is BinancePulseCollector
    assert all("binance_pulse" not in names for names in registry.TIERS.values())
    c = BinancePulseCollector()
    assert c.name == "binance_pulse" and c.rate_limit_key == "binance_futures"
