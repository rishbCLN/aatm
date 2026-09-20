"""Checkpoint store backed by SQLite.

Before each side-effecting tool call the coordinator persists a snapshot of the
recovery-relevant state (spec section 11):

- completed step IDs
- workflow variables
- current saga state
- pivot state
- known tool outcomes
- world-state snapshot for mock tools
- current WAL sequence
- active compensation plan

Checkpoint writes are atomic (single INSERT, committed synchronously). For Tier-3
actions a checkpoint does NOT make the action reversible; it only captures the
pre-action state for recovery logic and evidence.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from uuid import UUID, uuid4

from ..models import canonical_json, utcnow
from .db import close_quiet, connect
from .migrations import ensure_schema

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL,
    step_id       TEXT NOT NULL,
    wal_seq       INTEGER NOT NULL DEFAULT 0,
    pivot_crossed INTEGER NOT NULL DEFAULT 0,
    snapshot      TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ckpt_run ON checkpoints(run_id);
CREATE INDEX IF NOT EXISTS idx_ckpt_step ON checkpoints(run_id, step_id);
"""


class Snapshot:
    """Deserialized checkpoint snapshot."""

    __slots__ = (
        "checkpoint_id",
        "run_id",
        "step_id",
        "wal_seq",
        "pivot_crossed",
        "completed_step_ids",
        "variables",
        "saga_state",
        "known_outcomes",
        "world_state",
        "compensation_plan",
        "created_at",
    )

    def __init__(self, row: Any) -> None:
        data = json.loads(row["snapshot"])
        self.checkpoint_id: str = row["checkpoint_id"]
        self.run_id: str = row["run_id"]
        self.step_id: str = row["step_id"]
        self.wal_seq: int = row["wal_seq"]
        self.pivot_crossed: bool = bool(row["pivot_crossed"])
        self.completed_step_ids: list[str] = data.get("completed_step_ids", [])
        self.variables: dict[str, Any] = data.get("variables", {})
        self.saga_state: str = data.get("saga_state", "")
        self.known_outcomes: dict[str, Any] = data.get("known_outcomes", {})
        self.world_state: dict[str, Any] = data.get("world_state", {})
        self.compensation_plan: list[dict[str, Any]] = data.get("compensation_plan", [])
        self.created_at: str = row["created_at"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "wal_seq": self.wal_seq,
            "pivot_crossed": self.pivot_crossed,
            "completed_step_ids": self.completed_step_ids,
            "variables": self.variables,
            "saga_state": self.saga_state,
            "known_outcomes": self.known_outcomes,
            "world_state": self.world_state,
            "compensation_plan": self.compensation_plan,
            "created_at": self.created_at,
        }


class CheckpointStore:
    """Atomic, append-only checkpoint persistence."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self._conn = connect(self.db_path)
        self._conn.executescript(_SCHEMA)
        ensure_schema(self._conn, "checkpoints")

    def close(self) -> None:
        close_quiet(self._conn)

    def __enter__(self) -> "CheckpointStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def create_checkpoint(
        self,
        run_id: UUID | str,
        step_id: str,
        *,
        wal_seq: int = 0,
        pivot_crossed: bool = False,
        completed_step_ids: Optional[list[str]] = None,
        variables: Optional[dict[str, Any]] = None,
        saga_state: str = "",
        known_outcomes: Optional[dict[str, Any]] = None,
        world_state: Optional[dict[str, Any]] = None,
        compensation_plan: Optional[list[dict[str, Any]]] = None,
    ) -> str:
        """Persist a checkpoint atomically and return its id."""
        checkpoint_id = str(uuid4())
        snapshot = {
            "completed_step_ids": completed_step_ids or [],
            "variables": variables or {},
            "saga_state": saga_state,
            "known_outcomes": known_outcomes or {},
            "world_state": world_state or {},
            "compensation_plan": compensation_plan or [],
        }
        self._conn.execute(
            """
            INSERT INTO checkpoints (
                checkpoint_id, run_id, step_id, wal_seq, pivot_crossed,
                snapshot, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                checkpoint_id,
                str(run_id),
                step_id,
                int(wal_seq),
                1 if pivot_crossed else 0,
                canonical_json(snapshot),
                utcnow().isoformat(),
            ),
        )
        return checkpoint_id

    def load_checkpoint(self, checkpoint_id: str) -> Optional[Snapshot]:
        row = self._conn.execute(
            "SELECT * FROM checkpoints WHERE checkpoint_id = ?", (checkpoint_id,)
        ).fetchone()
        return Snapshot(row) if row else None

    def latest_for_run(self, run_id: UUID | str) -> Optional[Snapshot]:
        row = self._conn.execute(
            "SELECT * FROM checkpoints WHERE run_id = ? ORDER BY created_at DESC, "
            "rowid DESC LIMIT 1",
            (str(run_id),),
        ).fetchone()
        return Snapshot(row) if row else None

    def all_for_run(self, run_id: UUID | str) -> list[Snapshot]:
        rows = self._conn.execute(
            "SELECT * FROM checkpoints WHERE run_id = ? ORDER BY rowid ASC",
            (str(run_id),),
        ).fetchall()
        return [Snapshot(r) for r in rows]

    def restore_checkpoint(self, checkpoint_id: str) -> Optional[Snapshot]:
        """Return the snapshot for a checkpoint (state restoration is caller-driven).

        Restoration semantics are applied by the coordinator/adapters. For Tier-1
        (fully reversible) steps the world_state snapshot is authoritative; for
        higher tiers this snapshot is evidence of pre-action state only.
        """
        return self.load_checkpoint(checkpoint_id)
