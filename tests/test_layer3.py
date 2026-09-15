"""
Phase 7: Layer 3 structure and positioning risk.

Layer 3 is advisory, so the tests are less about whether it finds a pattern and
more about the guarantees around what it reports: a break is a CLOSE, a setup
always carries an invalidation level, a stale break is not a setup, nothing
here can see past its as_of date, and no reading of funding or OI can ever
raise a score.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from src.db.writes import upsert
from src.screening import layer3_structure as l3

AS_OF = "2026-06-01"


def bars_from(closes, highs=None, lows=None, opens=None, start="2024-01-01", freq="7D"):
    """An OHLC frame with a real DatetimeIndex, defaulting high/low to close."""
    index = pd.date_range(start, periods=len(closes), freq=freq)
    close = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "open": np.asarray(opens, dtype=float) if opens is not None else close,
            "high": np.asarray(highs, dtype=float) if highs is not None else close,
            "low": np.asarray(lows, dtype=float) if lows is not None else close,
            "close": close,
            "volume": np.full(len(close), 1e6),
        },
        index=index,
    )


def descending_series(n: int = 60) -> tuple[list[float], list[float]]:
    """Price under a clean descending resistance line, with three touches.

    Built rather than sampled so the expected answer is known: highs touch the
    line at bars 10, 25 and 40, and price stays below it everywhere else.
    """
    line = [100.0 - 0.5 * i for i in range(n)]
    highs = [value - 8.0 for value in line]
    closes = [value - 12.0 for value in line]
    for touch in (10, 25, 40):
        if touch < n:
            highs[touch] = line[touch]
    return closes, highs


def _seed_prices(db, asset: str, frame: pd.DataFrame) -> None:
    upsert(
        db,
        "price_daily",
        [
            {
                "snapshot_date": ts.strftime("%Y-%m-%d"),
                "base_asset": asset,
                "open_usd": float(row["open"]),
                "high_usd": float(row["high"]),
                "low_usd": float(row["low"]),
                "close_usd": float(row["close"]),
                "volume_usd": float(row["volume"]),
                "source": "test",
                "fetched_at_utc": f"{ts.strftime('%Y-%m-%d')}T00:00:00Z",
            }
            for ts, row in frame.iterrows()
        ],
    )
    db.commit()


class TestSwingHighs:
    def test_a_pivot_needs_bars_on_both_sides(self):
        """The final bars can never be pivots. A high with nothing after it is
        not yet known to be a high, and treating it as one is how a trendline
        detector fits itself to the present."""
        high = pd.Series([1, 2, 5, 2, 1, 2, 9])
        pivots = l3.swing_highs(high, window=2)
        assert pivots == [2]
        assert 6 not in pivots  # the last bar, even though it is the maximum

    def test_a_plateau_is_not_a_pivot(self):
        high = pd.Series([1, 3, 3, 3, 1])
        assert l3.swing_highs(high, window=1) == []


class TestTrendline:
    def test_finds_a_descending_line_with_the_required_touches(self):
        closes, highs = descending_series()
        line = l3.detect_descending_trendline(bars_from(closes, highs=highs))
        assert line is not None
        assert line.slope < 0
        assert line.touches >= 3

    def test_refuses_an_ascending_line(self):
        n = 60
        closes = [50.0 + 0.5 * i for i in range(n)]
        highs = [c + 1 for c in closes]
        for touch in (10, 25, 40):
            highs[touch] = closes[touch] + 5
        assert l3.detect_descending_trendline(bars_from(closes, highs=highs)) is None

    def test_refuses_a_line_price_already_closed_through(self):
        """A line price closed above is not resistance; it is a line drawn
        through old data."""
        closes, highs = descending_series()
        # Push one close between the touches far above the line.
        closes[20] = 200.0
        highs[20] = 200.0
        line = l3.detect_descending_trendline(bars_from(closes, highs=highs))
        if line is not None:
            assert not (line.first_bar <= 20 <= line.last_bar)

    def test_too_little_history_returns_none(self):
        closes, highs = descending_series(n=l3.MIN_BARS - 1)
        assert l3.detect_descending_trendline(bars_from(closes, highs=highs)) is None


class TestBreak:
    def test_a_break_is_a_close_not_a_wick(self):
        """A wick through resistance is what a stop-hunt looks like. Using
        highs here would report a break on nearly every asset."""
        closes, highs = descending_series()
        bars = bars_from(closes, highs=highs)
        line = l3.detect_descending_trendline(bars)
        assert line is not None

        # A single bar wicks well above the line but closes back below it.
        wicked = bars.copy()
        bar = 50
        wicked.iloc[bar, wicked.columns.get_loc("high")] = line.value_at(bar) * 1.5
        assert l3.detect_break(wicked, line) is None

        # The same bar CLOSING above it is a break.
        broken = wicked.copy()
        broken.iloc[bar, broken.columns.get_loc("close")] = line.value_at(bar) * 1.2
        event = l3.detect_break(broken, line)
        assert event is not None
        assert event.bar == bar
        assert event.margin_pct > 0

    def test_a_line_extrapolated_below_zero_reports_no_break(self):
        """Past that point every price is 'above the line' and the concept is
        meaningless."""
        closes, highs = descending_series()
        bars = bars_from(closes, highs=highs)
        line = l3.Trendline(
            slope=-50.0,
            intercept=100.0,
            touches=3,
            first_bar=0,
            last_bar=1,
            first_date="2024-01-01",
            last_date="2024-01-08",
            tolerance_pct=0.02,
        )
        assert l3.detect_break(bars, line) is None


class TestRetest:
    def test_a_close_back_below_the_line_is_a_failed_retest(self):
        closes, highs = descending_series()
        bars = bars_from(closes, highs=highs)
        line = l3.detect_descending_trendline(bars)
        assert line is not None

        broken = bars.copy()
        broken.iloc[50, broken.columns.get_loc("close")] = line.value_at(50) * 1.2
        broken.iloc[50, broken.columns.get_loc("high")] = line.value_at(50) * 1.25
        brk = l3.detect_break(broken, line)
        assert brk is not None

        # Next bar trades into the line and closes below it.
        broken.iloc[51, broken.columns.get_loc("low")] = line.value_at(51) * 0.9
        broken.iloc[51, broken.columns.get_loc("close")] = line.value_at(51) * 0.95
        retest = l3.detect_retest(broken, line, brk)
        assert retest is not None
        assert retest.held is False

    def test_a_close_back_above_the_line_is_a_held_retest(self):
        closes, highs = descending_series()
        bars = bars_from(closes, highs=highs)
        line = l3.detect_descending_trendline(bars)
        assert line is not None

        broken = bars.copy()
        broken.iloc[50, broken.columns.get_loc("close")] = line.value_at(50) * 1.2
        brk = l3.detect_break(broken, line)
        assert brk is not None

        broken.iloc[51, broken.columns.get_loc("low")] = line.value_at(51) * 0.98
        broken.iloc[51, broken.columns.get_loc("close")] = line.value_at(51) * 1.10
        retest = l3.detect_retest(broken, line, brk)
        assert retest is not None
        assert retest.held is True


class TestZones:
    def test_only_unfilled_gaps_are_returned(self):
        """A gap price has traded back through is no longer an imbalance."""
        # A single three-bar imbalance: bar0's high is 10, bar2's low is 12,
        # so nothing traded between 10 and 12. Bars 3-4 sit flat so they add no
        # further gaps.
        frame = bars_from(
            closes=[9, 11, 13, 11, 11],
            highs=[10, 11, 14, 11, 11],
            lows=[8, 10, 12, 11, 11],
        )
        gaps = l3.detect_fvg(frame)
        assert [z.kind for z in gaps] == ["fvg_bullish"]
        assert (gaps[0].low, gaps[0].high) == (10.0, 12.0)

        filled = frame.copy()
        filled.iloc[4, filled.columns.get_loc("low")] = 5.0  # trades back through
        assert l3.detect_fvg(filled) == []

    def test_a_violated_order_block_is_not_a_zone(self):
        frame = bars_from(
            closes=[10, 9, 14, 15, 16],
            opens=[10, 11, 9, 14, 15],
            highs=[11, 11, 15, 16, 17],
            lows=[9, 8, 9, 13, 14],
        )
        assert [z.kind for z in l3.detect_order_block(frame)] == ["order_block_bullish"]

        violated = frame.copy()
        violated.iloc[4, violated.columns.get_loc("close")] = 1.0
        assert l3.detect_order_block(violated) == []


class TestInvalidation:
    def test_every_reported_setup_carries_an_invalidation_level(self, db):
        """The rule, enforced rather than documented: no level, no candidate.
        Position sizing divides by exactly this number."""
        closes, highs = descending_series()
        bars = bars_from(closes, highs=highs, lows=[c - 2 for c in closes])
        # Break, then keep price above the line so the setup is live.
        # Recent enough to be a setup. The old version broke 9 bars back, past
        # the 8-bar window, so it was always stale and the assertions sat inside
        # an `if` that never ran.
        for i in range(55, 60):
            bars.iloc[i, bars.columns.get_loc("close")] = 200.0
            bars.iloc[i, bars.columns.get_loc("high")] = 205.0
            bars.iloc[i, bars.columns.get_loc("low")] = 195.0
        _seed_prices(db, "AAA", bars)

        result = l3.analyse(db, "AAA", "2025-03-01", timeframe="1W")
        assert result.setup_detected is True, result.notes
        assert result.invalidation_price is not None
        assert 0 < result.invalidation_price < 200.0

    def test_a_setup_whose_stop_is_above_price_is_refused(self, db):
        """Reporting it live would hand the reader a negative-risk trade."""
        line = l3.Trendline(
            slope=-1.0,
            intercept=100.0,
            touches=3,
            first_bar=0,
            last_bar=10,
            first_date="2024-01-01",
            last_date="2024-03-11",
            tolerance_pct=0.02,
        )
        bars = bars_from(closes=[50] * 40, lows=[60] * 40, highs=[70] * 40)
        brk = l3.BreakEvent(bar=20, date="2024-06-01", close=90.0, line_value=80.0, margin_pct=0.1)
        invalidation = l3.compute_invalidation(bars, line, brk, None)
        # The break bar's low (60) is above the last close (50): invalid.
        assert invalidation == 60.0
        assert invalidation > float(bars["close"].iloc[-1])

    def test_no_break_means_no_invalidation_and_no_setup(self, db):
        closes, highs = descending_series()
        _seed_prices(db, "BBB", bars_from(closes, highs=highs))
        result = l3.analyse(db, "BBB", "2025-03-01", timeframe="1W")
        assert result.setup_detected is False
        assert result.invalidation_price is None


class TestRecency:
    def test_an_old_break_is_recorded_but_not_reported_as_a_setup(self, db):
        """MINA broke 27 weekly bars before 2026-09-09 and was being reported
        as live with an invalidation level from March."""
        closes, highs = descending_series(n=90)
        bars = bars_from(closes, highs=highs, lows=[c - 2 for c in closes])
        # Break early, then hold well above the line for a long time.
        for i in range(45, 90):
            bars.iloc[i, bars.columns.get_loc("close")] = 200.0
            bars.iloc[i, bars.columns.get_loc("high")] = 205.0
            bars.iloc[i, bars.columns.get_loc("low")] = 195.0
        _seed_prices(db, "STALE", bars)

        result = l3.analyse(db, "STALE", "2025-12-01", timeframe="1W")
        assert result.break_event is not None  # the structure is still recorded
        assert result.setup_detected is False
        assert result.setup_type == "break_stale"
        assert any("beyond the" in note for note in result.notes)


class TestPointInTime:
    def test_load_bars_never_returns_data_past_as_of(self, db):
        """The backtest harness runs Layer 3 historically. A detector that can
        see one bar past its as_of date will find every break perfectly."""
        frame = bars_from(closes=list(range(40)), start="2026-01-01", freq="1D")
        _seed_prices(db, "CCC", frame)
        bars = l3.load_bars(db, "CCC", "2026-01-20")
        assert bars.index.max().strftime("%Y-%m-%d") <= "2026-01-20"
        assert len(bars) == 20

    def test_resample_stamps_a_bar_with_its_opening_date(self, db):
        """The alternative stamps a bar with a date after the data it contains,
        which is look-ahead bias wearing a timestamp."""
        frame = bars_from(closes=list(range(21)), start="2026-01-05", freq="1D")
        weekly = l3.resample(frame, "1W")
        first = weekly.index[0]
        assert first.strftime("%Y-%m-%d") == "2026-01-05"  # a Monday
        # The bar's high must come from its own week, not a later one.
        assert weekly["high"].iloc[0] == frame["high"].iloc[:7].max()

    def test_a_bar_still_forming_on_as_of_is_dropped(self, db):
        """D-060. On a Wednesday the weekly bar that opened on Monday holds two days."""
        frame = bars_from(closes=list(range(10)), start="2026-01-05", freq="1D")  # Mon..Wed
        weekly = l3.resample(frame, "1W", as_of="2026-01-14")
        assert [d.strftime("%Y-%m-%d") for d in weekly.index] == ["2026-01-05"]
        # Without as_of nothing is dropped: the caller decides what "now" is.
        assert len(l3.resample(frame, "1W")) == 2

    def test_3d_bar_boundaries_do_not_move_with_the_window_start(self, db):
        """D-060. Anchored to the first day loaded, every boundary shifted daily."""
        frame = bars_from(closes=list(range(30)), start="2026-01-01", freq="1D")
        full = l3.resample(frame, "3D")
        shifted = l3.resample(frame.iloc[1:], "3D")
        assert set(full.index[1:]) <= set(shifted.index)

    def test_an_unsupported_timeframe_raises(self, db):
        frame = bars_from(closes=list(range(10)))
        with pytest.raises(ValueError, match="unsupported timeframe"):
            l3.resample(frame, "4h")


class TestPositioningRisk:
    def test_funding_percentile_needs_history(self, db):
        assert l3.funding_percentile(db, "AAA", AS_OF) is None

    def test_low_funding_is_flagged_as_fragility_never_as_bullish(self, db):
        """TRB: negative funding was read as bullish crowd positioning hours
        before a 78% collapse. Funding's formula has a structural positive
        bias, so a low reading is a STRONG signal -- of crowding, not
        direction.

        The earlier version stamped rows with timestamps that did not sort in
        insertion order, so the -0.50 reading was never the latest; it only
        asserted a non-None percentile and then grepped this module's source.
        """
        start = pd.Timestamp("2026-05-01T00:00:00Z")
        rows = []
        for i in range(40):
            # A long flat history, then the newest reading far below all of it.
            stamp = (start + pd.Timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
            apr = 0.10 if i < 39 else -0.50
            rows.append(
                {
                    "ts_utc": stamp,
                    "exchange": "binance",
                    "symbol": "AAAUSDT",
                    "base_asset": "AAA",
                    "funding_rate": apr / (365 * 3),
                    "funding_interval_hours": 8.0,
                    "funding_apr": apr,
                    "open_interest_usd": 1e6,
                    "fetched_at_utc": stamp,
                }
            )
        upsert(db, "derivatives_snapshot", rows)
        closes, highs = descending_series()
        _seed_prices(db, "AAA", bars_from(closes, highs=highs))
        db.commit()

        # The newest of 40 readings is the lowest: the bottom 2.5% of its history.
        assert l3.funding_percentile(db, "AAA", "2026-06-01") == pytest.approx(2.5)
        result = l3.analyse(db, "AAA", "2026-06-01", timeframe="1W")
        assert "L3_FUNDING_NEGATIVE_EXTREME" in result.risk_flags
        assert "L3_FUNDING_POSITIVE_EXTREME" not in result.risk_flags

    def test_oi_change_is_measured_in_time_on_binance_rows_only(self, db):
        """D-060. Rows N apart were compared as 'N hours', and Hyperliquid's rows
        sat between Binance's in the same series."""
        start = pd.Timestamp("2026-05-30T00:00:00Z")

        def row(ts, exchange, symbol, oi):
            stamp = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
            return {
                "ts_utc": stamp,
                "exchange": exchange,
                "symbol": symbol,
                "base_asset": "AAA",
                "open_interest_base": oi,
                "open_interest_usd": oi,
                "fetched_at_utc": stamp,
            }

        rows = []
        for i in range(48):
            ts = start + pd.Timedelta(hours=i)
            rows.append(row(ts, "binance", "AAAUSDT", 100.0 if i < 47 else 200.0))
            rows.append(row(ts + pd.Timedelta(minutes=30), "hyperliquid", "AAA", 1.0))
        upsert(db, "derivatives_snapshot", rows)
        db.commit()

        out = l3.oi_change_percentiles(db, "AAA", "2026-06-01")
        # Binance OI doubled in the last hour: the largest 1h and 24h change on file.
        assert out["1h"] == pytest.approx(100.0)
        assert out["24h"] == pytest.approx(100.0)

    def test_oi_percentiles_report_every_window(self, db):
        out = l3.oi_change_percentiles(db, "AAA", AS_OF)
        assert set(out) == {"1h", "4h", "12h", "24h"}
        # No history: every window is None, never zero.
        assert all(v is None for v in out.values())

    def test_missing_depth_is_flagged_not_assumed_fine(self, db):
        closes, highs = descending_series()
        _seed_prices(db, "DDD", bars_from(closes, highs=highs))
        result = l3.analyse(db, "DDD", "2025-03-01", timeframe="1W")
        assert "L3_DEPTH_UNKNOWN" in result.risk_flags

    def test_thin_depth_is_flagged(self, db, monkeypatch):
        closes, highs = descending_series()
        _seed_prices(db, "EEE", bars_from(closes, highs=highs))
        monkeypatch.setattr(l3, "spot_depth_2pct", lambda *a, **k: 1000.0)
        result = l3.analyse(db, "EEE", "2025-03-01", timeframe="1W")
        assert "L3_THIN_DEPTH" in result.risk_flags
        assert any("no exit at size" in n for n in result.notes)

    def test_cvd_is_unavailable_rather_than_proxied(self, db):
        """A close-position-in-range proxy correlates with the move it is meant
        to explain, so it would confirm whatever the chart already showed while
        carrying the authority of a microstructure metric."""
        out = l3.cvd_divergence()
        assert out["available"] is False
        assert "aggressor flag" in out["reason"]


class TestPersistence:
    def test_run_layer3_writes_a_row_per_asset(self, db):
        closes, highs = descending_series()
        for asset in ("AAA", "BBB"):
            _seed_prices(db, asset, bars_from(closes, highs=highs))
        rows = l3.run_layer3(db, "2025-03-01", ["AAA", "BBB"])
        assert len(rows) == 2
        stored = db.query("SELECT base_asset, risk_flags FROM layer3_result")
        assert {r["base_asset"] for r in stored} == {"AAA", "BBB"}
        assert all(isinstance(json.loads(r["risk_flags"]), list) for r in stored)

    def test_an_asset_with_no_history_is_recorded_as_unassessed(self, db):
        rows = l3.run_layer3(db, "2025-03-01", ["NOPE"])
        assert rows[0]["setup_detected"] == 0
        assert rows[0]["invalidation_price"] is None

    def test_no_assets_writes_nothing(self, db):
        assert l3.run_layer3(db, "2025-03-01", []) == []
