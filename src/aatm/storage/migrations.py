"""Schema/format versioning and migrations.

Two kinds of persisted state need version discipline as the project evolves:

1. **SQL stores** (WAL, checkpoints, idempotency). Each database records its
   schema version in a ``schema_meta`` table. On open, :func:`ensure_schema`
   compares the stored version to the code's current version and applies ordered
   migrations to close the gap. A database newer than the running code is a hard
   error (fail closed rather than silently mis-reading rows).

2. **File formats** (audit log, dead-letter queue, approvals, workflow YAML).
   These carry an integer format version; :func:`check_format_version` validates
   a document's version against what this build supports.

The initial release is version 1 for every component, so there are no migrations
yet - but the machinery is wired in so future format changes are safe and
explicit rather than ad hoc.
"""

from __future__ import annotations

from typing import Any, Callable

from ..models import utcnow

# Current versions per component. Bump these when a format changes and add a
# migration (for SQL stores) or a compatibility branch (for file formats).
SQL_SCHEMA_VERSIONS: dict[str, int] = {
    "wal": 1,
    "checkpoints": 1,
    "idempotency": 1,
}

FILE_FORMAT_VERSIONS: dict[str, int] = {
    "audit": 1,
    "dead_letter": 1,
    "approvals": 1,
    "workflow": 1,
}

# Migration registry: component -> list of (target_version, migrate_fn).
# migrate_fn receives the connection and upgrades the schema to target_version.
# Ordered ascending. Empty today; future entries look like:
#   ("wal", 2, _wal_v1_to_v2)
Migration = tuple[str, int, Callable[[Any], None]]
_MIGRATIONS: list[Migration] = []


class SchemaVersionError(RuntimeError):
    """Raised when a persisted store is newer than the running code."""


_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    component  TEXT PRIMARY KEY,
    version    INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def ensure_schema(conn: Any, component: str) -> int:
    """Ensure ``conn`` is at the current schema version for ``component``.

    Records the version for a fresh database, applies any pending migrations for
    an older one, and refuses to run against a database from a newer build.
    Returns the resulting version.
    """
    target = SQL_SCHEMA_VERSIONS.get(component)
    if target is None:
        raise ValueError(f"unknown SQL component '{component}'")

    conn.executescript(_META_SCHEMA)
    row = conn.execute(
        "SELECT version FROM schema_meta WHERE component = ?", (component,)
    ).fetchone()

    if row is None:
        # Fresh database: stamp the current version.
        conn.execute(
            "INSERT INTO schema_meta (component, version, updated_at) "
            "VALUES (?, ?, ?)",
            (component, target, utcnow().isoformat()),
        )
        return target

    current = int(row["version"])
    if current == target:
        return current
    if current > target:
        raise SchemaVersionError(
            f"{component} store is at schema v{current} but this build supports "
            f"v{target}. Upgrade the application (do not downgrade data)."
        )

    # current < target: apply ordered migrations.
    for comp, ver, fn in sorted(
        (m for m in _MIGRATIONS if m[0] == component), key=lambda m: m[1]
    ):
        if current < ver <= target:
            fn(conn)
            conn.execute(
                "UPDATE schema_meta SET version = ?, updated_at = ? "
                "WHERE component = ?",
                (ver, utcnow().isoformat(), component),
            )
            current = ver
    return current


def check_format_version(component: str, version: int) -> None:
    """Validate a file document's declared format version. Raises on mismatch."""
    supported = FILE_FORMAT_VERSIONS.get(component)
    if supported is None:
        raise ValueError(f"unknown file component '{component}'")
    if version > supported:
        raise SchemaVersionError(
            f"{component} document is format v{version} but this build supports "
            f"v{supported}. Upgrade the application."
        )
