"""Net issuance from CoinGecko's circulating-supply history (D-072). No network."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.collectors import registry
from src.collectors.supply_history import (
    CHART_SOURCE,
    SNAPSHOT_SOURCE,
    SupplyHistoryCollector,
    annualise,
    chart_to_daily,
    net_issuance,
    plan_backfills,
    smoothed_level,
    trajectory,
)
from src.timeutil import add_days

TODAY = "2026-09-19"
AS_OF = datetime(2026, 9, 19, 3, 30, tzinfo=timezone.utc)


def ms(day: str, hour: int = 0) -> int:
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(hours=hour)
    return int(d.timestamp() * 1000)


def chart(days: dict[str, float], price: float = 2.0) -> dict:
    """A /market_chart payload whose cap/price ratio is the given supply."""
    return {
        "prices": [[ms(d), price] for d in days],
        "market_caps": [[ms(d), price * s] for d, s in days.items()],
    }


def steady(start_supply: float, annual: float, end_day: str, n_days: int) -> dict[str, float]:
    """A supply series growing at a constant annualised rate, ending on end_day."""
    daily = (1.0 + annual) ** (1.0 / 365.0)
    return {add_days(end_day, -k): start_supply * daily ** (n_days - k) for k in range(n_days + 1)}


class TestChartToDaily:
    def test_ratio_of_cap_to_price_is_the_supply(self):
        out = chart_to_daily(chart({"2026-09-10": 1_000.0, "2026-09-11": 1_010.0}), TODAY)
        assert out == pytest.approx({"2026-09-10": 1_000.0, "2026-09-11": 1_010.0})

    def test_the_partial_day_is_dropped(self):
        payload = chart({"2026-09-18": 100.0})
        payload["prices"].append([ms(TODAY, 8), 2.0])
        payload["market_caps"].append([ms(TODAY, 8), 999.0])
        assert TODAY not in chart_to_daily(payload, TODAY)

    def test_first_point_of_a_day_wins(self):
        payload = {
            "prices": [[ms("2026-09-10"), 1.0], [ms("2026-09-10", 9), 1.0]],
            "market_caps": [[ms("2026-09-10"), 500.0], [ms("2026-09-10", 9), 900.0]],
        }
        assert chart_to_daily(payload, TODAY) == {"2026-09-10": 500.0}

    def test_zero_cap_is_no_reading_not_zero_supply(self):
        payload = chart({"2026-09-10": 100.0})
        payload["market_caps"][0][1] = 0
        assert chart_to_daily(payload, TODAY) == {}

    def test_misaligned_timestamps_are_not_paired(self):
        payload = {"prices": [[ms("2026-09-10"), 1.0]], "market_caps": [[ms("2026-09-11"), 5.0]]}
        assert chart_to_daily(payload, TODAY) == {}


class TestNetIssuance:
    def test_a_one_day_revision_does_not_move_the_level(self):
        """CAKE 2026-02-11: +17.8% one day, reverted the next. The median ignores it."""
        series = {add_days(TODAY, -k): 100.0 for k in range(7)}
        series[add_days(TODAY, -3)] = 117.8
        assert smoothed_level(series, TODAY, 7) == 100.0

    def test_too_few_readings_is_no_level(self):
        series = {TODAY: 100.0, add_days(TODAY, -1): 100.0}
        assert smoothed_level(series, TODAY, 7) is None

    def test_constant_growth_is_recovered(self):
        series = steady(1_000_000.0, 0.10, TODAY, 200)
        current, previous = net_issuance(series, TODAY, 90, 7)
        assert current == pytest.approx(0.10, abs=1e-6)
        assert previous == pytest.approx(0.10, abs=1e-6)

    def test_a_burn_is_negative_issuance(self):
        series = steady(1_000_000.0, -0.05, TODAY, 200)
        current, _ = net_issuance(series, TODAY, 90, 7)
        assert current == pytest.approx(-0.05, abs=1e-6)

    def test_short_history_measures_only_what_it_can(self):
        series = steady(1_000.0, 0.10, TODAY, 100)  # one window, not two
        current, previous = net_issuance(series, TODAY, 90, 7)
        assert current is not None
        assert previous is None

    def test_annualise_refuses_a_non_positive_base(self):
        assert annualise(10.0, 0.0, 90) is None
        assert annualise(None, 10.0, 90) is None


class TestTrajectory:
    @pytest.mark.parametrize(
        ("current", "previous", "expected"),
        [(0.05, 0.20, "falling"), (0.30, 0.05, "rising"), (0.10, 0.11, "flat"), (None, 0.1, None)],
    )
    def test_classification(self, current, previous, expected):
        assert trajectory(current, previous, 0.02) == expected


class TestPlanBackfills:
    UNIVERSE = {
        "BIG": {"coingecko_id": "big", "market_cap_usd": 5e9},
        "SMALL": {"coingecko_id": "small", "market_cap_usd": 5e7},
        "SURV": {"coingecko_id": "surv", "market_cap_usd": 1e8},
        "DONE": {"coingecko_id": "done", "market_cap_usd": 1e9},
        "OLD": {"coingecko_id": "old", "market_cap_usd": 1e9},
        "MOVED": {"coingecko_id": "new-id", "market_cap_usd": 1e9},
    }
    STATE = {
        "DONE": {"coingecko_id": "done", "backfilled_utc": "2026-09-15T03:00:00Z"},
        "OLD": {"coingecko_id": "old", "backfilled_utc": "2026-07-01T03:00:00Z"},
        "MOVED": {"coingecko_id": "old-id", "backfilled_utc": "2026-09-15T03:00:00Z"},
    }

    def test_order(self):
        plan = plan_backfills(self.UNIVERSE, self.STATE, {"SURV"}, TODAY, 30)
        # Survivors first; then never-fetched (and re-identified) by market cap;
        # then stale; a fresh backfill is not repeated.
        assert plan == ["SURV", "BIG", "MOVED", "SMALL", "OLD"]
        assert "DONE" not in plan


class _Ctx:
    """get_db() stand-in that hands out the test database without closing it."""

    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, *exc):
        self.db.commit()
        return False


class TestTransformAndWrite:
    def test_backfill_plus_todays_snapshot_yields_a_metric(self, db, monkeypatch):
        backfilled = steady(1_000_000.0, 0.20, add_days(TODAY, -1), 250)
        raw = {
            "today": TODAY,
            "fetched_at": "2026-09-19T03:30:00Z",
            "universe": {
                "AAA": {
                    "coingecko_id": "aaa",
                    "circulating_supply": backfilled[add_days(TODAY, -1)],
                    "snapshot_date": TODAY,
                },
                "NEW": {"coingecko_id": "new", "circulating_supply": 50.0, "snapshot_date": TODAY},
                "STALE": {
                    "coingecko_id": "stale",
                    "circulating_supply": 70.0,
                    "snapshot_date": "2026-09-18",
                },
            },
            "charts": {"AAA": chart(backfilled)},
            "stored": [],
        }
        collector = SupplyHistoryCollector()
        rows = collector.transform(raw, AS_OF)

        metrics = {r["base_asset"]: r for r in rows if r["_kind"] == "metric"}
        assert set(metrics) == {"AAA"}, "one day of history is not a measurement"
        assert metrics["AAA"]["emissions_annual"] == pytest.approx(0.20, abs=0.01)
        assert metrics["AAA"]["emissions_trajectory"] == "flat"

        history = [r for r in rows if r["_kind"] == "history"]
        today_rows = {r["base_asset"]: r for r in history if r["snapshot_date"] == TODAY}
        # Yesterday's snapshot is never stamped with today's date.
        assert set(today_rows) == {"AAA", "NEW"}
        assert today_rows["AAA"]["source"] == SNAPSHOT_SOURCE
        assert all(r["source"] == CHART_SOURCE for r in history if r["snapshot_date"] < TODAY)

        monkeypatch.setattr("src.collectors.supply_history.get_db", lambda: _Ctx(db))
        assert collector.write(rows) > 0
        stored = db.query_one(
            "SELECT emissions_annual, emissions_trajectory FROM supply_metrics "
            "WHERE snapshot_date = ? AND base_asset = 'AAA'",
            (TODAY,),
        )
        assert stored["emissions_trajectory"] == "flat"
        backfill = db.query_one("SELECT days_returned FROM supply_backfill WHERE base_asset='AAA'")
        assert backfill["days_returned"] == len(backfilled)

    def test_stored_history_alone_is_enough(self):
        stored_series = steady(500.0, 0.03, add_days(TODAY, -1), 200)
        raw = {
            "today": TODAY,
            "fetched_at": "2026-09-19T03:30:00Z",
            "universe": {
                "OLDIE": {"coingecko_id": "oldie", "circulating_supply": None, "snapshot_date": TODAY}
            },
            "charts": {},
            "stored": [
                {"snapshot_date": d, "base_asset": "OLDIE", "circulating_supply": v}
                for d, v in stored_series.items()
            ],
        }
        rows = SupplyHistoryCollector().transform(raw, AS_OF)
        metric = next(r for r in rows if r["_kind"] == "metric")
        assert metric["emissions_annual"] == pytest.approx(0.03, abs=0.005)


def test_runs_last_in_the_supply_tier():
    """After asset_contracts, so the two never share CoinGecko's minute."""
    tier = registry.TIERS["supply"]
    assert tier[-1] == "supply_history"
    assert tier.index("asset_contracts") < tier.index("supply_history")
    assert "supply_history" in registry.COLLECTORS
