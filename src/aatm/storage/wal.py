"""Write-Ahead Log (WAL) backed by SQLite.

Central invariant (spec 10.1):

    No side effect may execute before its intent is durable.

The coordinator must call :meth:`WriteAheadLog.write_intent` and have it return
(committing the row) BEFORE any adapter side effect. State transitions are then
recorded as the action progresses.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional
from uuid import UUID

from ..enums import WALStatus
from ..models import ActionIntent, canonical_json, utcnow
from .db import close_quiet, connect
from .migrations import ensure_schema

_SCHEMA = """
CREATE TABLE IF NOT EXISTS wal (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id      TEXT NOT NULL,
    run_id         TEXT NOT NULL,
    workflow_id    TEXT NOT NULL,
    step_id        TEXT NOT NULL,
    tool           TEXT NOT NULL,
    parameters     TEXT NOT NULL,
    parameters_hash TEXT NOT NULL,
    idempotency_key TEXT,
    is_compensation INTEGER NOT NULL DEFAULT 0,
    tier           INTEGER NOT NULL DEFAULT 0,
    pivot          INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL,
    outcome        TEXT,
    result         TEXT,
    error          TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wal_intent ON wal(intent_id);
CREATE INDEX IF NOT EXISTS idx_wal_status ON wal(status);
CREATE INDEX IF NOT EXISTS idx_wal_run ON wal(run_id);
"""

# States that indicate the record still needs reconciliation on restart.
_PENDING_STATES = (
    WALStatus.PENDING.value,
    WALStatus.RUNNING.value,
    WALStatus.UNKNOWN.value,
    WALStatus.COMPENSATING.value,
)


class WALEntry:
    """Lightweight row wrapper for a WAL record."""

    __slots__ = (
        "seq",
        "intent_id",
        "run_id",
        "workflow_id",
        "step_id",
        "tool",
        "parameters",
        "parameters_hash",
        "idempotency_key",
        "is_compensation",
        "tier",
        "pivot",
        "status",
        "outcome",
        "result",
        "error",
        "created_at",
        "updated_at",
    )

    def __init__(self, row: sqlite3.Row) -> None:
        import json

        self.seq: int = row["seq"]
        self.intent_id: str = row["intent_id"]
        self.run_id: str = row["run_id"]
        self.workflow_id: str = row["workflow_id"]
        self.step_id: str = row["step_id"]
        self.tool: str = row["tool"]
        self.parameters: dict[str, Any] = json.loads(row["parameters"])
        self.parameters_hash: str = row["parameters_hash"]
        self.idempotency_key: Optional[str] = row["idempotency_key"]
        self.is_compensation: bool = bool(row["is_compensation"])
        self.tier: int = row["tier"]
        self.pivot: bool = bool(row["pivot"])
        self.status: str = row["status"]
        self.outcome: Optional[str] = row["outcome"]
        self.result: Optional[dict[str, Any]] = (
            json.loads(row["result"]) if row["result"] else None
        )
        self.error: Optional[dict[str, Any]] = (
            json.loads(row["error"]) if row["error"] else None
        )
        self.created_at: str = row["created_at"]
        self.updated_at: str = row["updated_at"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "intent_id": self.intent_id,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "tool": self.tool,
            "parameters_hash": self.parameters_hash,
            "tier": self.tier,
            "pivot": self.pivot,
            "status": self.status,
            "outcome": self.outcome,
            "created_at": self.created_at,
        }

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"WALEntry(seq={self.seq}, step={self.step_id}, tool={self.tool}, "
            f"status={self.status}, outcome={self.outcome})"
        )


class WriteAheadLog:
    """Durable, ordered log of action intents and their lifecycle."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self._conn = connect(self.db_path)
        self._conn.executescript(_SCHEMA)
        ensure_schema(self._conn, "wal")

    # --- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        close_quiet(self._conn)

    def __enter__(self) -> "WriteAheadLog":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- writes --------------------------------------------------------------

    def write_intent(
        self,
        intent: ActionIntent,
        *,
        tier: int = 0,
        pivot: bool = False,
    ) -> int:
        """Durably persist a PENDING intent and return its sequence number.

        This MUST complete before any side effect for the intent. The row is
        committed synchronously (``synchronous=FULL``).
        """
        now = utcnow().isoformat()
        cur = self._conn.execute(
            """
            INSERT INTO wal (
                intent_id, run_id, workflow_id, step_id, tool, parameters,
                parameters_hash, idempotency_key, is_compensation, tier, pivot,
                status, outcome, result, error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(intent.intent_id),
                str(intent.run_id),
                intent.workflow_id,
                intent.step_id,
                intent.tool_name,
                canonical_json(intent.parameters),
                intent.parameters_hash,
                intent.idempotency_key,
                1 if intent.is_compensation else 0,
                int(tier),
                1 if pivot else 0,
                WALStatus.PENDING.value,
                None,
                None,
                None,
                now,
                now,
            ),
        )
        seq = int(cur.lastrowid)
        return seq

    def update_status(
        self,
        intent_id: UUID | str,
        status: WALStatus,
        *,
        outcome: Optional[str] = None,
        result: Optional[dict[str, Any]] = None,
        error: Optional[dict[str, Any]] = None,
    ) -> None:
        """Transition a WAL record to a new state, optionally recording data."""
        now = utcnow().isoformat()
        self._conn.execute(
            """
            UPDATE wal
               SET status = ?,
                   outcome = COALESCE(?, outcome),
                   result = COALESCE(?, result),
                   error = COALESCE(?, error),
                   updated_at = ?
             WHERE intent_id = ?
            """,
            (
                status.value,
                outcome,
                canonical_json(result) if result is not None else None,
                canonical_json(error) if error is not None else None,
                now,
                str(intent_id),
            ),
        )

    # --- reads ---------------------------------------------------------------

    def get(self, intent_id: UUID | str) -> Optional[WALEntry]:
        row = self._conn.execute(
            "SELECT * FROM wal WHERE intent_id = ? ORDER BY seq DESC LIMIT 1",
            (str(intent_id),),
        ).fetchone()
        return WALEntry(row) if row else None

    def get_by_seq(self, seq: int) -> Optional[WALEntry]:
        row = self._conn.execute("SELECT * FROM wal WHERE seq = ?", (seq,)).fetchone()
        return WALEntry(row) if row else None

    def all_entries(self) -> list[WALEntry]:
        rows = self._conn.execute("SELECT * FROM wal ORDER BY seq ASC").fetchall()
        return [WALEntry(r) for r in rows]

    def entries_for_run(self, run_id: UUID | str) -> list[WALEntry]:
        rows = self._conn.execute(
            "SELECT * FROM wal WHERE run_id = ? ORDER BY seq ASC", (str(run_id),)
        ).fetchall()
        return [WALEntry(r) for r in rows]

    def pending_entries(self, run_id: Optional[UUID | str] = None) -> list[WALEntry]:
        """Return entries that still require reconciliation on restart.

        These are PENDING / RUNNING / UNKNOWN / COMPENSATING records.
        """
        placeholders = ",".join("?" for _ in _PENDING_STATES)
        if run_id is not None:
            rows = self._conn.execute(
                f"SELECT * FROM wal WHERE run_id = ? AND status IN ({placeholders}) "
                "ORDER BY seq ASC",
                (str(run_id), *_PENDING_STATES),
            ).fetchall()
        else:
            rows = self._conn.execute(
                f"SELECT * FROM wal WHERE status IN ({placeholders}) ORDER BY seq ASC",
                _PENDING_STATES,
            ).fetchall()
        return [WALEntry(r) for r in rows]

    def find_by_idempotency_key(
        self, run_id: UUID | str, idempotency_key: str
    ) -> Optional[WALEntry]:
        row = self._conn.execute(
            """
            SELECT * FROM wal
             WHERE run_id = ? AND idempotency_key = ?
             ORDER BY seq ASC LIMIT 1
            """,
            (str(run_id), idempotency_key),
        ).fetchone()
        return WALEntry(row) if row else None

    def max_seq(self) -> int:
        row = self._conn.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM wal").fetchone()
        return int(row["m"])
