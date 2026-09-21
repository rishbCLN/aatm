"""High-level engine facade.

Ties the planner, coordinator, recovery manager, and reporting together behind a
small API used by the CLI and the demo. Keeps per-run wiring (config, injector,
approval policy) in one place.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import UUID, uuid4

import yaml

from .adapters.registry import AdapterRegistry
from .adapters.failures import CrashSignal, FailureInjector
from .config import AATMConfig, default_config
from .enums import AuditEvent, WALStatus
from .models import ActionIntent, SagaPlan, WorkflowRun
from .planner import SagaPlanner, WorkflowParser
from .reporting.evidence_report import EvidenceReport
from .runtime import RecoveryManager, TransactionCoordinator
from .storage.audit_log import AuditLog
from .storage.dead_letter import DeadLetterQueue
from .storage.wal import WriteAheadLog


class EngineError(Exception):
    """User-facing engine error (mapped to a non-zero CLI exit)."""


class PlanResult:
    def __init__(self, plan: SagaPlan) -> None:
        self.plan = plan


class RunOutput:
    def __init__(
        self,
        run: WorkflowRun,
        plan: SagaPlan,
        registry: AdapterRegistry,
        duration_ms: float,
        crashed: bool = False,
        metrics: Optional[dict[str, Any]] = None,
    ) -> None:
        self.run = run
        self.plan = plan
        self.registry = registry
        self.duration_ms = duration_ms
        self.crashed = crashed
        self.metrics = metrics or {}


class RedriveAttempt:
    def __init__(self, step_id: str, tool: Optional[str], resolution: str,
                 detail: str) -> None:
        self.step_id = step_id
        self.tool = tool
        self.resolution = resolution  # resolved | failed
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        return {"step_id": self.step_id, "tool": self.tool,
                "resolution": self.resolution, "detail": self.detail}


class RedriveReport:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.attempts: list[RedriveAttempt] = []
        self.open_before = 0
        self.resolved = 0
        self.still_failed = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "open_before": self.open_before,
            "resolved": self.resolved,
            "still_failed": self.still_failed,
            "attempts": [a.to_dict() for a in self.attempts],
        }


class AATMEngine:
    def __init__(self, config: Optional[AATMConfig] = None) -> None:
        self.config = config or default_config
        self.config.ensure_dirs()

    # -- planning -------------------------------------------------------------

    def plan(self, workflow_path: str | Path) -> SagaPlan:
        parser = WorkflowParser(self.config)
        parsed = parser.parse(workflow_path)  # raises WorkflowParseError
        planner = SagaPlanner(self.config)
        return planner.plan(parsed)

    # -- injection loading ----------------------------------------------------

    def load_injector(self, injection_path: Optional[str | Path]) -> FailureInjector:
        if injection_path is None:
            return FailureInjector(enabled=False)
        p = Path(injection_path)
        if not p.exists():
            raise EngineError(f"injection file not found: {p}")
        with p.open("r", encoding="utf-8") as fh:
            spec = yaml.safe_load(fh) or {}
        return FailureInjector.from_spec(spec)

    # -- running --------------------------------------------------------------

    async def run(
        self,
        workflow_path: str | Path,
        *,
        injection_path: Optional[str | Path] = None,
        run_id: Optional[UUID] = None,
        approval_callback: Optional[Callable[[Any], bool]] = None,
        backoff_scale: float = 0.001,
        timeout_scale: float = 1.0,
    ) -> RunOutput:
        plan = self.plan(workflow_path)
        if not plan.is_valid:
            raise EngineError(
                "plan validation failed:\n  - " + "\n  - ".join(plan.critical_issues)
            )
        injector = self.load_injector(injection_path)
        rid = run_id or uuid4()
        registry = AdapterRegistry(
            injector=injector,
            world_persist_path=self.config.world_state_path(str(rid)),
        )
        coord = TransactionCoordinator(
            plan, registry, config=self.config, run_id=rid,
            approval_callback=approval_callback, backoff_scale=backoff_scale,
            timeout_scale=timeout_scale,
        )
        start = time.perf_counter()
        crashed = False
        try:
            run = await coord.run_workflow()
        except CrashSignal:
            crashed = True
            run = coord.run
        finally:
            duration_ms = (time.perf_counter() - start) * 1000.0
            metrics = coord.metrics.snapshot()
            coord.close()
        return RunOutput(run, plan, registry, duration_ms, crashed=crashed,
                         metrics=metrics)

    # -- recovery -------------------------------------------------------------

    async def recover(
        self,
        run_id: UUID | str,
        *,
        registry: Optional[AdapterRegistry] = None,
    ) -> tuple[Any, AdapterRegistry]:
        # Fresh registry that loads the authoritative external state persisted by
        # the crashed process (models the external systems' own durability).
        reg = registry or AdapterRegistry(
            world_persist_path=self.config.world_state_path(str(run_id))
        )
        manager = RecoveryManager(run_id, reg, config=self.config)
        try:
            # NOTE: do NOT restore the mock world from the pre-action checkpoint
            # here. The persisted world file models the EXTERNAL systems' own
            # durable state and is authoritative for reconciliation. Restoring the
            # stale checkpoint snapshot would hide a side effect that actually
            # committed server-side (e.g. a captured payment).
            report = await manager.recover()
        finally:
            manager.close()
        return report, reg

    # -- resume-forward -------------------------------------------------------

    async def resume(
        self,
        workflow_path: str | Path,
        run_id: UUID | str,
        *,
        approval_callback: Optional[Callable[[Any], bool]] = None,
        backoff_scale: float = 0.001,
        timeout_scale: float = 1.0,
    ) -> tuple[Any, RunOutput]:
        """Reconcile a crashed run, then drive the REMAINING steps forward.

        Re-plans deterministically from the same workflow file (the planner is
        pure, so the plan is identical), reconciles any non-terminal WAL intents,
        then resumes forward under the SAME run_id, skipping already-committed
        steps. Returns ``(recovery_report, run_output)``.
        """
        plan = self.plan(workflow_path)
        if not plan.is_valid:
            raise EngineError(
                "plan validation failed:\n  - " + "\n  - ".join(plan.critical_issues)
            )
        # 1) Reconcile pending intents against authoritative external state.
        report, reg = await self.recover(run_id)
        # 2) Resume forward under the same run_id, reusing the loaded world.
        coord = TransactionCoordinator(
            plan, reg, config=self.config, run_id=UUID(str(run_id)),
            approval_callback=approval_callback, backoff_scale=backoff_scale,
            timeout_scale=timeout_scale,
        )
        start = time.perf_counter()
        crashed = False
        try:
            run = await coord.resume_workflow()
        except CrashSignal:
            crashed = True
            run = coord.run
        finally:
            duration_ms = (time.perf_counter() - start) * 1000.0
            metrics = coord.metrics.snapshot()
            coord.close()
        output = RunOutput(run, plan, reg, duration_ms, crashed=crashed,
                           metrics=metrics)
        return report, output

    # -- dead-letter redrive --------------------------------------------------

    async def redrive(
        self,
        run_id: UUID | str,
        *,
        registry: Optional[AdapterRegistry] = None,
    ) -> RedriveReport:
        """Re-attempt the OPEN dead-letter entries for a run.

        For each unresolved entry we re-execute its action against the
        authoritative (persisted) world. Re-execution is dedup-safe: a
        compensation entry carries the STABLE compensation ``intent_id``, so if
        the effect actually succeeded server-side before, the adapter returns the
        recorded result instead of duplicating it. On success the entry is
        resolved (append-only marker); on failure it stays open for a later retry.
        """
        rid = str(run_id)
        report = RedriveReport(rid)
        reg = registry or AdapterRegistry(
            world_persist_path=self.config.world_state_path(rid)
        )
        wal = WriteAheadLog(self.config.wal_db_path(rid))
        audit = AuditLog(self.config.audit_log_path(rid),
                         secret_key=self.config.audit_hmac_key,
                         redact=self.config.redact_pii)
        dlq = DeadLetterQueue(self.config.dead_letter_path(rid))

        try:
            open_entries = dlq.open_entries()
            report.open_before = len(open_entries)
            audit.append(
                AuditEvent.REDRIVE_START,
                run_id=rid,
                entity_id=rid,
                payload={"open_entries": report.open_before},
            )

            for entry in open_entries:
                attempt = await self._redrive_one(entry, reg, wal, rid)
                report.attempts.append(attempt)
                if attempt.resolution == "resolved":
                    report.resolved += 1
                    dlq.resolve(
                        DeadLetterQueue._entry_key(entry),
                        "resolved",
                        detail=attempt.detail,
                        resolved_by="redrive",
                    )
                else:
                    report.still_failed += 1
                audit.append(
                    AuditEvent.REDRIVE_RESULT,
                    run_id=rid,
                    entity_id=entry.get("step_id", "?"),
                    payload=attempt.to_dict(),
                )
        finally:
            wal.close()
        return report

    async def _redrive_one(
        self, entry: dict[str, Any], reg: AdapterRegistry,
        wal: WriteAheadLog, run_id: str,
    ) -> "RedriveAttempt":
        """Re-execute a single dead-letter entry's action. Best-effort, dedup-safe."""
        step_id = entry.get("step_id", "?")
        intent_id = entry.get("intent_id")
        tool = entry.get("tool")
        params = entry.get("params")
        workflow_id = ""

        # Prefer the durable WAL row (authoritative tool + bound parameters).
        wal_entry = wal.get(intent_id) if intent_id else None
        if wal_entry is not None:
            tool = wal_entry.tool
            params = wal_entry.parameters
            workflow_id = wal_entry.workflow_id
            is_comp = wal_entry.is_compensation
            reuse_intent_id = intent_id
        else:
            # Forward-retry escalation carries no comp intent and no WAL comp row:
            # it is the ORIGINAL forward step that must be driven forward again (a
            # retryable Tier-2 action), NOT a compensation. Reconstruct from the
            # entry's captured params.
            is_comp = False
            reuse_intent_id = None

        if not tool:
            return RedriveAttempt(step_id, tool, "failed",
                                  "no tool recorded; cannot redrive")
        adapter = reg.get(tool)
        if adapter is None:
            return RedriveAttempt(step_id, tool, "failed",
                                  f"no adapter for tool '{tool}'")

        kwargs: dict[str, Any] = dict(
            run_id=UUID(run_id),
            workflow_id=workflow_id,
            step_id=step_id,
            tool_name=tool,
            parameters=params or {},
            is_compensation=is_comp,
        )
        if reuse_intent_id:
            kwargs["intent_id"] = UUID(reuse_intent_id)
        intent = ActionIntent(**kwargs)

        try:
            result = await adapter.execute(intent)
        except Exception as exc:  # noqa: BLE001 - convert to a failed attempt
            return RedriveAttempt(step_id, tool, "failed",
                                  f"redrive raised: {exc}")

        if result.is_success:
            if reuse_intent_id:
                status = (WALStatus.COMPENSATED if is_comp
                          else WALStatus.COMMITTED)
                wal.update_status(intent.intent_id, status,
                                  outcome="success", result=result.data)
            return RedriveAttempt(step_id, tool, "resolved",
                                  "re-executed successfully")
        detail = result.error_message or "redrive did not succeed"
        return RedriveAttempt(step_id, tool, "failed", detail)

    # -- reporting ------------------------------------------------------------

    def report(
        self,
        output: RunOutput,
        *,
        experiment: Optional[dict[str, Any]] = None,
        retries: int = 0,
        unknown_outcomes: int = 0,
        approvals: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        reporter = EvidenceReport(self.config)
        # Prefer counters captured by the coordinator's metrics when available.
        counters = (output.metrics or {}).get("counters", {})
        return reporter.generate(
            output.run,
            plan=output.plan,
            world_summary=output.registry.world.summary(),
            duration_ms=output.duration_ms,
            retries=retries or int(counters.get("retries", 0)),
            unknown_outcomes=unknown_outcomes or int(counters.get("unknown_outcomes", 0)),
            approvals=approvals,
            experiment=experiment,
            metrics=output.metrics,
        )
