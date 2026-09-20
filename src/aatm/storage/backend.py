"""Pluggable storage backend abstraction.

The stores (WAL, checkpoints, idempotency) are written against a small subset of
the DB-API: ``execute(sql, params)``, ``executescript(sql)``, ``fetchone`` /
``fetchall`` returning mapping-style rows, ``lastrowid`` for autoincrement
inserts, and ``close``. SQLite satisfies this natively; this module lets the same
stores run on another engine without touching store code.

Selection is via the ``AATM_DB_URL`` environment variable (or an explicit
:class:`StorageBackend` passed to :func:`set_backend`):

- unset, ``sqlite``, or a filesystem path  -> :class:`SQLiteBackend` (default).
- ``postgres://...`` / ``postgresql://...`` -> :class:`PostgresBackend`.

The default path is fully exercised by the test-suite. The Postgres backend is a
reference implementation: it adapts the SQLite-dialect SQL the stores emit
(``?`` placeholders, ``AUTOINCREMENT``, ``PRAGMA``) to Postgres via a thin shim,
and requires ``psycopg`` (v3). It is exercised only when a live Postgres is
configured; treat it as experimental until validated against your database.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

from .db import sqlite_connect as _sqlite_connect


@runtime_checkable
class Connection(Protocol):
    """Minimal connection surface the stores rely on."""

    def execute(self, sql: str, params: tuple = ()) -> Any: ...
    def executescript(self, sql: str) -> Any: ...
    def close(self) -> None: ...


class StorageBackend(Protocol):
    dialect: str

    def connect(self, db_path: Path | str) -> Connection: ...


# ---------------------------------------------------------------------------
# SQLite (default)
# ---------------------------------------------------------------------------


class SQLiteBackend:
    """Default backend: one SQLite file per store (the local-first default)."""

    dialect = "sqlite"

    def connect(self, db_path: Path | str) -> Connection:
        return _sqlite_connect(db_path)


# ---------------------------------------------------------------------------
# Postgres (reference / experimental)
# ---------------------------------------------------------------------------


def _sqlite_sql_to_pg(sql: str) -> str:
    """Best-effort translation of the SQLite-dialect SQL the stores emit."""
    # Autoincrement primary key -> SERIAL.
    sql = re.sub(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT",
                 "SERIAL PRIMARY KEY", sql, flags=re.IGNORECASE)
    # Positional placeholders: ? -> %s (params are still passed positionally).
    sql = sql.replace("?", "%s")
    return sql


class _PgCursor:
    """Wraps a psycopg cursor to expose the sqlite3-style surface used here."""

    def __init__(self, cur: Any, lastrowid: Optional[int]) -> None:
        self._cur = cur
        self.lastrowid = lastrowid

    def fetchone(self) -> Any:
        return self._cur.fetchone()

    def fetchall(self) -> Any:
        return self._cur.fetchall()


class PostgresConnection:
    """psycopg (v3) connection adapted to the store's expected interface."""

    def __init__(self, dsn: str) -> None:
        try:
            import psycopg  # type: ignore
            from psycopg.rows import dict_row  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "PostgresBackend requires the 'psycopg' package (v3). "
                "Install it with: pip install 'psycopg[binary]'"
            ) from exc
        # autocommit mirrors the SQLite stores' isolation_level=None behavior
        # (each statement is durable immediately), preserving WAL-before-effect.
        self._conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)

    def execute(self, sql: str, params: tuple = ()) -> _PgCursor:
        pg_sql = _sqlite_sql_to_pg(sql)
        # Emulate lastrowid for INSERTs into an autoincrement 'seq' column.
        wants_seq = (pg_sql.lstrip().upper().startswith("INSERT")
                     and "RETURNING" not in pg_sql.upper()
                     and re.search(r"INSERT\s+INTO\s+wal", pg_sql, re.IGNORECASE))
        if wants_seq:
            pg_sql = pg_sql.rstrip().rstrip(";") + " RETURNING seq"
        cur = self._conn.cursor()
        cur.execute(pg_sql, params)
        lastrowid: Optional[int] = None
        if wants_seq:
            row = cur.fetchone()
            if row is not None:
                lastrowid = int(row["seq"])
        return _PgCursor(cur, lastrowid)

    def executescript(self, sql: str) -> None:
        for stmt in sql.split(";"):
            s = stmt.strip()
            if not s or s.upper().startswith("PRAGMA"):
                continue
            self._conn.execute(_sqlite_sql_to_pg(s))

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001 - teardown best-effort
            pass


class PostgresBackend:
    """Reference Postgres backend (requires psycopg v3). Experimental."""

    dialect = "postgres"

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def connect(self, db_path: Path | str) -> Connection:
        # A single Postgres database holds all tables; the per-store file path is
        # ignored (tables are namespaced by their distinct names + run_id column).
        return PostgresConnection(self.dsn)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


_active: Optional[StorageBackend] = None


def backend_from_url(url: Optional[str]) -> StorageBackend:
    if not url or url == "sqlite" or not url.lower().startswith(("postgres://",
                                                                 "postgresql://")):
        return SQLiteBackend()
    return PostgresBackend(url)


def get_backend() -> StorageBackend:
    """Return the active backend, resolving ``AATM_DB_URL`` on first use."""
    global _active
    if _active is None:
        _active = backend_from_url(os.environ.get("AATM_DB_URL"))
    return _active


def set_backend(backend: Optional[StorageBackend]) -> None:
    """Override the active backend (mainly for tests / embedding)."""
    global _active
    _active = backend
