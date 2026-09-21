"""Deterministic replay + time-travel reconstruction from durable logs.

The audit log is an append-only, hash-chained event stream: an immutable record
of *everything the coordinator decided and did*, in order. Because it is
immutable and ordered, folding it back into state is a pure function - the same
log always reconstructs the same sequence of states. That is exactly what
deterministic replay needs.

:class:`ReplayEngine` reads a run's audit log (optionally enriched by the WAL for
final result data) and folds the events into a list of :class:`ReplayFrame`
snapshots - one per event. Each frame is the reconstructed state of the run
*as of* that audit ``seq``, so callers can "scrub" to any point in time:

    engine.replay(run_id)                 # full timeline
    engine.replay(run_id, to_seq=12)      # state as it was at audit seq 12

Replay is strictly READ-ONLY: it never calls an adapter, never mutates the world,
and never causes a side effect. It is a forensic / debugging / visualization tool,
not a re-execution. (Re-driving actual work is what ``resume`` and ``redrive`` do.)
"""

from __future__ import annotations

import copy
from typing import Any, Optional
from uuid import UUID

from .config import AATMConfig, default_config
from .enums import AuditEvent
from .storage.audit_log import AuditLog
from .storage.wal import WriteAheadLog

# Steps are shown in first-seen order; these are the per-step status values the
# fold assigns as it walks the event stream.
_STEP_PENDING = "pending"
_STEP_RUNNING = "running"
_STEP_SUCCESS = "success"
_STEP_FAILURE = "failure"
_STEP_UNKNOWN = "unknown"
_STEP_COMPENSATED = "compensated"
_STEP_SKIPPED = "skipped"


class ReplayFrame:
    """The reconstructed state of a run as of a single audit ``seq``."""

    def __init__(
        self,
        seq: int,
        timestamp: str,
        event: str,
        entity_id: str,
        note: str,
        workflow_state: str,
        pivot_crossed: bool,
        side_effects: int,
        steps: dict[str, dict[str, Any]],
    ) -> None:
        self.seq = seq
        self.timestamp = timestamp
        self.event = event
        self.entity_id = entity_id
        self.note = note
        self.workflow_state = workflow_state
        self.pivot_crossed = pivot_crossed
        self.side_effects = side_effects
        # Deep-copied at construction so later mutations of the running state do
        # not retroactively change this frame (frames are immutable snapshots).
        self.steps = steps

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "timestamp": self.timestamp,
            "event": self.event,
            "entity_id": self.entity_id,
            "note": self.note,
            "workflow_state": self.workflow_state,
            "pivot_crossed": self.pivot_crossed,
            "side_effects": self.side_effects,
            "steps": self.steps,
        }


class ReplayResult:
    """The full (or truncated) timeline plus the reconstructed end state."""

    def __init__(
        self,
        run_id: str,
        frames: list[ReplayFrame],
        chain_valid: bool,
        chain_detail: str,
        total_events: int,
        to_seq: Optional[int],
    ) -> None:
        self.run_id = run_id
        self.frames = frames
        self.chain_valid = chain_valid
        self.chain_detail = chain_detail
        self.total_events = total_events
        self.to_seq = to_seq

    @property
    def final_frame(self) -> Optional[ReplayFrame]:
        return self.frames[-1] if self.frames else None

    def at(self, seq: int) -> Optional[ReplayFrame]:
        """The frame in effect at ``seq`` (the last frame with ``frame.seq <= seq``)."""
        match: Optional[ReplayFrame] = None
        for f in self.frames:
            if f.seq <= seq:
                match = f
            else:
                break
        return match

    def to_dict(self) -> dict[str, Any]:
        ff = self.final_frame
        return {
            "run_id": self.run_id,
            "chain_valid": self.chain_valid,
            "chain_detail": self.chain_detail,
            "total_events": self.total_events,
            "to_seq": self.to_seq,
            "replayed_events": len(self.frames),
            "final_state": ff.workflow_state if ff else None,
            "frames": [f.to_dict() for f in self.frames],
        }


class ReplayEngine:
    """Folds a run's immutable audit event stream into a state timeline."""

    def __init__(self, config: Optional[AATMConfig] = None) -> None:
        self.config = config or default_config

    def replay(
        self, run_id: UUID | str, *, to_seq: Optional[int] = None
    ) -> ReplayResult:
        rid = str(run_id)
        audit = AuditLog(self.config.audit_log_path(rid),
                         secret_key=self.config.audit_hmac_key)
        verification = audit.verify()
        entries = audit.entries()

        # Enrich with the WAL so the final per-step result/tool is available even
        # if an event payload omitted it (read-only; never executed).
        wal_tool: dict[str, str] = {}
        try:
            wal = WriteAheadLog(self.config.wal_db_path(rid))
            try:
                for e in wal.entries_for_run(rid):
                    if not e.is_compensation:
                        wal_tool.setdefault(e.step_id, e.tool)
            finally:
                wal.close()
        except Exception:  # noqa: BLE001 - WAL is optional enrichment
            pass

        frames: list[ReplayFrame] = []
        state = {
            "workflow_state": "PLANNED",
            "pivot_crossed": False,
            "side_effects": 0,
            "steps": {},  # step_id -> {status, attempts, tool, detail, reconciled, approval}
        }

        for entry in entries:
            seq = int(entry.get("seq", 0))
            if to_seq is not None and seq > to_seq:
                break
            note = self._apply(state, entry, wal_tool)
            frames.append(ReplayFrame(
                seq=seq,
                timestamp=entry.get("timestamp", ""),
                event=entry.get("event", ""),
                entity_id=entry.get("entity_id", ""),
                note=note,
                workflow_state=state["workflow_state"],
                pivot_crossed=state["pivot_crossed"],
                side_effects=state["side_effects"],
                steps=copy.deepcopy(state["steps"]),
            ))

        return ReplayResult(
            run_id=rid,
            frames=frames,
            chain_valid=verification.valid,
            chain_detail=verification.detail,
            total_events=len(entries),
            to_seq=to_seq,
        )

    # -- fold -----------------------------------------------------------------

    def _step(self, state: dict[str, Any], step_id: str) -> dict[str, Any]:
        steps = state["steps"]
        if step_id not in steps:
            steps[step_id] = {
                "status": _STEP_PENDING,
                "attempts": 0,
                "tool": "",
                "detail": "",
                "reconciled": False,
                "approval": "",
                "is_compensation_target": False,
            }
        return steps[step_id]

    def _apply(
        self, state: dict[str, Any], entry: dict[str, Any],
        wal_tool: dict[str, str],
    ) -> str:
        """Apply one audit event to the running state; return a human note.

        This is a pure state transition: identical input always yields identical
        output, which is what makes the replay deterministic.
        """
        event = entry.get("event", "")
        entity = entry.get("entity_id", "")
        payload = entry.get("payload", {}) or {}

        if event == AuditEvent.WORKFLOW_START.value:
            state["workflow_state"] = "RUNNING"
            return "run started"
        if event == AuditEvent.PLAN_CREATED.value:
            return f"plan created ({payload.get('steps', '?')} steps)"
        if event == AuditEvent.ACTION_INTENT_CREATED.value:
            st = self._step(state, entity)
            st["tool"] = payload.get("tool") or st["tool"] or wal_tool.get(entity, "")
            return f"intent durable for {entity}"
        if event == AuditEvent.ACTION_START.value:
            st = self._step(state, entity)
            st["status"] = _STEP_RUNNING
            st["tool"] = payload.get("tool") or st["tool"] or wal_tool.get(entity, "")
            st["attempts"] = int(payload.get("attempt", st["attempts"] or 1))
            return f"{entity} -> {st['tool']} (attempt {st['attempts']})"
        if event == AuditEvent.RETRY.value:
            st = self._step(state, entity)
            st["attempts"] = int(payload.get("attempt", st["attempts"] + 1))
            return f"retry {entity} (attempt {st['attempts']})"
        if event == AuditEvent.ACTION_COMPLETE.value:
            st = self._step(state, entity)
            st["status"] = _STEP_SUCCESS
            state["side_effects"] += 1
            return f"{entity} committed"
        if event == AuditEvent.ACTION_FAILED.value:
            st = self._step(state, entity)
            st["status"] = _STEP_FAILURE
            st["detail"] = payload.get("detail", st["detail"])
            return f"{entity} failed: {st['detail']}"
        if event == AuditEvent.ACTION_UNKNOWN.value:
            st = self._step(state, entity)
            st["status"] = _STEP_UNKNOWN
            return f"{entity} outcome unknown"
        if event == AuditEvent.POST_CONDITION_FAIL.value:
            st = self._step(state, entity)
            st["detail"] = "postcondition failed"
            return f"{entity} postcondition failed"
        if event == AuditEvent.POST_CONDITION_PASS.value:
            return f"{entity} postcondition ok"
        if event in (AuditEvent.RECONCILIATION_START.value,):
            return f"reconciling {entity}"
        if event == AuditEvent.RECONCILIATION_RESULT.value:
            st = self._step(state, entity)
            st["reconciled"] = True
            outcome = payload.get("outcome") or payload.get("resolution", "")
            if outcome in ("committed", "success"):
                st["status"] = _STEP_SUCCESS
            elif outcome in ("failed", "failure"):
                st["status"] = _STEP_FAILURE
            return f"{entity} reconciled: {outcome or 'done'}"
        if event == AuditEvent.PIVOT_REACHED.value:
            state["pivot_crossed"] = True
            return f"PIVOT crossed at {entity}"
        if event == AuditEvent.APPROVAL_REQUESTED.value:
            self._step(state, entity)["approval"] = "requested"
            return f"approval requested for {entity}"
        if event == AuditEvent.APPROVAL_GRANTED.value:
            self._step(state, entity)["approval"] = "granted"
            return f"approval granted for {entity}"
        if event == AuditEvent.APPROVAL_DENIED.value:
            self._step(state, entity)["approval"] = "denied"
            return f"approval denied for {entity}"
        if event == AuditEvent.COMPENSATION_PLANNED.value:
            return "compensations planned"
        if event == AuditEvent.COMPENSATION_START.value:
            self._step(state, entity)["is_compensation_target"] = True
            return f"compensating {entity}"
        if event == AuditEvent.COMPENSATION_COMPLETE.value:
            st = self._step(state, entity)
            st["status"] = _STEP_COMPENSATED
            st["is_compensation_target"] = True
            state["side_effects"] += 1
            return f"{entity} compensated"
        if event == AuditEvent.COMPENSATION_FAILED.value:
            st = self._step(state, entity)
            st["detail"] = "compensation failed"
            st["is_compensation_target"] = True
            return f"{entity} compensation FAILED"
        if event == AuditEvent.ESCALATION.value:
            return f"escalated {entity} for manual handling"
        if event == AuditEvent.CRASH_RECOVERY_START.value:
            state["workflow_state"] = "RECOVERING"
            return "crash recovery started"
        if event == AuditEvent.CRASH_RECOVERY_COMPLETE.value:
            return "crash recovery complete"
        if event == AuditEvent.WORKFLOW_RESUMED.value:
            state["workflow_state"] = "RUNNING"
            return "workflow resumed (forward)"
        if event == AuditEvent.STEP_SKIPPED_RESUME.value:
            st = self._step(state, entity)
            if st["status"] not in (_STEP_SUCCESS, _STEP_COMPENSATED):
                st["status"] = _STEP_SUCCESS
            st["detail"] = "resumed (already committed)"
            return f"{entity} skipped on resume (already committed)"
        if event == AuditEvent.REDRIVE_START.value:
            return f"redrive started ({payload.get('open_entries', '?')} open)"
        if event == AuditEvent.REDRIVE_RESULT.value:
            st = self._step(state, entity)
            if payload.get("resolution") == "resolved":
                if st["is_compensation_target"]:
                    st["status"] = _STEP_COMPENSATED
                else:
                    st["status"] = _STEP_SUCCESS
                state["side_effects"] += 1
            return f"redrive {entity}: {payload.get('resolution', '?')}"
        if event == AuditEvent.WORKFLOW_COMPLETE.value:
            state["workflow_state"] = "COMPLETED"
            return "run COMPLETED"
        if event == AuditEvent.WORKFLOW_ABORTED.value:
            state["workflow_state"] = "ABORTED"
            return "run ABORTED"
        if event == AuditEvent.WORKFLOW_INCONSISTENT.value:
            state["workflow_state"] = "INCONSISTENT"
            return "run INCONSISTENT"
        if event in (AuditEvent.CIRCUIT_OPEN.value, AuditEvent.CIRCUIT_HALF_OPEN.value,
                     AuditEvent.CIRCUIT_CLOSED.value):
            return f"circuit breaker: {event.replace('CIRCUIT_', '').lower()}"
        if event == AuditEvent.CHECKPOINT_CREATED.value:
            return f"checkpoint @ {entity}"
        return event.lower().replace("_", " ")
