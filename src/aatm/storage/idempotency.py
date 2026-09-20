"""Idempotency store backed by SQLite.

Every action invocation gets a stable ``intent_id``. Retries of the same logical
invocation reuse the same ``intent_id`` and the same ``idempotency_key``. This
store maps an idempotency key -> the intent that first claimed it, plus the last
known outcome, so the coordinator can:

- deduplicate a repeated logical action (return the prior result),
- avoid blindly re-issuing an external side effect after an unknown outcome.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from uuid import UUID

from ..models import canonical_json, utcnow
from .db import close_quiet, connect
from .migrations import ensure_schema

_SCHEMA = """
CREATE TABLE IF NOT EXISTS idempotency (
    idem_key    TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    intent_id   TEXT NOT NULL,
    tool        TEXT NOT NULL,
    params_hash TEXT NOT NULL,
    outcome     TEXT,
    result      TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (run_id, idem_key)
);
CREATE INDEX IF NOT EXISTS idx_idem_intent ON idempotency(intent_id);
"""


class IdempotencyRecord:
    __slots__ = (
        "idem_key",
        "run_id",
        "intent_id",
        "tool",
        "params_hash",
        "outcome",
        "result",
        "created_at",
        "updated_at",
    )

    def __init__(self, row: Any) -> None:
        import json

        self.idem_key: str = row["idem_key"]
        self.run_id: str = row["run_id"]
        self.intent_id: str = row["intent_id"]
        self.tool: str = row["tool"]
        self.params_hash: str = row["params_hash"]
        self.outcome: Optional[str] = row["outcome"]
        self.result: Optional[dict[str, Any]] = (
            json.loads(row["result"]) if row["result"] else None
        )
        self.created_at: str = row["created_at"]
        self.updated_at: str = row["updated_at"]


class IdempotencyStore:
    """Durable idempotency-key registry."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self._conn = connect(self.db_path)
        self._conn.executescript(_SCHEMA)
        ensure_schema(self._conn, "idempotency")

    def close(self) -> None:
        close_quiet(self._conn)

    def __enter__(self) -> "IdempotencyStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def claim(
        self,
        run_id: UUID | str,
        idem_key: str,
        intent_id: UUID | str,
        tool: str,
        params_hash: str,
    ) -> tuple[bool, Optional[IdempotencyRecord]]:
        """Try to claim an idempotency key.

        Returns ``(is_new, existing_record)``.

        - If the key is new, inserts the claim and returns ``(True, None)``.
        - If the key already exists, returns ``(False, existing)`` WITHOUT
          modifying the stored intent_id. The caller must reuse the existing
          intent rather than issuing a fresh side effect.
        """
        existing = self.get(run_id, idem_key)
        if existing is not None:
            return False, existing
        now = utcnow().isoformat()
        try:
            self._conn.execute(
                """
                INSERT INTO idempotency (
                    idem_key, run_id, intent_id, tool, params_hash,
                    outcome, result, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    idem_key,
                    str(run_id),
                    str(intent_id),
                    tool,
                    params_hash,
                    None,
                    None,
                    now,
                    now,
                ),
            )
        except Exception:
            # Race / duplicate insert: fall back to the existing record.
            existing = self.get(run_id, idem_key)
            return False, existing
        return True, None

    def record_outcome(
        self,
        run_id: UUID | str,
        idem_key: str,
        outcome: str,
        result: Optional[dict[str, Any]] = None,
    ) -> None:
        self._conn.execute(
            """
            UPDATE idempotency
               SET outcome = ?, result = ?, updated_at = ?
             WHERE run_id = ? AND idem_key = ?
            """,
            (
                outcome,
                canonical_json(result) if result is not None else None,
                utcnow().isoformat(),
                str(run_id),
                idem_key,
            ),
        )

    def get(self, run_id: UUID | str, idem_key: str) -> Optional[IdempotencyRecord]:
        row = self._conn.execute(
            "SELECT * FROM idempotency WHERE run_id = ? AND idem_key = ?",
            (str(run_id), idem_key),
        ).fetchone()
        return IdempotencyRecord(row) if row else None

    def get_by_intent(self, intent_id: UUID | str) -> Optional[IdempotencyRecord]:
        row = self._conn.execute(
            "SELECT * FROM idempotency WHERE intent_id = ? LIMIT 1", (str(intent_id),)
        ).fetchone()
        return IdempotencyRecord(row) if row else None
