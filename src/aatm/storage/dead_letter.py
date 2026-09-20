"""Dead-letter queue for compensations that could not be completed.

When a compensation exhausts its retries, cannot be reconciled, or requires
manual escalation, the run is left business-inconsistent. Rather than losing that
fact, we durably record it here so an operator can inspect and drain it later
(retry by hand, issue a manual refund, etc.).

The queue is an append-only JSONL file per run - the same local-first, no-server
approach used by the audit log.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


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
    ) -> None:
        self.run_id = run_id
        self.step_id = step_id
        self.tool = tool
        self.strategy = strategy
        self.reason = reason
        self.residual_risk = residual_risk
        self.intent_id = intent_id
        self.recorded_at = recorded_at or _utcnow()

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "step_id": self.step_id,
            "tool": self.tool,
            "strategy": self.strategy,
            "reason": self.reason,
            "residual_risk": self.residual_risk,
            "intent_id": self.intent_id,
            "recorded_at": self.recorded_at,
        }


class DeadLetterQueue:
    """Append-only, per-run store of unrecoverable compensations."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def append(self, entry: DeadLetterEntry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry.to_dict(), default=str) + "\n")

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

    def count(self) -> int:
        return len(self.entries())

    def is_empty(self) -> bool:
        return self.count() == 0
