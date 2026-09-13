"""
Phase 9: reporting, publishing, and the notification path.

The report is where the system talks to its operator, so the tests here are
about honesty of presentation: a dark check must not read as a pass, a row
count must not read as data coverage, an infinity must survive the JSON
boundary, and no payload may carry a secret.
"""

from __future__ import annotations

import json

import pytest

from src.db.writes import upsert
from src.report import daily, publish, telegram

RUN_DATE = "2026-06-01"


def _l1(asset: str, passed: bool, failed=None, checks=None) -> dict:
    return {
        "run_date": RUN_DATE,
        "base_asset": asset,
        "passed": 1 if passed else 0,
        "failed_checks": json.dumps(failed or []),
        "check_values": json.dumps(checks or {}),
        "fetched_at_utc": f"{RUN_DATE}T03:10:00Z",
    }


def _check(passed: bool, value, threshold, reason="", dark=False, display=None) -> dict:
    return {
        "passed": passed,
        "value": value,
        "value_display": display if display is not None else str(value),
        "threshold": threshold,
        "reason": reason,
        "source_unavailable": dark,
        "description": "",
    }


def _seed(db, rows: list[dict], ranked: list[tuple[str, float, int]] | None = None) -> None:
    upsert(db, "layer1_result", rows)
    for asset, score, rank in ranked or []:
        upsert(
            db,
            "layer2_result",
            [
                {
                    "run_date": RUN_DATE,
                    "base_asset": asset,
                    "total_score": score,
                    "score_fundamental": None,
                    "score_supply": 70.0,
                    "score_sector": None,
                    "score_drawdown": 60.0,
                    "score_events": 50.0,
                    "score_attention": None,
                    "rank": rank,
                    "universe_size": len(rows),
                    "percentiles": json.dumps(
                        {
                            "events.overhang_cleared": 100.0,
                            "events.monitoring_tag": 100.0,
                            "events.positive_catalyst": 0.0,
                        }
                    ),
                    "fetched_at_utc": f"{RUN_DATE}T03:10:00Z",
                }
            ],
        )
    db.commit()


class TestDarkChecks:
    def test_a_check_dark_for_everyone_is_reported_as_dark(self, db):
        """D-007: a source outage is not a universe of passing assets."""
        rows = [
            _l1(a, True, checks={"L1_HOLDER_CONC": _check(True, None, 0.6, dark=True)})
            for a in ("AAA", "BBB")
        ]
        _seed(db, rows, ranked=[("AAA", 80.0, 1)])
        payload = daily.gather(db, RUN_DATE)
        assert payload["dark_checks"] == ["L1_HOLDER_CONC"]

        text = daily.render(payload)
        assert "Checks dark this run" in text
        assert "A dark check is not a passing check" in text

    def test_a_check_dark_for_only_some_assets_is_not_dark(self, db):
        """One asset with no holder data is an asset-level gap, which FAILS.
        Calling that a source outage would excuse the individual asset."""
        rows = [
            _l1("AAA", True, checks={"L1_HOLDER_CONC": _check(True, 0.4, 0.6)}),
            _l1("BBB", False, checks={"L1_HOLDER_CONC": _check(False, None, 0.6, dark=True)}),
        ]
        _seed(db, rows)
        assert daily.gather(db, RUN_DATE)["dark_checks"] == []


class TestCoverage:
    def test_a_row_is_not_data(self, db):
        """The DefiLlama collector writes a row per asset with
        has_fundamentals = 0. Counting rows reported 100% coverage of a block
        that was fed for 13 of 528 assets."""
        _seed(db, [_l1(a, True) for a in ("AAA", "BBB", "CCC", "DDD")])
        upsert(
            db,
            "fundamentals_snapshot",
            [
                {
                    "snapshot_date": RUN_DATE,
                    "base_asset": asset,
                    "protocol_slug": "x" if asset == "AAA" else None,
                    "tvl_usd": 1e6 if asset == "AAA" else None,
                    "fees_24h_usd": None,
                    "fees_7d_usd": None,
                    "fees_30d_usd": None,
                    "revenue_24h_usd": None,
                    "revenue_7d_usd": None,
                    "revenue_30d_usd": None,
                    "revenue_prev_30d_usd": None,
                    "revenue_annualised": None,
                    "active_addresses_24h": None,
                    "has_fundamentals": 1 if asset == "AAA" else 0,
                    "fetched_at_utc": f"{RUN_DATE}T03:10:00Z",
                }
                for asset in ("AAA", "BBB", "CCC", "DDD")
            ],
        )
        db.commit()

        coverage = daily.data_quality(db, RUN_DATE)["coverage"]["fundamentals_snapshot"]
        assert coverage["rows_present"] == 4
        assert coverage["assets"] == 1
        assert coverage["of_universe"] == 0.25

    def test_coverage_cannot_exceed_the_universe(self, db):
        """market_snapshot covers 1,218 CoinGecko assets against a 528-asset
        perp universe. Un-intersected, coverage read 230%."""
        _seed(db, [_l1("AAA", True)])
        upsert(
            db,
            "market_snapshot",
            [
                {
                    "snapshot_date": RUN_DATE,
                    "base_asset": asset,
                    "coingecko_id": asset.lower(),
                    "price_usd": 1.0,
                    "market_cap_usd": 1e8,
                    "fdv_usd": None,
                    "circulating_supply": None,
                    "total_supply": None,
                    "max_supply": None,
                    "spot_volume_24h_usd": None,
                    "ath_usd": None,
                    "ath_date": None,
                    "pct_below_ath": None,
                    "price_change_24h_pct": None,
                    "fetched_at_utc": f"{RUN_DATE}T03:10:00Z",
                }
                for asset in ("AAA", "ZZZ", "YYY")
            ],
        )
        db.commit()
        coverage = daily.data_quality(db, RUN_DATE)["coverage"]["market_snapshot"]
        assert coverage["assets"] == 1
        assert coverage["of_universe"] == 1.0


class TestCollectorStatus:
    def test_partial_is_not_a_failure(self, db):
        """'partial' means the collector ran and degraded gracefully. Bucketed
        with 'failed', five working collectors reported 0% success."""
        _seed(db, [_l1("AAA", True)])
        upsert(
            db,
            "collector_run",
            [
                {
                    "run_id": f"r{i}",
                    "collector_name": "coingecko",
                    "started_at_utc": f"{RUN_DATE}T03:1{i}:00Z",
                    "ended_at_utc": f"{RUN_DATE}T03:1{i}:30Z",
                    "status": status,
                    "rows_written": 10,
                    "error_message": None,
                }
                for i, status in enumerate(("partial", "partial", "success"))
            ],
        )
        db.commit()
        stats = daily.data_quality(db, RUN_DATE)["collectors"]["coingecko"]
        assert stats["failed"] == 0
        assert stats["partial"] == 2
        assert stats["completion_rate"] == 1.0

        text = daily.render(daily.gather(db, RUN_DATE))
        assert "No failed runs." in text
        assert "they are not outages" in text


class TestNotableFailures:
    def test_biggest_single_check_failure_comes_first(self, db):
        """The table exists to show the biggest name the filter removed --
        the one the reader is most likely to think it got wrong."""
        rows = [
            _l1(
                "SMALL",
                False,
                failed=["L1_FLOAT"],
                checks={
                    "L1_FLOAT": _check(False, 0.2, 0.3),
                    "L1_MCAP": _check(True, 5e7, 3e7),
                },
            ),
            _l1(
                "HUGE",
                False,
                failed=["L1_PERP_SPOT"],
                checks={
                    "L1_PERP_SPOT": _check(False, 87.0, 40.0),
                    "L1_MCAP": _check(True, 9.6e9, 3e7),
                },
            ),
            _l1(
                "MANY",
                False,
                failed=["L1_FLOAT", "L1_MCAP", "L1_OI_MCAP"],
                checks={
                    "L1_FLOAT": _check(False, 0.1, 0.3),
                    "L1_MCAP": _check(False, 1e10, 3e7),
                },
            ),
        ]
        _seed(db, rows)
        notable = daily._notable_failures(daily.gather(db, RUN_DATE)["failures"])
        # HUGE before SMALL (bigger cap), and both before MANY (more checks).
        assert [r["base_asset"] for r in notable] == ["HUGE", "SMALL", "MANY"]

    def test_report_survives_an_empty_day(self, db):
        text = daily.render(daily.gather(db, RUN_DATE))
        assert "No universe rows" in text
        # An empty template pretending to be a screen is worse than a stated
        # absence.
        assert "Top 15" not in text


class TestPublish:
    def test_infinity_crosses_the_json_boundary_as_a_string(self, db, tmp_path):
        """An orphan perp's ratio is deliberately inf, never None. json.dumps
        emits a bare `Infinity`, which JSON.parse rejects -- so every page
        would fail to load, on the assets the check exists to catch."""
        payload = {"value": float("inf"), "nested": [float("-inf"), float("nan")]}
        name, size = publish._write(tmp_path, "t.json", payload)
        parsed = json.loads((tmp_path / "t.json").read_text(encoding="utf-8"))
        assert parsed["value"] == "Infinity"
        assert parsed["nested"] == ["-Infinity", None]
        assert size > 0

    def test_every_published_file_is_strict_json(self, db, tmp_path, monkeypatch):
        rows = [
            _l1(
                "ORPHAN",
                False,
                failed=["L1_PERP_SPOT"],
                checks={"L1_PERP_SPOT": _check(False, float("inf"), 40.0, display="inf")},
            ),
            _l1("AAA", True, checks={"L1_MCAP": _check(True, 1e8, 3e7)}),
        ]
        _seed(db, rows, ranked=[("AAA", 80.0, 1)])
        monkeypatch.setattr(
            publish.get_config().settings.reporting, "public_json_dir", str(tmp_path)
        )
        monkeypatch.setattr(publish.get_config(), "repo_root", tmp_path.parent)

        written = publish.publish_all(RUN_DATE)
        assert written
        for name, _size in written:
            raw = (tmp_path / name).read_text(encoding="utf-8")
            # parse_constant fires only on Infinity/-Infinity/NaN.
            json.loads(
                raw,
                parse_constant=lambda c: pytest.fail(f"{name} contains non-JSON {c}"),
            )

    # The fake tokens below are split into adjacent literals ("github_" "pat_...")
    # that Python joins at compile time. Written whole, they tripped ci.yml's
    # secrets-scan on every push from Phase 9 on, and a scan that is always red
    # is one nobody reads -- so a REAL committed token would have gone unnoticed.
    def test_a_secret_shaped_string_refuses_to_publish(self, db, tmp_path):
        """The CI grep runs on the built bundle, by which point the value is
        already in a commit -- and the repo is public, so that is permanent."""
        leaky = {"note": "Authorization: Bearer github_" "pat_11ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"}
        with pytest.raises(publish.SecretLeak):
            publish._write(tmp_path, "leak.json", leaky)
        assert not (tmp_path / "leak.json").exists()

    def test_secret_value_is_not_echoed_in_the_error(self, db, tmp_path):
        token = "github_" "pat_11ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
        with pytest.raises(publish.SecretLeak) as exc:
            publish._write(tmp_path, "leak.json", {"t": token})
        # An exception message lands in the Actions log, which is public.
        assert token not in str(exc.value)

    def test_non_ascii_tickers_get_distinct_filenames(self, db):
        """Five CJK-named Binance contracts all sanitised to 'unknown.json':
        four assets were overwritten and one verdict was shown under five
        names."""
        names = {publish._safe_name(t) for t in ("哈基米", "币安人生", "牛来", "龙虾", "我踏马来了")}
        assert len(names) == 5
        assert publish._safe_name("1000PEPE") == "1000PEPE"
        assert publish._safe_name("../../etc/passwd") not in {"../../etc/passwd"}
        assert "/" not in publish._safe_name("a/b")

    def test_rejected_wall_groups_by_check_and_orders_by_size(self, db):
        rows = [
            _l1(
                "BIG",
                False,
                failed=["L1_MCAP"],
                checks={"L1_MCAP": _check(False, 2.9e7, 3e7)},
            ),
            _l1(
                "SMALLER",
                False,
                failed=["L1_MCAP"],
                checks={"L1_MCAP": _check(False, 1e6, 3e7)},
            ),
        ]
        _seed(db, rows)
        out = publish.build_rejected(daily.gather(db, RUN_DATE))
        group = out["groups"][0]
        assert group["check_id"] == "L1_MCAP"
        assert [a["asset"] for a in group["assets"]] == ["BIG", "SMALLER"]
        assert out["ordering"] == "market_cap_desc"

    def test_ranked_rows_carry_the_file_they_link_to(self, db):
        _seed(db, [_l1("AAA", True)], ranked=[("AAA", 80.0, 1)])
        out = publish.build_latest(db, RUN_DATE, daily.gather(db, RUN_DATE))
        assert out["ranked"][0]["file"] == "assets/AAA.json"
        assert out["funnel"]["survivors"] == 1
        assert "overhang_cleared" in out["ranked"][0]["flags"]


class TestTelegram:
    def test_unconfigured_is_a_no_op_not_an_error(self, db, monkeypatch):
        """Losing a phone notification must never fail a screening run."""
        monkeypatch.setattr(telegram.get_config().secrets, "telegram_bot_token", None)
        monkeypatch.setattr(telegram.get_config().secrets, "telegram_chat_id", None)
        assert telegram.is_configured() is False
        assert telegram.send_message("hello") is False
        assert telegram.send_daily_summary(RUN_DATE) is False

    def test_a_network_failure_returns_false_rather_than_raising(self, db, monkeypatch):
        import httpx

        monkeypatch.setattr(telegram.get_config().secrets, "telegram_bot_token", "t")
        monkeypatch.setattr(telegram.get_config().secrets, "telegram_chat_id", "1")

        def boom(*args, **kwargs):
            raise httpx.ConnectError("no route to host")

        monkeypatch.setattr(telegram.httpx, "post", boom)
        assert telegram.send_message("hello") is False

    def test_summary_names_dark_checks(self, db):
        rows = [
            _l1(a, True, checks={"L1_UNLOCK": _check(True, None, 0.05, dark=True)})
            for a in ("AAA", "BBB")
        ]
        _seed(db, rows, ranked=[("AAA", 80.0, 1)])
        text = telegram.format_summary(daily.gather(db, RUN_DATE))
        # Escaped for parse_mode=Markdown, where a bare `_` opens italics and an
        # unclosed one gets the message refused. Telegram displays it as L1_UNLOCK.
        assert r"L1\_UNLOCK" in text
        assert "UNMEASURED" in text
        # The summary must not read as a recommendation.
        assert "not a buy signal" in text

    def test_summary_is_truncated_not_rejected(self, db, monkeypatch):
        sent: dict = {}

        class Response:
            status_code = 200

        def capture(url, json, timeout):
            sent.update(json)
            return Response()

        monkeypatch.setattr(telegram.get_config().secrets, "telegram_bot_token", "t")
        monkeypatch.setattr(telegram.get_config().secrets, "telegram_chat_id", "1")
        monkeypatch.setattr(telegram.httpx, "post", capture)

        assert telegram.send_message("x" * 9000) is True
        assert len(sent["text"]) <= telegram.MAX_MESSAGE_CHARS
        assert "truncated" in sent["text"]
