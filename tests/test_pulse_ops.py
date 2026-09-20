"""Pulse operations: run, journal (D-081), alerts (D-080), pulse.json bake.

The data/features/score stages belong to other modules; here they are stubbed
through sys.modules so these tests pin the ORCHESTRATION: what is written,
when, and what is refused.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from src.db.writes import json_dump, upsert
from src.pulse import alerts, journal
from src.pulse import publish as pulse_publish
from src.pulse.contract import FEATURE_COLUMNS, PULSE_COLUMNS, PULSE_JSON_EXAMPLE, MarketWindow
from src.timeutil import format_instant

T0 = datetime(2026, 9, 19, 14, 0, tzinfo=timezone.utc)
TS0 = format_instant(T0)
TS_PREV = format_instant(T0 - timedelta(hours=1))


def _row(ts, asset, score, rank, aligned=False, state_4h="neutral", gem_rank=None, flags=None,
         version="pulse-v1"):
    return {
        "ts_utc": ts,
        "base_asset": asset,
        "score": score,
        "rank": rank,
        "universe_size": 20,
        "state_4h": state_4h,
        "state_1h": "neutral",
        "oi_quadrant": "neutral",
        "flags": json_dump(flags or []),
        "components": json_dump({"flow": 50.0}),
        "features": json_dump({"flow_24h": 0.1, "coverage": 1.0, "bars_since_4h": 2,
                                "spark_1h": [1.0, 1.1], "flow_1h": [0.1, 0.2],
                                "spark_4h": [1.0]}),
        "aligned": 1 if aligned else 0,
        "gem_rank": gem_rank,
        "score_version": version,
        "fetched_at_utc": ts,
    }


def _hour(db, ts, ranking, aligned=(), states=None, gem=None, excluded=()):
    """ranking: assets best first; scores descend from 90."""
    rows = [
        _row(ts, a, 90.0 - i, i + 1, aligned=a in aligned,
             state_4h=(states or {}).get(a, "neutral"), gem_rank=(gem or {}).get(a))
        for i, a in enumerate(ranking)
    ]
    rows += [_row(ts, a, None, None, flags=["thin_book"]) for a in excluded]
    upsert(db, "pulse_result", rows)
    db.commit()
    return rows


ASSETS = [f"A{i:02d}" for i in range(20)]


@pytest.fixture(autouse=True)
def _no_real_telegram(monkeypatch):
    """No test may ever reach api.telegram.org, whatever a developer's .env holds."""
    from src.report import telegram

    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to send a real Telegram message")

    monkeypatch.setattr(telegram.httpx, "post", refuse)


def _bar(db, asset, ts_open, o, h, low, c):
    upsert(db, "bar_1h", [{
        "base_asset": asset, "ts_open_utc": ts_open, "symbol": f"{asset}USDT",
        "open": o, "high": h, "low": low, "close": c, "quote_volume": 1e6,
        "taker_buy_quote": 5e5, "trades": 10, "fetched_at_utc": ts_open,
    }])


def _bars(db, asset, start: datetime, n, price=100.0, step=1.0):
    for i in range(n):
        p = price + step * i
        _bar(db, asset, format_instant(start + timedelta(hours=i)), p, p + 2, p - 3, p + step)
    db.commit()


# ==============================================================================
# journal entries
# ==============================================================================
class TestJournalEntries:
    def test_entering_top10_and_aligned_are_entries_with_one_control(self, db):
        prev_order = ASSETS
        cur_order = ["A15"] + ASSETS[:15] + ASSETS[16:]  # A15 jumps to #1, A09 drops out
        prev = _hour(db, TS_PREV, prev_order)
        cur = _hour(db, TS0, cur_order, aligned={"A15"})
        out = journal.write_entries(db, TS0, cur, prev)
        db.commit()
        assert out == {"entries": 2, "controls": 1, "baseline": True}
        got = db.query("SELECT base_asset, trigger, is_control FROM pulse_journal ORDER BY trigger")
        signals = {(r["base_asset"], r["trigger"]) for r in got if not r["is_control"]}
        assert signals == {("A15", "top10"), ("A15", "aligned")}
        control = [r for r in got if r["is_control"]]
        assert len(control) == 1 and control[0]["trigger"] == "control"
        assert control[0]["base_asset"] != "A15"

    def test_staying_in_top10_is_not_an_entry(self, db):
        prev = _hour(db, TS_PREV, ASSETS, aligned={"A00"})
        cur = _hour(db, TS0, ASSETS, aligned={"A00"})
        assert journal.write_entries(db, TS0, cur, prev)["entries"] == 0
        assert db.scalar("SELECT COUNT(*) FROM pulse_journal") == 0

    def test_rerun_is_idempotent_even_if_the_pool_changes(self, db):
        prev = _hour(db, TS_PREV, ASSETS)
        cur_order = ["A15"] + ASSETS[:15] + ASSETS[16:]
        cur = _hour(db, TS0, cur_order)
        journal.write_entries(db, TS0, cur, prev)
        # Same hour, a different pool: the control id is per hour, not per asset.
        journal.write_entries(db, TS0, cur[:12], prev)
        db.commit()
        assert db.scalar("SELECT COUNT(*) FROM pulse_journal WHERE is_control = 1") == 1
        assert db.scalar("SELECT COUNT(*) FROM pulse_journal") == 2

    def test_control_is_deterministic(self):
        cur = [{"base_asset": a, "score": 50.0} for a in ASSETS]
        picks = {journal.draw_control(TS0, cur, {"A01"})["base_asset"] for _ in range(5)}
        assert len(picks) == 1

    def test_no_recent_baseline_writes_nothing(self, db):
        _hour(db, format_instant(T0 - timedelta(hours=5)), ASSETS)
        _hour(db, TS0, ["A15"] + ASSETS[:15])
        assert journal.previous_scored_hour(db, TS0) is None
        assert journal.write_entries(db, TS0)["entries"] == 0

    def test_journal_is_append_only(self, db):
        prev = _hour(db, TS_PREV, ASSETS)
        cur = _hour(db, TS0, ["A15"] + ASSETS[:15])
        journal.write_entries(db, TS0, cur, prev)
        db.commit()
        with pytest.raises(Exception, match="append-only"):
            db.execute("UPDATE pulse_journal SET pulse_score = 0")
        with pytest.raises(Exception, match="append-only"):
            db.execute("DELETE FROM pulse_journal")


# ==============================================================================
# forward returns
# ==============================================================================
class TestForwardReturns:
    def test_entry_is_the_next_bars_open_never_the_signal_bar(self, db):
        # Bars from the signal bar (13:00) through 14:00 + 4h.
        _bars(db, "AAA", T0 - timedelta(hours=1), 8, price=100.0, step=1.0)
        _bars(db, "BTC", T0 - timedelta(hours=1), 8, price=1000.0, step=0.0)
        got = journal.compute_forward_return(db, "AAA", TS0, 4)
        # Bar opening at 15:00 (index 2) opens at 102; exit = close of bar 18:00 (index 5).
        assert got["entry_ts_utc"] == format_instant(T0 + timedelta(hours=1))
        assert got["entry_price"] == 102.0
        assert got["exit_price"] == 106.0
        assert got["return_raw"] == pytest.approx(4 / 102, abs=1e-6)
        assert got["return_vs_btc"] == pytest.approx(4 / 102, abs=1e-6)
        assert got["max_favourable"] == pytest.approx((105 + 2 - 102) / 102, abs=1e-6)
        assert got["max_adverse"] == pytest.approx((102 - 3 - 102) / 102, abs=1e-6)

    def test_a_missing_bar_defers(self, db):
        start = T0 + timedelta(hours=1)
        for i in (0, 1, 3):  # hour 2 missing
            ts = format_instant(start + timedelta(hours=i))
            _bar(db, "AAA", ts, 100, 101, 99, 100)
            _bar(db, "BTC", ts, 100, 101, 99, 100)
        db.commit()
        assert journal.compute_forward_return(db, "AAA", TS0, 4) is None

    def test_missing_btc_defers(self, db):
        _bars(db, "AAA", T0 + timedelta(hours=1), 4)
        assert journal.compute_forward_return(db, "AAA", TS0, 4) is None

    def test_backfill_writes_elapsed_horizons_only_once(self, db, monkeypatch):
        prev = _hour(db, TS_PREV, ASSETS)
        cur = _hour(db, TS0, ["A15"] + ASSETS[:15])
        journal.write_entries(db, TS0, cur, prev)
        for asset in ASSETS + ["BTC"]:
            _bars(db, asset, T0 + timedelta(hours=1), 30)
        now = T0 + timedelta(hours=30)
        first = journal.backfill_returns(db, now=now)
        db.commit()
        entries = db.scalar("SELECT COUNT(*) FROM pulse_journal")
        assert first == entries * 2  # 4h and 24h elapsed; 72h not yet
        assert journal.backfill_returns(db, now=now) == 0
        db.commit()
        assert db.scalar("SELECT COUNT(*) FROM pulse_forward_return") == entries * 2
        assert db.scalar("SELECT COUNT(*) FROM pulse_forward_return WHERE horizon = '72h'") == 0
        with pytest.raises(Exception, match="append-only"):
            db.execute("DELETE FROM pulse_forward_return")

    def test_report_groups_by_version_trigger_horizon(self, db):
        prev = _hour(db, TS_PREV, ASSETS)
        cur = _hour(db, TS0, ["A15"] + ASSETS[:15])
        journal.write_entries(db, TS0, cur, prev)
        for asset in ASSETS + ["BTC"]:
            _bars(db, asset, T0 + timedelta(hours=1), 30)
        journal.backfill_returns(db, now=T0 + timedelta(hours=30))
        db.commit()
        rep = journal.pulse_report(db)
        block = rep["versions"]["pulse-v1"]
        assert set(block) == {"top10", "control"}
        assert set(block["top10"]) == {"4h", "24h"}
        assert block["top10"]["4h"]["n"] == 1
        assert block["top10"]["4h"]["vs_control"]["control_n"] == 1
        assert "kill" not in json.dumps(rep).lower()
        assert "score_version pulse-v1" in journal.render_report(rep)


# ==============================================================================
# alerts
# ==============================================================================
class TestAlerts:
    @pytest.fixture
    def sent(self, monkeypatch):
        from src.report import telegram

        messages: list[str] = []
        monkeypatch.setattr(telegram, "is_configured", lambda: True)
        monkeypatch.setattr(telegram, "send_message", lambda text: messages.append(text) or True)
        return messages

    def test_new_aligned_and_bear_break_in_one_message(self, db, sent):
        prev = _hour(db, TS_PREV, ASSETS, gem={"A03": 2})
        cur = _hour(db, TS0, ASSETS, aligned={"A01"}, states={"A03": "bear_break"}, gem={"A03": 2})
        out = alerts.send_alerts(db, TS0, cur, prev)
        db.commit()
        assert out["sent"] == 2 and out["delivered"] is True
        assert len(sent) == 1 and "A01" in sent[0] and "A03" in sent[0]
        assert db.scalar("SELECT COUNT(*) FROM pulse_alert WHERE delivered = 1") == 2

    def test_bear_break_outside_gem_top_is_silent(self, db, sent):
        prev = _hour(db, TS_PREV, ASSETS, gem={"A03": 40})
        cur = _hour(db, TS0, ASSETS, states={"A03": "bear_break"}, gem={"A03": 40})
        assert alerts.send_alerts(db, TS0, cur, prev)["sent"] == 0
        assert sent == []

    def test_still_aligned_is_never_repeated(self, db, sent):
        prev = _hour(db, TS_PREV, ASSETS, aligned={"A01"})
        cur = _hour(db, TS0, ASSETS, aligned={"A01"})
        assert alerts.send_alerts(db, TS0, cur, prev)["reason"] == "no_change"
        assert sent == []

    def test_dedup_within_24h(self, db, sent):
        # A01 aligned at 10:00, lost it, regained it at 14:00.
        upsert(db, "pulse_alert", [{"ts_utc": format_instant(T0 - timedelta(hours=4)),
                                    "base_asset": "A01", "kind": "aligned", "message": "x",
                                    "delivered": 1, "created_at_utc": TS0}])
        prev = _hour(db, TS_PREV, ASSETS)
        cur = _hour(db, TS0, ASSETS, aligned={"A01"})
        assert alerts.send_alerts(db, TS0, cur, prev)["reason"] == "deduplicated"
        assert sent == []

    def test_daily_cap(self, db, sent, monkeypatch):
        from src.config import get_config

        monkeypatch.setattr(get_config().thresholds.pulse, "alert_max_per_day", 2)
        prev = _hour(db, TS_PREV, ASSETS)
        cur = _hour(db, TS0, ASSETS, aligned={"A01", "A02", "A04"})
        out = alerts.send_alerts(db, TS0, cur, prev)
        db.commit()
        assert out["sent"] == 2
        assert db.scalar("SELECT COUNT(*) FROM pulse_alert") == 2
        # The day is full: the next hour sends nothing.
        ts1 = format_instant(T0 + timedelta(hours=1))
        cur2 = _hour(db, ts1, ASSETS, aligned={"A01", "A02", "A04", "A05"})
        assert alerts.send_alerts(db, ts1, cur2, cur)["reason"] == "daily_cap"

    def test_unset_secrets_skip_quietly_and_record_nothing(self, db, monkeypatch):
        from src.report import telegram

        monkeypatch.setattr(telegram, "is_configured", lambda: False)
        called = []
        monkeypatch.setattr(telegram, "send_message", lambda text: called.append(text))
        prev = _hour(db, TS_PREV, ASSETS)
        cur = _hour(db, TS0, ASSETS, aligned={"A01"})
        assert alerts.send_alerts(db, TS0, cur, prev)["reason"] == "unconfigured"
        assert called == []
        assert db.scalar("SELECT COUNT(*) FROM pulse_alert") == 0

    def test_delivery_failure_records_delivered_zero(self, db, monkeypatch):
        from src.report import telegram

        monkeypatch.setattr(telegram, "is_configured", lambda: True)
        monkeypatch.setattr(telegram, "send_message", lambda text: False)
        prev = _hour(db, TS_PREV, ASSETS)
        cur = _hour(db, TS0, ASSETS, aligned={"A01"})
        out = alerts.send_alerts(db, TS0, cur, prev)
        db.commit()
        assert out["reason"] == "delivery_failed"
        assert db.scalar("SELECT delivered FROM pulse_alert") == 0


# ==============================================================================
# pulse.json
# ==============================================================================
class TestBake:
    def test_shape_matches_the_contract_example(self, db, tmp_path):
        _hour(db, TS_PREV, ASSETS)
        _hour(db, TS0, ["A15"] + ASSETS[:15], aligned={"A15"}, excluded=["XYZ"])
        path = pulse_publish.bake_pulse_json(db, tmp_path, now=T0 + timedelta(minutes=30))
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert set(doc) == set(PULSE_JSON_EXAMPLE)
        assert doc["status"] == "ok"
        assert doc["as_of_utc"] == "2026-09-19T14:00:00Z"
        asset = doc["assets"][0]
        assert set(asset) == set(PULSE_JSON_EXAMPLE["assets"][0])
        assert set(asset["features"]) == set(PULSE_JSON_EXAMPLE["assets"][0]["features"])
        assert set(asset["components"]) == set(PULSE_JSON_EXAMPLE["assets"][0]["components"])
        assert set(doc["risers"][0]) == set(PULSE_JSON_EXAMPLE["risers"][0])
        assert set(doc["excluded"][0]) == set(PULSE_JSON_EXAMPLE["excluded"][0])
        assert asset["asset"] == "A15" and asset["prev_rank"] == 16 and asset["delta"] == 15.0
        assert asset["file"] == "assets/A15.json"
        assert doc["aligned"] == ["A15"]
        assert doc["risers"][0]["asset"] == "A15"
        assert doc["excluded"] == [{"asset": "XYZ", "reason": "thin_book"}]
        assert doc["universe_size"] == 17 and doc["scored"] == 16

    @pytest.mark.parametrize("hours_old", [None, 4])
    def test_absent_or_stale_is_unavailable(self, db, tmp_path, hours_old):
        if hours_old:
            _hour(db, TS0, ASSETS)
        path = pulse_publish.bake_pulse_json(db, tmp_path, now=T0 + timedelta(hours=hours_old or 0))
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["status"] == "unavailable" and doc["assets"] == []
        assert set(doc) == set(PULSE_JSON_EXAMPLE)

    def test_database_error_still_writes_unavailable_then_raises(self, tmp_path):
        with pytest.raises(RuntimeError):
            pulse_publish.bake_pulse_json(None, tmp_path)
        doc = json.loads((tmp_path / "pulse.json").read_text(encoding="utf-8"))
        assert doc["status"] == "unavailable"

    def test_key_shaped_string_is_refused(self, db, tmp_path):
        from src.report.publish import SecretLeak

        _hour(db, TS0, ["ghp_" + "a" * 36])
        with pytest.raises(SecretLeak):
            pulse_publish.bake_pulse_json(db, tmp_path, now=T0)
        assert not (tmp_path / "pulse.json").exists()

    def test_pulse_json_is_git_ignored(self):
        import subprocess
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        res = subprocess.run(
            ["git", "check-ignore", "-q", "data/public/pulse.json"], cwd=root, check=False
        )
        assert res.returncode == 0
        # ...while the committed files are not.
        res = subprocess.run(
            ["git", "check-ignore", "-q", "data/public/latest.json"], cwd=root, check=False
        )
        assert res.returncode == 1


# ==============================================================================
# run_pulse with stubbed stages
# ==============================================================================
class _Result:
    def __init__(self, status):
        self.status = status
        self.error_message = None if status != "failed" else "boom"

    @property
    def ok(self):
        return self.status in {"success", "partial"}


def _bars_frame(n=60, start=T0 - timedelta(hours=60)):
    idx = pd.date_range(start, periods=n, freq="h", tz="UTC")
    close = [100.0 + i for i in range(n)]
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close,
         "quote_volume": [1000.0] * n, "taker_buy_quote": [600.0] * n, "trades": [1] * n},
        index=idx,
    )


def _install_stubs(monkeypatch, status="success", survivors=("AAA", "BBB", "CCC")):
    window = MarketWindow(as_of=T0, survivors=list(survivors),
                          bars={a: _bars_frame() for a in survivors})

    class Collector:
        def __init__(self):
            self.window = None

        async def run(self, as_of):
            assert as_of == T0
            self.window = window
            return _Result(status)

    def hour_floor(dt):
        return dt.replace(minute=0, second=0, microsecond=0)

    def compute_features(win, cfg=None):
        data = {c: [float("nan")] * len(win.survivors) for c in FEATURE_COLUMNS}
        data["flags"] = [[] for _ in win.survivors]
        data["state_4h"] = ["bull_trend"] * len(win.survivors)
        data["bars_since_4h"] = [3.0] * len(win.survivors)
        data["flow_24h"] = [0.2] * len(win.survivors)
        return pd.DataFrame(data, index=pd.Index(win.survivors, name="base_asset"))

    def resample_4h(bars):
        return bars.resample("4h").agg({"close": "last"}).dropna()

    def score_pulse(features, gem_ranks, cfg=None):
        rows = []
        for i, asset in enumerate(features.index):
            excluded = asset == "CCC"
            rows.append({
                "score": float("nan") if excluded else 80.0 - i,
                "rank": float("nan") if excluded else i + 1,
                "components": {"flow": 70.0},
                "coverage": 1.0,
                "state_4h": "bull_trend", "state_1h": "neutral", "oi_quadrant": "neutral",
                "flags": ["thin_book"] if excluded else [],
                "penalty": 1.0,
                "gem_rank": gem_ranks.get(asset),
                "aligned": asset == "AAA",
            })
        return pd.DataFrame(rows, index=features.index, columns=list(PULSE_COLUMNS))

    for name, attrs in {
        "src.pulse.data": {"BinancePulseCollector": Collector, "hour_floor": hour_floor},
        "src.pulse.features": {"compute_features": compute_features, "resample_4h": resample_4h},
        "src.pulse.score": {"score_pulse": score_pulse},
    }.items():
        mod = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        monkeypatch.setitem(sys.modules, name, mod)


class TestRunPulse:
    def test_writes_one_row_per_survivor(self, db, monkeypatch):
        _install_stubs(monkeypatch)
        upsert(db, "layer2_result", [{
            "run_date": "2026-09-19", "base_asset": "AAA", "total_score": 70.0, "rank": 3,
            "universe_size": 3, "fetched_at_utc": TS0}])
        db.commit()
        from src.pulse.run import run_pulse

        summary = asyncio.run(run_pulse(T0 + timedelta(minutes=25)))
        assert summary["ok"] and summary["ts_utc"] == TS0
        rows = {r["base_asset"]: r for r in db.query("SELECT * FROM pulse_result")}
        assert set(rows) == {"AAA", "BBB", "CCC"}
        assert rows["CCC"]["score"] is None and rows["CCC"]["rank"] is None
        assert rows["AAA"]["gem_rank"] == 3 and rows["AAA"]["aligned"] == 1
        assert rows["AAA"]["score_version"] == "pulse-v1"
        feats = json.loads(rows["AAA"]["features"])
        assert len(feats["spark_1h"]) == 48 and len(feats["flow_1h"]) == 48
        assert feats["flow_1h"][0] == pytest.approx(0.2)
        assert 0 < len(feats["spark_4h"]) <= 42
        assert feats["bars_since_4h"] == 3.0 and feats["ret_4h"] is None
        assert "spark_1h" not in json.loads(rows["CCC"]["features"])
        # Idempotent per hour.
        asyncio.run(run_pulse(T0 + timedelta(minutes=50)))
        assert db.scalar("SELECT COUNT(*) FROM pulse_result") == 3

    def test_collector_failure_writes_nothing(self, db, monkeypatch):
        _install_stubs(monkeypatch, status="failed")
        from src.pulse.run import run_pulse

        summary = asyncio.run(run_pulse(T0))
        assert not summary["ok"] and "collector failed" in summary["error"]
        assert db.scalar("SELECT COUNT(*) FROM pulse_result") == 0

    def test_ensure_tables_is_a_noop_when_present(self, db):
        from src.pulse.run import ensure_pulse_tables

        assert ensure_pulse_tables(db) is False


class TestCli:
    def test_pulse_run_exit_codes(self, db, monkeypatch):
        from typer.testing import CliRunner

        from src.cli import app

        _install_stubs(monkeypatch, status="failed")
        res = CliRunner().invoke(app, ["pulse", "run", "--as-of", "2026-09-19T14:25:00Z"])
        assert res.exit_code == 1 and "NOT scored" in res.output
        _install_stubs(monkeypatch)
        res = CliRunner().invoke(app, ["pulse", "run", "--as-of", "2026-09-19T14:25:00Z"])
        assert res.exit_code == 0, res.output
        res = CliRunner().invoke(app, ["pulse", "run", "--as-of", "not-a-date"])
        assert res.exit_code == 2

    def test_pulse_bake_and_report(self, db, monkeypatch, tmp_path):
        from typer.testing import CliRunner

        from src import cli
        from src.config import get_config

        cfg = get_config()
        monkeypatch.setattr(type(cfg), "path", lambda self, p: tmp_path / "public")
        res = CliRunner().invoke(cli.app, ["pulse", "bake"])
        assert res.exit_code == 0, res.output
        assert "unavailable" in res.output
        res = CliRunner().invoke(cli.app, ["pulse", "report"])
        assert res.exit_code == 0 and "No completed forward returns" in res.output


class TestSparklinePruning:
    """Sparklines are drawn once. Kept forever they are ~2.5GB of Turso a year."""

    def _row(self, db, ts: str, spark: bool) -> None:
        features = {"flow_24h": 0.1}
        if spark:
            features |= {"spark_1h": [1.0, 2.0], "flow_1h": [0.1], "spark_4h": [1.5]}
        upsert(db, "pulse_result", [{
            "ts_utc": ts, "base_asset": "AAA", "score": 70.0, "rank": 1,
            "universe_size": 1, "features": json_dump(features), "aligned": 0,
            "fetched_at_utc": ts,
        }])

    def test_only_hours_older_than_the_keep_window_lose_their_sparks(self, db):
        from src.pulse.run import prune_sparklines

        now = datetime(2026, 9, 19, 14, tzinfo=timezone.utc)
        for hours in (0, 1, 3, 200):
            self._row(db, format_instant(now - timedelta(hours=hours)), spark=True)

        assert prune_sparklines(db, now) == 1, "only the 3h-old row is in the band"
        kept = {
            r["ts_utc"]: json.loads(r["features"])
            for r in db.query("SELECT ts_utc, features FROM pulse_result")
        }
        assert "spark_1h" in kept[format_instant(now)]
        assert "spark_1h" in kept[format_instant(now - timedelta(hours=1))]
        assert "spark_1h" not in kept[format_instant(now - timedelta(hours=3))]
        # Outside the band, and past evidence is never rewritten in bulk.
        assert "spark_1h" in kept[format_instant(now - timedelta(hours=200))]
        # The rest of the row survives, and a second pass is a no-op.
        assert kept[format_instant(now - timedelta(hours=3))]["flow_24h"] == 0.1
        assert prune_sparklines(db, now) == 0
