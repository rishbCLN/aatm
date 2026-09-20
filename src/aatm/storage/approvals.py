"""Durable approval store for human-in-the-loop gates.

The coordinator already pauses at Tier-3 (irreversible) steps for approval. This
module makes that gate **durable** and **out-of-band**:

- every approval request is persisted (so a crash/restart still knows a decision
  was pending),
- a decision can be supplied ahead of time or after the fact by an operator via
  the ``aatm approve`` / ``aatm deny`` CLI commands (which write here),
- requests carry a deadline; if it passes with no decision the fail-safe is to
  DENY (never auto-approve an irreversible action).

State is an append-only JSONL file per run - consistent with the audit log and
dead-letter queue. The latest record for a given step wins.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ApprovalStore:
    """Append-only per-run store of approval requests and decisions."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    # -- writes ---------------------------------------------------------------

    def _append(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

    def request(self, step_id: str, tool: str, timeout_s: float) -> dict[str, Any]:
        now = _utcnow()
        rec = {
            "kind": "request",
            "step_id": step_id,
            "tool": tool,
            "requested_at": now.isoformat(),
            "deadline": (now + timedelta(seconds=timeout_s)).isoformat(),
        }
        self._append(rec)
        return rec

    def decide(self, step_id: str, granted: bool, *, decided_by: str = "operator",
               reason: str = "") -> dict[str, Any]:
        rec = {
            "kind": "decision",
            "step_id": step_id,
            "granted": bool(granted),
            "decided_by": decided_by,
            "reason": reason,
            "decided_at": _utcnow().isoformat(),
        }
        self._append(rec)
        return rec

    # -- reads ----------------------------------------------------------------

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def latest_decision(self, step_id: str) -> Optional[dict[str, Any]]:
        """Return the most recent decision for a step, if any."""
        found: Optional[dict[str, Any]] = None
        for rec in self.entries():
            if rec.get("kind") == "decision" and rec.get("step_id") == step_id:
                found = rec
        return found

    def pending(self) -> list[dict[str, Any]]:
        """Requests that have no matching decision yet."""
        requested = [r for r in self.entries() if r.get("kind") == "request"]
        decided = {r["step_id"] for r in self.entries()
                   if r.get("kind") == "decision"}
        return [r for r in requested if r["step_id"] not in decided]

    @staticmethod
    def is_expired(request_rec: dict[str, Any], now: Optional[datetime] = None) -> bool:
        now = now or _utcnow()
        try:
            deadline = datetime.fromisoformat(request_rec["deadline"])
        except (KeyError, ValueError):
            return False
        return now > deadline
