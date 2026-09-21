"""Dead-letter queue for compensations that could not be completed.

When a compensation exhausts its retries, cannot be reconciled, or requires
manual escalation, the run is left business-inconsistent. Rather than losing that
fact, we durably record it here so an operator can inspect and drain it later
(retry by hand, issue a manual refund, etc.).

The queue is an append-only JSONL file per run - the same local-first, no-server
approach used by the audit log. Resolution never mutates or deletes a prior
record: it appends a ``_kind="resolution"`` marker that references the entry's
stable ``entry_id``. The open work-list is derived by subtracting resolved
entries from all entries, so the full history (and audit trail) is preserved.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class DeadLetterEntry:
    """A single unrecoverable compensation needing manual intervention."""

    def __init__(
        self,
        run_id: str,
        step_id: str,
        tool: Optional[str],
        strategy: str,
        reason: str,
        residual_risk: list[str],
        intent_id: Optional[str] = None,
        recorded_at: Optional[str] = None,
        entry_id: Optional[str] = None,
        params: Optional[dict[str, Any]] = None,
    ) -> None:
        self.run_id = run_id
        self.step_id = step_id
        self.tool = tool
        self.strategy = strategy
        self.reason = reason
        self.residual_risk = residual_risk
        self.intent_id = intent_id
        self.recorded_at = recorded_at or _utcnow()
        # Stable identity so a resolution marker can reference this exact entry.
        self.entry_id = entry_id or str(uuid4())
        # Optional captured parameters to aid a redrive when the WAL row is not
        # enough on its own (best-effort; may be None for legacy entries).
        self.params = params

    def to_dict(self) -> dict[str, Any]:
        return {
            "_kind": "entry",
            "entry_id": self.entry_id,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "tool": self.tool,
            "strategy": self.strategy,
            "reason": self.reason,
            "residual_risk": self.residual_risk,
            "intent_id": self.intent_id,
            "params": self.params,
            "recorded_at": self.recorded_at,
        }


class DeadLetterQueue:
    """Append-only, per-run store of unrecoverable compensations.

    Two record kinds share the JSONL file:

    - ``_kind="entry"``      : a dead-lettered item (default for legacy rows).
    - ``_kind="resolution"`` : marks a prior entry (by ``entry_id``) resolved.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    # -- low-level io ---------------------------------------------------------

    def _write(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

    def _records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    @staticmethod
    def _entry_key(rec: dict[str, Any]) -> str:
        """Stable key for an entry, tolerant of legacy rows without entry_id."""
        if rec.get("entry_id"):
            return str(rec["entry_id"])
        # Legacy fallback: compose a key from identifying fields.
        return "|".join(
            str(rec.get(k, "")) for k in ("step_id", "intent_id", "recorded_at")
        )

    # -- writing --------------------------------------------------------------

    def append(self, entry: DeadLetterEntry) -> None:
        self._write(entry.to_dict())

    def resolve(
        self,
        entry_key: str,
        resolution: str = "resolved",
        *,
        detail: str = "",
        resolved_by: str = "operator",
    ) -> None:
        """Append a resolution marker for a prior entry (never mutates history)."""
        self._write({
            "_kind": "resolution",
            "entry_key": entry_key,
            "resolution": resolution,  # resolved | failed | dismissed
            "detail": detail,
            "resolved_by": resolved_by,
            "recorded_at": _utcnow(),
        })

    # -- reading --------------------------------------------------------------

    def entries(self) -> list[dict[str, Any]]:
        """All dead-letter entries (legacy rows without ``_kind`` count as entries)."""
        return [r for r in self._records() if r.get("_kind", "entry") == "entry"]

    def resolutions(self) -> list[dict[str, Any]]:
        return [r for r in self._records() if r.get("_kind") == "resolution"]

    def resolved_keys(self) -> set[str]:
        return {str(r.get("entry_key")) for r in self.resolutions()
                if r.get("resolution") in (None, "resolved", "dismissed")}

    def open_entries(self) -> list[dict[str, Any]]:
        """Entries that have not yet been resolved/dismissed - the work list."""
        resolved = self.resolved_keys()
        return [e for e in self.entries() if self._entry_key(e) not in resolved]

    def count(self) -> int:
        """Total number of dead-letter entries ever recorded (excludes markers)."""
        return len(self.entries())

    def open_count(self) -> int:
        return len(self.open_entries())

    def is_empty(self) -> bool:
        return self.count() == 0
