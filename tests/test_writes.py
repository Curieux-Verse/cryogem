"""
D-053: what an upsert may overwrite, and how much it sends in one statement.
"""

from __future__ import annotations

from src.db.writes import BATCH_ROWS, upsert


def _universe(interval: float | None) -> dict:
    return {
        "snapshot_date": "2026-09-15",
        "exchange": "binance",
        "symbol": "BTCUSDT",
        "base_asset": "BTC",
        "quote_asset": "USDT",
        "status": "TRADING",
        "funding_interval_hours": interval,
        "fetched_at_utc": "2026-09-15T03:10:00Z",
    }


class TestKeepWhenNull:
    def test_a_rerun_without_the_interval_keeps_the_first_runs_value(self, db):
        """A NULL on a same-day re-run means the funding-history read failed
        this time, not that the interval went away."""
        upsert(db, "universe_snapshot", [_universe(8.0)])
        upsert(db, "universe_snapshot", [_universe(None)])
        assert db.scalar("SELECT funding_interval_hours FROM universe_snapshot") == 8.0

    def test_a_real_new_value_still_replaces_the_old_one(self, db):
        upsert(db, "universe_snapshot", [_universe(8.0)])
        upsert(db, "universe_snapshot", [_universe(4.0)])
        assert db.scalar("SELECT funding_interval_hours FROM universe_snapshot") == 4.0


class TestFirstSeen:
    def test_news_lag_stays_first_seen_rather_than_becoming_item_age(self, db):
        first = {
            "news_id": "n1",
            "published_at_utc": "2026-09-15T00:00:00Z",
            "fetched_at_utc": "2026-09-15T00:01:00Z",
            "lag_seconds": 60.0,
            "title": "headline",
        }
        upsert(db, "news_item", [first])
        later = {
            **first,
            "fetched_at_utc": "2026-09-15T05:00:00Z",
            "lag_seconds": 18_060.0,
            "title": "headline, edited",
        }
        upsert(db, "news_item", [later])
        row = db.query_one("SELECT fetched_at_utc, lag_seconds, title FROM news_item")
        assert row["fetched_at_utc"] == "2026-09-15T00:01:00Z"
        assert row["lag_seconds"] == 60.0
        # Everything that is not a first-seen measurement still updates.
        assert row["title"] == "headline, edited"


class TestPreferredSource:
    """D-068. price_daily has no `source` in its key, and the journal reads only
    kline closes: a coingecko re-run that overwrote the row made the day's bar
    invisible and left the journal entry pending forever."""

    def _price(self, source: str, close: float) -> dict:
        return {
            "snapshot_date": "2026-09-01",
            "base_asset": "BTC",
            "close_usd": close,
            "source": source,
            "fetched_at_utc": "2026-09-02T03:10:00Z",
        }

    def _row(self, db) -> dict:
        return db.query("SELECT close_usd, source FROM price_daily")[0]

    def test_a_coingecko_price_never_overwrites_a_kline_close(self, db):
        upsert(db, "price_daily", [self._price("binance_klines", 100.0)])
        upsert(db, "price_daily", [self._price("coingecko", 111.0)])
        db.commit()
        assert self._row(db) == {"close_usd": 100.0, "source": "binance_klines"}

    def test_a_kline_close_still_replaces_a_coingecko_price(self, db):
        upsert(db, "price_daily", [self._price("coingecko", 111.0)])
        upsert(db, "price_daily", [self._price("binance_klines", 100.0)])
        db.commit()
        assert self._row(db) == {"close_usd": 100.0, "source": "binance_klines"}

    def test_a_corrected_kline_close_still_lands(self, db):
        """The rule is about sources, not about freezing the row."""
        upsert(db, "price_daily", [self._price("binance_klines", 100.0)])
        upsert(db, "price_daily", [self._price("binance_klines", 101.0)])
        db.commit()
        assert self._row(db) == {"close_usd": 101.0, "source": "binance_klines"}


class TestBatching:
    def test_rows_are_sent_in_bounded_batches(self, db, monkeypatch):
        """On Turso one batch is one HTTP request."""
        sizes: list[int] = []
        original = db.executemany

        def spy(sql, rows):
            sizes.append(len(rows))
            return original(sql, rows)

        monkeypatch.setattr(db, "executemany", spy)
        rows = [
            {
                "snapshot_date": f"2020-01-01+{i:05d}",
                "base_asset": "BTC",
                "close_usd": 1.0,
                "source": "binance_klines",
                "fetched_at_utc": "2026-09-15T03:10:00Z",
            }
            for i in range(BATCH_ROWS * 2 + 1)
        ]
        assert upsert(db, "price_daily", rows) == BATCH_ROWS * 2 + 1
        assert sizes == [BATCH_ROWS, BATCH_ROWS, 1]
        assert db.scalar("SELECT COUNT(*) FROM price_daily") == BATCH_ROWS * 2 + 1
