"""Crash recovery: WAL replay + status reconciliation on restart.

When a process crashes mid-run, durable state remains in the per-run WAL,
checkpoint store, and audit log. On restart, :class:`RecoveryManager` replays the
WAL, finds intents left in a non-terminal state (PENDING/RUNNING/UNKNOWN/
COMPENSATING), and reconciles each one by querying the adapter by ``intent_id``.

It NEVER re-issues a blind duplicate side effect: the decision to commit, fail,
or compensate is driven by the authoritative adapter status query.
"""

from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from ..adapters.registry import AdapterRegistry
from ..config import AATMConfig, default_config
from ..enums import AuditEvent, Outcome, WALStatus, WorkflowState
from ..storage.audit_log import AuditLog
from ..storage.checkpoints import CheckpointStore
from ..storage.wal import WALEntry, WriteAheadLog


class ReconciliationOutcome:
    def __init__(self, intent_id: str, step_id: str, tool: str,
                 resolution: str, detail: str) -> None:
        self.intent_id = intent_id
        self.step_id = step_id
        self.tool = tool
        self.resolution = resolution  # committed | failed | unknown | already_terminal
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "step_id": self.step_id,
            "tool": self.tool,
            "resolution": self.resolution,
            "detail": self.detail,
        }


class RecoveryReport:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.reconciliations: list[ReconciliationOutcome] = []
        self.pending_found = 0
        self.duplicates_prevented = 0
        self.final_state: Optional[WorkflowState] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "pending_found": self.pending_found,
            "duplicates_prevented": self.duplicates_prevented,
            "reconciliations": [r.to_dict() for r in self.reconciliations],
            "final_state": str(self.final_state) if self.final_state else None,
        }


class RecoveryManager:
    """Replays a crashed run's WAL and reconciles unresolved intents."""

    def __init__(
        self,
        run_id: UUID | str,
        registry: AdapterRegistry,
        config: Optional[AATMConfig] = None,
    ) -> None:
        self.run_id = str(run_id)
        self.registry = registry
        self.config = config or default_config
        self.wal = WriteAheadLog(self.config.wal_db_path(self.run_id))
        self.checkpoints = CheckpointStore(self.config.checkpoint_db_path(self.run_id))
        self.audit = AuditLog(self.config.audit_log_path(self.run_id),
                              secret_key=self.config.audit_hmac_key,
                              redact=self.config.redact_pii)

    def close(self) -> None:
        self.wal.close()
        self.checkpoints.close()

    def restore_world(self) -> None:
        """Restore the mock world from the latest checkpoint (for demo continuity).

        NOTE: This restores the *pre-action* snapshot. For Tier-3 intents that may
        have committed server-side, reconciliation (not this restore) is the
        authority; the adapter status query re-applies the discovered effect.
        """
        snap = self.checkpoints.latest_for_run(self.run_id)
        if snap is not None and snap.world_state:
            self.registry.world.restore(snap.world_state)

    async def recover(self) -> RecoveryReport:
        report = RecoveryReport(self.run_id)
        self.audit.append(
            AuditEvent.CRASH_RECOVERY_START,
            run_id=self.run_id,
            entity_id=self.run_id,
            payload={},
        )

        pending = self.wal.pending_entries(self.run_id)
        report.pending_found = len(pending)

        for entry in pending:
            outcome = await self._reconcile_entry(entry)
            report.reconciliations.append(outcome)
            if outcome.resolution == "committed" and entry.status in (
                WALStatus.PENDING.value,
                WALStatus.RUNNING.value,
                WALStatus.UNKNOWN.value,
            ):
                # We discovered an existing effect instead of re-issuing it.
                report.duplicates_prevented += 1

        self.audit.append(
            AuditEvent.CRASH_RECOVERY_COMPLETE,
            run_id=self.run_id,
            entity_id=self.run_id,
            payload=report.to_dict(),
        )
        return report

    async def _reconcile_entry(self, entry: WALEntry) -> ReconciliationOutcome:
        adapter = self.registry.get(entry.tool)
        if adapter is None:
            self.wal.update_status(entry.intent_id, WALStatus.ESCALATED,
                                   outcome="unknown")
            return ReconciliationOutcome(
                entry.intent_id, entry.step_id, entry.tool, "unknown",
                "no adapter registered for tool during recovery",
            )

        self.audit.append(
            AuditEvent.RECONCILIATION_START,
            run_id=self.run_id,
            entity_id=entry.step_id,
            payload={"intent_id": entry.intent_id, "tool": entry.tool},
        )

        query = await adapter.query_status(UUID(entry.intent_id))

        if query.found and query.outcome == Outcome.SUCCESS:
            # The side effect DID happen. Commit it; do NOT re-issue.
            self.wal.update_status(entry.intent_id, WALStatus.COMMITTED,
                                   outcome="success", result=query.data)
            self.audit.append(
                AuditEvent.RECONCILIATION_RESULT,
                run_id=self.run_id,
                entity_id=entry.step_id,
                payload={"resolved": "committed", "detail": query.detail},
            )
            return ReconciliationOutcome(
                entry.intent_id, entry.step_id, entry.tool, "committed",
                f"discovered existing side effect: {query.detail}",
            )

        # Not found -> the side effect did not happen. Mark FAILED (safe).
        self.wal.update_status(entry.intent_id, WALStatus.FAILED, outcome="failure")
        self.audit.append(
            AuditEvent.RECONCILIATION_RESULT,
            run_id=self.run_id,
            entity_id=entry.step_id,
            payload={"resolved": "failed", "detail": query.detail},
        )
        return ReconciliationOutcome(
            entry.intent_id, entry.step_id, entry.tool, "failed",
            f"no side effect found; safe to treat as not executed: {query.detail}",
        )
