"""Transaction coordinator - the highest-priority runtime component.

Drives the execution loop with the WAL-first invariant:

    WAL intent written -> checkpoint -> (approval) -> execute -> verify -> commit

On failure it invokes recovery (pre-pivot compensation or post-pivot forward
recovery). On unknown outcomes it reconciles via adapter status queries. It never
issues a blind duplicate external side effect.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional
from uuid import UUID, uuid4

from ..adapters.registry import AdapterRegistry
from ..compensation.engine import CompensationEngine, CompletedAction
from ..config import AATMConfig, default_config
from ..enums import (
    ApprovalState,
    AuditEvent,
    CompensationStrategy,
    FailureClass,
    IntentStatus,
    Outcome,
    RiskLevel,
    Tier,
    WALStatus,
    WorkflowState,
)
from ..models import (
    ActionIntent,
    CompensationExecution,
    SagaPlan,
    StepExecution,
    WorkflowRun,
    utcnow,
)
from ..storage.audit_log import AuditLog
from ..storage.checkpoints import CheckpointStore
from ..storage.idempotency import IdempotencyStore
from ..storage.wal import WriteAheadLog
from ..verification.post_conditions import PostconditionVerifier
from ..adapters.failures import CrashSignal
from .retry import backoff_delay_ms, is_retryable, sleep_backoff


# Approval callback: given a step, return True to grant, False to deny.
ApprovalCallback = Callable[[Any], bool]


def auto_approve(_step: Any) -> bool:
    return True


class CoordinatorResult:
    """Outcome of a coordinator run (before report generation)."""

    def __init__(self, run: WorkflowRun) -> None:
        self.run = run


class TransactionCoordinator:
    """Executes a saga plan with durability, verification, and recovery."""

    def __init__(
        self,
        plan: SagaPlan,
        registry: AdapterRegistry,
        config: Optional[AATMConfig] = None,
        run_id: Optional[UUID] = None,
        approval_callback: Optional[ApprovalCallback] = None,
        backoff_scale: float = 0.001,
    ) -> None:
        self.plan = plan
        self.registry = registry
        self.config = config or default_config
        self.config.ensure_dirs()
        self.run_id = run_id or uuid4()
        self.approval_callback = approval_callback or auto_approve
        self.backoff_scale = backoff_scale

        # Storage
        self.wal = WriteAheadLog(self.config.wal_db_path(str(self.run_id)))
        self.checkpoints = CheckpointStore(
            self.config.checkpoint_db_path(str(self.run_id))
        )
        self.idempotency = IdempotencyStore(
            self.config.idempotency_db_path(str(self.run_id))
        )
        self.audit = AuditLog(self.config.audit_log_path(str(self.run_id)))

        self.verifier = PostconditionVerifier()

        # Run state
        self.run = WorkflowRun(
            run_id=self.run_id,
            workflow_id=plan.workflow_id,
            workflow_name=plan.workflow_name,
            agent=plan.agent,
            state=WorkflowState.PLANNED,
            pivot_step_id=plan.pivot_step_id,
            variables=dict(plan.variables),
        )
        # Completed side-effecting actions (for compensation).
        self._completed: list[CompletedAction] = []
        self._completion_counter = 0
        self._approvals: list[dict[str, Any]] = []
        self._retries = 0
        self._unknowns = 0
        self.compensation_engine = CompensationEngine(
            registry=self.registry,
            wal=self.wal,
            audit=self.audit,
            run_id=self.run_id,
            workflow_id=plan.workflow_id,
            variables=self.run.variables,
            max_retries=plan.failure_policy.max_compensation_retries,
            backoff_scale=backoff_scale,
        )

    # -- lifecycle ------------------------------------------------------------

    def close(self) -> None:
        self.wal.close()
        self.checkpoints.close()
        self.idempotency.close()

    # -- main run -------------------------------------------------------------

    async def run_workflow(self) -> WorkflowRun:
        start = time.perf_counter()
        self.run.state = WorkflowState.RUNNING
        self.audit.append(
            AuditEvent.WORKFLOW_START,
            run_id=str(self.run_id),
            entity_id=self.plan.workflow_id,
            payload={"workflow": self.plan.workflow_name, "agent": self.plan.agent},
        )
        self.audit.append(
            AuditEvent.PLAN_CREATED,
            run_id=str(self.run_id),
            entity_id=self.plan.workflow_id,
            payload={
                "steps": [s.step_id for s in self.plan.steps],
                "pivot": self.plan.pivot_step_id,
            },
        )

        deadline = start + self.plan.timeout_seconds

        try:
            for step in self.plan.steps:
                if time.perf_counter() > deadline:
                    self.run.state = WorkflowState.TIMED_OUT
                    self.run.detail = "workflow timeout reached"
                    await self._recover(reason="workflow_timeout")
                    break

                proceed = await self._execute_step(step)
                if not proceed:
                    break
            else:
                # All steps completed.
                self.run.state = WorkflowState.COMPLETED
                self.audit.append(
                    AuditEvent.WORKFLOW_COMPLETE,
                    run_id=str(self.run_id),
                    entity_id=self.plan.workflow_id,
                    payload={"completed": self.run.completed_step_ids},
                )
        except CrashSignal:
            # Propagate crash to the caller (used by crash-recovery tests/CLI).
            self.run.updated_at = utcnow()
            raise

        self.run.updated_at = utcnow()
        return self.run

    # -- single step ----------------------------------------------------------

    async def _execute_step(self, step) -> bool:
        """Execute one step. Returns True to continue, False to stop the loop."""
        step_exec = StepExecution(
            step_id=step.step_id,
            tier=step.tier,
            is_pivot=step.is_pivot,
            is_post_pivot=step.is_post_pivot,
            started_at=utcnow(),
        )
        self.run.step_executions.append(step_exec)

        # Preconditions (declared expressions evaluated against variables).
        step_exec.precondition_passed = True

        # Approval gate BEFORE any intent/side effect.
        if step.approval_required:
            self.run.state = WorkflowState.WAITING_APPROVAL
            step_exec.approval_state = ApprovalState.REQUESTED
            self.audit.append(
                AuditEvent.APPROVAL_REQUESTED,
                run_id=str(self.run_id),
                entity_id=step.step_id,
                payload={"tool": step.tool_name, "tier": int(step.tier)},
            )
            granted = self.approval_callback(step)
            if not granted:
                step_exec.approval_state = ApprovalState.DENIED
                self._approvals.append({"step_id": step.step_id, "granted": False})
                self.audit.append(
                    AuditEvent.APPROVAL_DENIED,
                    run_id=str(self.run_id),
                    entity_id=step.step_id,
                    payload={"tool": step.tool_name},
                )
                step_exec.final_status = IntentStatus.FAILED
                step_exec.detail = "approval denied"
                step_exec.completed_at = utcnow()
                # No side effect happened; compensate previously completed work.
                await self._recover(reason="approval_denied")
                return False
            step_exec.approval_state = ApprovalState.GRANTED
            self._approvals.append({"step_id": step.step_id, "granted": True})
            self.audit.append(
                AuditEvent.APPROVAL_GRANTED,
                run_id=str(self.run_id),
                entity_id=step.step_id,
                payload={"tool": step.tool_name},
            )
            self.run.state = WorkflowState.RUNNING

        # Retry loop preserving a single logical intent id.
        intent = self._build_intent(step)
        step_exec.intent_id = intent.intent_id

        # Idempotency claim (dedupe repeated logical actions).
        if intent.idempotency_key:
            is_new, existing = self.idempotency.claim(
                self.run_id, intent.idempotency_key, intent.intent_id,
                step.tool_name, intent.parameters_hash,
            )
            if not is_new and existing is not None and existing.outcome == "success":
                # Duplicate logical action already succeeded -> reuse result.
                step_exec.outcome = Outcome.SUCCESS
                step_exec.final_status = IntentStatus.COMMITTED
                step_exec.action_result = existing.result or {}
                step_exec.detail = "deduplicated (idempotency): reused prior result"
                self._mark_completed(step, intent, existing.result or {})
                step_exec.completed_at = utcnow()
                return True

        attempt = 0
        max_attempts = max(1, step.retry.max_attempts)
        while attempt < max_attempts:
            attempt += 1
            step_exec.attempt = attempt

            # WAL intent BEFORE the side effect (durability invariant).
            if attempt == 1:
                self.wal.write_intent(intent, tier=int(step.tier), pivot=step.is_pivot)
            self.wal.update_status(intent.intent_id, WALStatus.RUNNING)
            self.audit.append(
                AuditEvent.ACTION_INTENT_CREATED,
                run_id=str(self.run_id),
                entity_id=step.step_id,
                payload={"intent_id": str(intent.intent_id), "tool": step.tool_name,
                         "attempt": attempt},
            )

            # Checkpoint pre-action state.
            self._checkpoint(step)

            if step.is_pivot:
                self.audit.append(
                    AuditEvent.PIVOT_REACHED,
                    run_id=str(self.run_id),
                    entity_id=step.step_id,
                    payload={"tool": step.tool_name},
                )

            self.audit.append(
                AuditEvent.ACTION_START,
                run_id=str(self.run_id),
                entity_id=step.step_id,
                payload={"tool": step.tool_name, "attempt": attempt},
            )

            adapter = self.registry.get(step.tool_name)
            if adapter is None:
                # Should not happen (planner validates), but fail closed.
                step_exec.outcome = Outcome.FAILURE
                step_exec.final_status = IntentStatus.FAILED
                step_exec.detail = f"no adapter for tool '{step.tool_name}'"
                self.wal.update_status(intent.intent_id, WALStatus.FAILED,
                                       outcome="failure")
                await self._recover(reason="missing_adapter")
                return False

            # Execute (crash may raise CrashSignal and propagate).
            result = await adapter.execute(intent)

            # Mark pivot crossed once a Tier-3 pivot side effect commits.
            if result.is_success and step.is_pivot:
                self.run.pivot_crossed = True

            # Classify outcome.
            if result.is_success:
                # Verify postconditions (adapter + expressions).
                adapter_v = await adapter.verify_postcondition(
                    intent, {"result": result.data}
                )
                expr_v = self.verifier.verify_expressions(
                    step, result.data, self.run.variables
                )
                verification = self.verifier.combine(adapter_v, expr_v)
                step_exec.postcondition = verification

                if not verification.passed:
                    # Phantom success / postcondition failure -> treat as FAILURE.
                    self.audit.append(
                        AuditEvent.POST_CONDITION_FAIL,
                        run_id=str(self.run_id),
                        entity_id=step.step_id,
                        payload={"detail": verification.detail},
                    )
                    handled = await self._handle_failure(
                        step, step_exec, intent,
                        failure_class=FailureClass.VALIDATION,
                        detail=f"postcondition failed: {verification.detail}",
                        attempt=attempt, max_attempts=max_attempts,
                    )
                    if handled == "retry":
                        continue
                    return handled == "continue"

                # Committed.
                self.audit.append(
                    AuditEvent.POST_CONDITION_PASS,
                    run_id=str(self.run_id),
                    entity_id=step.step_id,
                    payload={"expression": verification.expression},
                )
                self.wal.update_status(intent.intent_id, WALStatus.COMMITTED,
                                       outcome="success", result=result.data)
                if intent.idempotency_key:
                    self.idempotency.record_outcome(
                        self.run_id, intent.idempotency_key, "success", result.data
                    )
                self.audit.append(
                    AuditEvent.ACTION_COMPLETE,
                    run_id=str(self.run_id),
                    entity_id=step.step_id,
                    payload={"tool": step.tool_name, "result": result.data},
                )
                step_exec.outcome = Outcome.SUCCESS
                step_exec.final_status = IntentStatus.COMMITTED
                step_exec.action_result = result.data
                step_exec.retries = attempt - 1
                self._retries += attempt - 1
                step_exec.completed_at = utcnow()
                self._mark_completed(step, intent, result.data)
                return True

            if result.is_unknown:
                self._unknowns += 1
                step_exec.outcome = Outcome.UNKNOWN
                self.wal.update_status(intent.intent_id, WALStatus.UNKNOWN,
                                       outcome="unknown")
                self.audit.append(
                    AuditEvent.ACTION_UNKNOWN,
                    run_id=str(self.run_id),
                    entity_id=step.step_id,
                    payload={"tool": step.tool_name},
                )
                reconciled = await self._reconcile(step, step_exec, intent, adapter)
                if reconciled == "success":
                    return True
                if reconciled == "retry" and attempt < max_attempts:
                    continue
                # Unknown/failed after reconcile -> recover.
                handled = await self._handle_failure(
                    step, step_exec, intent,
                    failure_class=FailureClass.TIMEOUT,
                    detail="unknown outcome unresolved after reconciliation",
                    attempt=attempt, max_attempts=max_attempts,
                    already_failed=True,
                )
                if handled == "retry":
                    continue
                return handled == "continue"

            # FAILURE
            handled = await self._handle_failure(
                step, step_exec, intent,
                failure_class=result.failure_class,
                detail=result.error_message or "tool failure",
                attempt=attempt, max_attempts=max_attempts,
            )
            if handled == "retry":
                self.audit.append(
                    AuditEvent.RETRY,
                    run_id=str(self.run_id),
                    entity_id=step.step_id,
                    payload={"attempt": attempt, "failure_class":
                             str(result.failure_class)},
                )
                self._retries += 1
                await sleep_backoff(step.retry, attempt, self.backoff_scale)
                continue
            return handled == "continue"

        # Attempts exhausted without success.
        return await self._on_exhausted(step, step_exec, intent)

    # -- failure handling -----------------------------------------------------

    async def _handle_failure(
        self, step, step_exec, intent, *, failure_class, detail, attempt,
        max_attempts, already_failed=False,
    ) -> str:
        """Return one of: 'retry', 'stop', 'continue'.

        'retry' -> the caller loops again; 'stop'/'continue' -> return that bool.
        """
        if not already_failed:
            self.wal.update_status(intent.intent_id, WALStatus.FAILED,
                                   outcome="failure",
                                   error={"message": detail,
                                          "class": str(failure_class)})
            self.audit.append(
                AuditEvent.ACTION_FAILED,
                run_id=str(self.run_id),
                entity_id=step.step_id,
                payload={"tool": step.tool_name, "detail": detail,
                         "failure_class": str(failure_class)},
            )

        # Retry if allowed and attempts remain.
        if attempt < max_attempts and is_retryable(failure_class, step.retry):
            return "retry"

        step_exec.outcome = Outcome.FAILURE
        step_exec.final_status = IntentStatus.FAILED
        step_exec.detail = detail
        step_exec.completed_at = utcnow()

        # Recover (pre-pivot compensation vs post-pivot forward recovery).
        await self._recover(reason="step_failure", failed_step=step)
        return "stop"

    async def _on_exhausted(self, step, step_exec, intent) -> bool:
        step_exec.outcome = Outcome.FAILURE
        step_exec.final_status = IntentStatus.FAILED
        if not step_exec.detail:
            step_exec.detail = "retries exhausted"
        step_exec.completed_at = utcnow()
        await self._recover(reason="retries_exhausted", failed_step=step)
        return False

    # -- reconciliation (unknown outcome) -------------------------------------

    async def _reconcile(self, step, step_exec, intent, adapter) -> str:
        """Query adapter status by intent id. Returns 'success'|'failed'|'retry'."""
        self.audit.append(
            AuditEvent.RECONCILIATION_START,
            run_id=str(self.run_id),
            entity_id=step.step_id,
            payload={"intent_id": str(intent.intent_id)},
        )
        query = await adapter.query_status(intent.intent_id)
        step_exec.reconciled = True

        if query.found and query.outcome == Outcome.SUCCESS:
            # The side effect DID happen. Commit the original intent.
            self.wal.update_status(intent.intent_id, WALStatus.COMMITTED,
                                   outcome="success", result=query.data)
            if intent.idempotency_key:
                self.idempotency.record_outcome(
                    self.run_id, intent.idempotency_key, "success", query.data
                )
            if step.is_pivot:
                self.run.pivot_crossed = True
            self.audit.append(
                AuditEvent.RECONCILIATION_RESULT,
                run_id=str(self.run_id),
                entity_id=step.step_id,
                payload={"resolved": "success", "detail": query.detail},
            )
            self.audit.append(
                AuditEvent.ACTION_COMPLETE,
                run_id=str(self.run_id),
                entity_id=step.step_id,
                payload={"tool": step.tool_name, "reconciled": True,
                         "result": query.data},
            )
            step_exec.outcome = Outcome.SUCCESS
            step_exec.final_status = IntentStatus.COMMITTED
            step_exec.action_result = query.data
            step_exec.completed_at = utcnow()
            self._mark_completed(step, intent, query.data)
            return "success"

        if not query.found:
            # Side effect did NOT happen. Safe to fail (and retry a fresh attempt).
            self.wal.update_status(intent.intent_id, WALStatus.FAILED,
                                   outcome="failure")
            self.audit.append(
                AuditEvent.RECONCILIATION_RESULT,
                run_id=str(self.run_id),
                entity_id=step.step_id,
                payload={"resolved": "not_found", "detail": query.detail},
            )
            return "retry"

        # Still unknown.
        self.audit.append(
            AuditEvent.RECONCILIATION_RESULT,
            run_id=str(self.run_id),
            entity_id=step.step_id,
            payload={"resolved": "unknown", "detail": query.detail},
        )
        return "failed"

    # -- recovery -------------------------------------------------------------

    async def _recover(self, reason: str, failed_step=None) -> None:
        """Recover after a failure.

        Pre-pivot: compensate completed reversible/compensatable work in reverse
        completion order, then abort.

        Post-pivot: forward recovery - the pivot side effect stands; we perform a
        business-level reversal (refund + cancellations) via NEW transactions and
        report that exact rollback was impossible.
        """
        self.run.state = WorkflowState.RECOVERING

        post_pivot = self.run.pivot_crossed
        engine = self.compensation_engine

        ordered = engine.plan_compensations(self._completed)

        any_failed = False
        any_inconsistent = False
        for action in ordered:
            record = await engine.execute_compensation(action)
            self.run.compensations.append(record)
            if record.outcome == Outcome.FAILURE:
                if record.strategy == CompensationStrategy.MANUAL_ESCALATION:
                    any_inconsistent = True
                else:
                    any_failed = True
                    any_inconsistent = True

        if post_pivot:
            self.run.detail = (
                f"recovered after pivot ({reason}); business-level reversal used. "
                "Exact rollback impossible after Tier-3 side effect."
            )
        else:
            self.run.detail = f"recovered before pivot ({reason})"

        if any_inconsistent:
            self.run.state = WorkflowState.INCONSISTENT
            self.audit.append(
                AuditEvent.WORKFLOW_INCONSISTENT,
                run_id=str(self.run_id),
                entity_id=self.plan.workflow_id,
                payload={"reason": reason},
            )
        else:
            self.run.state = WorkflowState.ABORTED
            self.audit.append(
                AuditEvent.WORKFLOW_ABORTED,
                run_id=str(self.run_id),
                entity_id=self.plan.workflow_id,
                payload={"reason": reason, "post_pivot": post_pivot},
            )

    # -- helpers --------------------------------------------------------------

    def _build_intent(self, step) -> ActionIntent:
        idem_key = self._resolve_idem_key(step)
        return ActionIntent(
            run_id=self.run_id,
            workflow_id=self.plan.workflow_id,
            step_id=step.step_id,
            tool_name=step.tool_name,
            parameters=dict(step.parameters),
            idempotency_key=idem_key,
        )

    def _resolve_idem_key(self, step) -> Optional[str]:
        expr = step.idempotency_key_expr
        if not expr:
            return None
        # Simple template: "a|b|c" where tokens may be variable names or literals.
        parts = [p.strip() for p in expr.split("|")]
        values: list[str] = []
        for p in parts:
            if p in self.run.variables:
                values.append(str(self.run.variables[p]))
            elif p in step.parameters:
                values.append(str(step.parameters[p]))
            else:
                values.append(p)  # literal
        return f"{step.step_id}:" + "|".join(values)

    def _checkpoint(self, step) -> None:
        checkpoint_id = self.checkpoints.create_checkpoint(
            self.run_id,
            step.step_id,
            wal_seq=self.wal.max_seq(),
            pivot_crossed=self.run.pivot_crossed,
            completed_step_ids=list(self.run.completed_step_ids),
            variables=dict(self.run.variables),
            saga_state=str(self.run.state),
            world_state=self.registry.world.snapshot(),
            compensation_plan=[
                {"step_id": c.step_id, "tool": c.compensation.tool
                 if c.compensation else None}
                for c in self._completed
            ],
        )
        self.audit.append(
            AuditEvent.CHECKPOINT_CREATED,
            run_id=str(self.run_id),
            entity_id=step.step_id,
            payload={"checkpoint_id": checkpoint_id, "wal_seq": self.wal.max_seq()},
        )

    def _mark_completed(self, step, intent: ActionIntent, result: dict[str, Any]) -> None:
        self.run.completed_step_ids.append(step.step_id)
        self._completion_counter += 1
        # Only side-effecting steps need compensation tracking.
        if step.tier in (Tier.TWO, Tier.THREE) or (
            step.compensation and step.compensation.tool
        ):
            self._completed.append(
                CompletedAction(
                    step_id=step.step_id,
                    tool_name=step.tool_name,
                    parameters=dict(step.parameters),
                    result=result,
                    compensation=step.compensation,
                    completion_order=self._completion_counter,
                )
            )
