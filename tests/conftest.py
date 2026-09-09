"""
# WHY: ------------------------------------------------------------------------
# Shared test fixtures.
#
# Two rules the whole suite obeys:
#   1. NO NETWORK. Every collector test drives transform() from a saved fixture
#      in tests/fixtures/. A test suite that hits Binance is a test suite that
#      fails when Binance is slow, and it is not testing our code anyway.
#   2. Every test gets its OWN database file. Tests that share a database pass
#      or fail depending on the order pytest happens to pick.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.db import connection as connection_module
from src.db.connection import Database

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    """Load a saved API response from tests/fixtures/."""
    path = FIXTURE_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"missing test fixture: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def db(tmp_path, monkeypatch) -> Database:
    """A fresh, schema-applied sqlite database, isolated per test."""
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(connection_module, "_sqlite_path", lambda: db_path)
    # Guarantee the local backend even if a developer has Turso vars exported.
    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
    monkeypatch.setattr(
        connection_module.get_config().secrets, "turso_database_url", None, raising=False
    )
    database = connection_module.open_database()
    database.apply_schema()
    yield database
    database.close()


@pytest.fixture
def as_of():
    """A fixed logical timestamp so tests never depend on the wall clock."""
    from datetime import datetime, timezone

    return datetime(2026, 9, 9, 3, 12, 0, tzinfo=timezone.utc)
