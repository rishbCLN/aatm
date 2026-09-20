"""SQLite helpers shared by the storage layer.

Uses parameterized queries everywhere (no string interpolation of values) and
enables WAL journaling for durability. Connections are created per-store and are
safe for the single-process demo engine.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


def sqlite_connect(db_path: Path | str) -> sqlite3.Connection:
    """Open a SQLite connection with sane durability defaults.

    - ``journal_mode=WAL`` for durability + concurrent reads.
    - ``synchronous=FULL`` so committed rows survive a process crash.
    - ``foreign_keys=ON``.
    - ``row_factory=sqlite3.Row`` for dict-like access.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=FULL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def connect(db_path: Path | str) -> Any:
    """Open a connection using the active storage backend.

    Defaults to SQLite (see :func:`sqlite_connect`). Delegates to the configured
    backend (e.g. Postgres) when ``AATM_DB_URL`` selects one. Imported lazily to
    avoid a circular import with the backend module.
    """
    from .backend import get_backend

    return get_backend().connect(db_path)


def close_quiet(conn: sqlite3.Connection | None) -> None:
    """Close a connection, swallowing errors (used in teardown paths)."""
    if conn is None:
        return
    try:
        conn.close()
    except sqlite3.Error:
        pass
