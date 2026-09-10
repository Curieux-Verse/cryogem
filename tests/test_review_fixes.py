"""
Regressions for the defects a review pass found after Phase 11.

Each test names the wrong behaviour it prevents, because a test whose name
only describes the fix stops explaining itself the moment the fix looks
obvious.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.db.writes import upsert
from src.journal import forward_returns as fr
from src.screening import layer2_score as l2

RUN_DATE = "2026-06-01"


class TestUnscorableAssetIsNotPersistedAsZero:
    """`_clean(...) or 0.0` turned "no data in any block" into a score of 0.

    total_score is NOT NULL, so the choice is between omitting the row and
    inventing a number. Inventing one is worse than it looks: the row then
    occupies a rank, enters the journal as a signal, and its forward return
    gets attributed to a score that was never computed.
    """

    @staticmethod
    def _frame(assets):
        df = pd.DataFrame(index=pd.Index(assets, name="base_asset"))
        for column in (
            "market_cap_usd", "circulating_supply", "total_supply", "pct_below_ath",
            "price_usd", "tvl_usd", "fees_7d_usd", "fees_30d_usd", "revenue_30d_usd",
            "revenue_prev_30d_usd", "revenue_annualised", "active_addresses_24h",
            "has_fundamentals", "emissions_trajectory", "burned_pct_of_total",
            "staked_ratio", "staked_is_team_controlled", "social_volume_z",
            "social_dominance", "days_to_next_major_unlock",
            "days_since_last_major_unlock", "unlock_overhang_cleared",
            "has_unlock_record", "positive_catalyst_30d", "monitoring_tag_active",
            "return_7d", "return_30d",
        ):
            df[column] = np.nan
        return df

    def test_a_nan_total_is_omitted_rather_than_written_as_zero(self, db, monkeypatch):
        assets = ["GOOD", "VOID"]
        monkeypatch.setattr(l2, "load_scoring_frame", lambda *a, **k: self._frame(assets))

        real_score = l2.Layer2Scorer.score

        def score_with_a_void(self, df):
            out = real_score(self, df)
            out.loc["VOID", "total_score"] = np.nan
            return out

        monkeypatch.setattr(l2.Layer2Scorer, "score", score_with_a_void)

        rows = l2.run_layer2(db, RUN_DATE, assets)
        written = {r["base_asset"] for r in rows}
        assert "GOOD" in written
        assert "VOID" not in written, "an unmeasured asset must not be scored zero"

        stored = db.query("SELECT base_asset, total_score FROM layer2_result")
        assert {r["base_asset"] for r in stored} == {"GOOD"}
        assert all(r["total_score"] is not None for r in stored)

    def test_a_genuine_zero_still_reaches_the_database(self, db, monkeypatch):
        """`or 0.0` also swallowed a real 0.0 -- indistinguishable from the bug."""
        assets = ["FLOOR"]
        monkeypatch.setattr(l2, "load_scoring_frame", lambda *a, **k: self._frame(assets))

        real_score = l2.Layer2Scorer.score

        def score_at_the_floor(self, df):
            out = real_score(self, df)
            out.loc["FLOOR", "total_score"] = 0.0
            return out

        monkeypatch.setattr(l2.Layer2Scorer, "score", score_at_the_floor)

        rows = l2.run_layer2(db, RUN_DATE, assets)
        assert len(rows) == 1
        assert rows[0]["total_score"] == 0.0


class TestJournalRefusesASignalWithoutAScore:
    """journal_entry is append-only and defended by DB triggers.

    A fabricated total_score written there can never be corrected, so the
    entry is refused instead.
    """

    @staticmethod
    def _prices(db, asset: str) -> dict[str, float]:
        upsert(
            db,
            "price_daily",
            [
                {
                    "snapshot_date": RUN_DATE,
                    "base_asset": name,
                    "open_usd": price,
                    "high_usd": price,
                    "low_usd": price,
                    "close_usd": price,
                    "volume_usd": 1e6,
                    "source": "test",
                    "fetched_at_utc": f"{RUN_DATE}T00:00:00Z",
                }
                for name, price in ((asset, 100.0), ("BTC", 60_000.0))
            ],
        )
        return {asset: 100.0, "BTC": 60_000.0}

    def test_a_signal_with_a_null_score_is_skipped(self, db):
        prices = self._prices(db, "AAA")
        row = fr._build_entry(
            db,
            RUN_DATE,
            {"base_asset": "AAA", "rank": 1, "total_score": None},
            prices,
            prices["BTC"],
            is_control=False,
        )
        assert row is None, "refuse the entry rather than invent a score"

    def test_a_control_keeps_its_explicit_zero_sentinel(self, db):
        """A control was never ranked. rank 0 / score 0.0 says exactly that."""
        prices = self._prices(db, "CCC")
        row = fr._build_entry(
            db,
            RUN_DATE,
            {"base_asset": "CCC", "rank": 0, "total_score": 0.0},
            prices,
            prices["BTC"],
            is_control=True,
        )
        assert row is not None
        assert row["is_control"] == 1
        assert row["total_score"] == 0.0
        assert row["rank"] == 0

    def test_a_real_score_is_written_unchanged(self, db):
        prices = self._prices(db, "BBB")
        row = fr._build_entry(
            db,
            RUN_DATE,
            {"base_asset": "BBB", "rank": 3, "total_score": 61.25},
            prices,
            prices["BTC"],
            is_control=False,
        )
        assert row["total_score"] == pytest.approx(61.25)


class TestRollbackHonestyOnLibsql:
    def test_sqlite_rollback_actually_rolls_back(self, db):
        db.execute(
            "INSERT INTO price_daily (snapshot_date, base_asset, open_usd, high_usd, "
            "low_usd, close_usd, volume_usd, source, fetched_at_utc) "
            "VALUES ('2026-01-01','ZZZ',1,1,1,1,1,'test','2026-01-01T00:00:00Z')"
        )
        db.rollback()
        assert db.scalar("SELECT COUNT(*) FROM price_daily WHERE base_asset = 'ZZZ'") == 0

    def test_the_libsql_no_op_is_documented_where_a_reader_will_find_it(self):
        """The two backends genuinely differ; the docstring must say so."""
        from src.db.connection import Database

        doc = Database.rollback.__doc__ or ""
        assert "CANNOT UNDO" in doc
        assert "upsert" in doc, "point the reader at the real mitigation"
