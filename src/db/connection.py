"""
# WHY: ------------------------------------------------------------------------
# One database API for two very different backends.
#
# Local development uses plain sqlite3 (a file on disk). CI has no persistent
# filesystem, so it uses Turso (managed libSQL) over HTTP. Both speak SQLite
# SQL, so the rest of the codebase must never know which one it is talking to --
# otherwise every query grows a branch and the two paths drift apart.
#
# The selection rule is a single environment variable: if TURSO_DATABASE_URL is
# set we use Turso, otherwise we use the local file. Nothing else decides it.
#
# Two things this module deliberately does NOT do:
#   * It does not expose raw cursors. Callers get lists of dicts, so a schema
#     change cannot silently reorder positional tuple unpacking somewhere.
#   * It does not offer a "delete" helper. The journal is append-only and the
#     schema enforces it with triggers; no convenience wrapper should exist that
#     makes violating that look routine.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from src.config import get_config

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def _split_sql_statements(sql: str) -> list[str]:
    """Split a schema script into individual statements.

    Needed because libSQL's client has no `executescript`. A naive split on ';'
    would tear CREATE TRIGGER bodies in half -- the trigger body itself contains
    semicolons -- so BEGIN ... END blocks are tracked explicitly.
    """
    statements: list[str] = []
    buffer: list[str] = []
    in_trigger_body = False

    for raw_line in sql.splitlines():
        line = raw_line.split("--")[0].rstrip() if "--" in raw_line else raw_line.rstrip()
        if not line.strip():
            continue
        buffer.append(line)
        upper = line.strip().upper()

        if upper.startswith("CREATE TRIGGER"):
            in_trigger_body = True
            continue
        if in_trigger_body:
            if upper == "END;":
                statements.append("\n".join(buffer).strip())
                buffer = []
                in_trigger_body = False
            continue
        if line.rstrip().endswith(";"):
            statements.append("\n".join(buffer).strip())
            buffer = []

    if buffer:
        leftover = "\n".join(buffer).strip()
        if leftover:
            statements.append(leftover)
    return [s for s in statements if s]


class Database:
    """A thin, uniform wrapper over sqlite3 or libsql.

    Every method takes the same SQL text on both backends. Parameters are always
    positional '?' placeholders, which both backends accept.
    """

    def __init__(self, backend: str, conn: Any) -> None:
        self.backend = backend  # 'sqlite' | 'libsql'
        self._conn = conn

    # -- reads ---------------------------------------------------------------
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        """Run a SELECT and return a list of dicts (never raw tuples)."""
        if self.backend == "sqlite":
            cur = self._conn.execute(sql, tuple(params))
            cols = [d[0] for d in cur.description] if cur.description else []
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        rs = self._conn.execute(sql, list(params))
        cols = list(rs.columns)
        return [dict(zip(cols, list(row))) for row in rs.rows]

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = self.query_one(sql, params)
        if not row:
            return default
        return next(iter(row.values()), default)

    # -- writes --------------------------------------------------------------
    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        if self.backend == "sqlite":
            cur = self._conn.execute(sql, tuple(params))
            return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        rs = self._conn.execute(sql, list(params))
        return int(getattr(rs, "rows_affected", 0) or 0)

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> int:
        """Write many rows. Returns the number of rows submitted, not affected.

        libSQL has no executemany, so this batches. Batching matters for Turso:
        a per-row round trip over HTTP for 500 symbols is 500 requests.
        """
        materialised = [tuple(r) for r in rows]
        if not materialised:
            return 0
        if self.backend == "sqlite":
            self._conn.executemany(sql, materialised)
            return len(materialised)
        self._conn.batch([(sql, list(r)) for r in materialised])
        return len(materialised)

    def commit(self) -> None:
        """Commit on sqlite. ON TURSO THIS IS A NO-OP -- read rollback() below."""
        if self.backend == "sqlite":
            self._conn.commit()

    def rollback(self) -> None:
        """Roll back on sqlite. ON TURSO THIS CANNOT UNDO ANYTHING.

        The libSQL HTTP client autocommits every execute and every batch, so
        there is no open transaction for either method to act on. A pipeline
        that fails halfway therefore leaves the writes it already made
        COMMITTED on Turso, while the same failure on local sqlite rolls the
        whole run back. The two backends do not behave the same, and code that
        relies on rollback for correctness is wrong on one of them.

        This is deliberately not papered over with a fake transaction, because
        the real mitigation is already in place and is stronger: every writer
        goes through src/db/writes.py upsert(), which is keyed on
        (run_date, base_asset) or the table's natural key. Re-running a failed
        day overwrites the partial rows with complete ones rather than
        appending duplicates, so a half-finished run is recoverable by
        repeating it -- which is exactly what the external cron does.

        The ONE table this reasoning does not cover is journal_entry, which is
        append-only and defended by DB triggers rather than by upsert. Its
        entry_id is a deterministic hash of (run_date, asset, kind) and it is
        written with INSERT OR IGNORE, so a partial journal write also heals
        on re-run without ever mutating what was already recorded.

        If a future table is neither idempotent on re-run nor append-only,
        it must not depend on this method.
        """
        if self.backend == "sqlite":
            self._conn.rollback()
            return
        # Imported here rather than at module scope: logging_setup builds a
        # file handler on first use, and the DB layer must stay importable by
        # tooling that has not configured logging yet.
        from src.logging_setup import get_logger

        get_logger("db").warning(
            "rollback_unavailable",
            backend=self.backend,
            effect="libSQL autocommits; earlier writes in this run stay committed",
            mitigation="re-run the day -- every write is an idempotent upsert",
        )

    def close(self) -> None:
        self._conn.close()

    # -- schema --------------------------------------------------------------
    def apply_schema(self, schema_sql: str | None = None) -> int:
        """Apply schema.sql idempotently. Returns the statement count executed."""
        sql = schema_sql if schema_sql is not None else SCHEMA_PATH.read_text(encoding="utf-8")
        statements = _split_sql_statements(sql)
        executed = 0
        for stmt in statements:
            # PRAGMAs are a local-sqlite concern; libSQL manages its own storage.
            if stmt.strip().upper().startswith("PRAGMA") and self.backend != "sqlite":
                continue
            self.execute(stmt)
            executed += 1
        self.commit()
        return executed

    def table_names(self) -> list[str]:
        rows = self.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
        return [r["name"] for r in rows]


def _sqlite_path() -> Path:
    cfg = get_config()
    path = cfg.path(cfg.settings.database.local_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _open_sqlite() -> Database:
    conn = sqlite3.connect(str(_sqlite_path()), timeout=30.0)
    # WAL: a reader (the report job) must not block a writer (a collector).
    conn.execute("PRAGMA journal_mode=WAL")
    # Foreign keys are OFF by default in SQLite. forward_return -> journal_entry
    # is a real constraint and we want it enforced, not decorative.
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return Database("sqlite", conn)


def _open_libsql(url: str, auth_token: str | None) -> Database:
    try:
        import libsql_client
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise RuntimeError(
            "TURSO_DATABASE_URL is set but libsql-client is not installed. "
            "Run: pip install -r requirements.txt"
        ) from exc

    # libsql_client speaks https/wss; Turso hands out libsql:// URLs.
    normalised = re.sub(r"^libsql://", "https://", url.strip())
    client = libsql_client.create_client_sync(url=normalised, auth_token=auth_token)
    return Database("libsql", client)


def open_database() -> Database:
    """Open the database the environment says to use.

    Selection is by TURSO_DATABASE_URL alone (spec 15.1.2). Callers should
    prefer the `get_db()` context manager so the connection is always closed.
    """
    cfg = get_config()
    url = cfg.secrets.turso_database_url or os.getenv("TURSO_DATABASE_URL")
    if url:
        token = cfg.secrets.turso_auth_token or os.getenv("TURSO_AUTH_TOKEN")
        return _open_libsql(url, token)
    return _open_sqlite()


@contextmanager
def get_db() -> Iterator[Database]:
    """Context-managed connection. Commits on success, rolls back on error."""
    db = open_database()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> dict[str, Any]:
    """Create every table, index and trigger. Safe to run repeatedly."""
    with get_db() as db:
        statements = db.apply_schema()
        tables = db.table_names()
        return {
            "backend": db.backend,
            "statements_executed": statements,
            "tables": tables,
            "table_count": len(tables),
        }


__all__ = ["Database", "get_db", "open_database", "init_db", "SCHEMA_PATH"]
