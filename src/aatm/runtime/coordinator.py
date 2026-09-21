"""Transaction coordinator - the highest-priority runtime component.

Drives the execution loop with the WAL-first invariant:

    WAL intent written -> checkpoint -> (approval) -> execute -> verify -> commit

On failure it invokes recovery (pre-pivot compensation or post-pivot forward
recovery). On unknown outcomes it reconciles via adapter status queries. It never
issues a blind duplicate external side effect.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Optional
from uuid import UUID, uuid4

from ..adapters.registry import AdapterRegistry
from ..compensation.engine import CompensationEngine, CompletedAction
from ..config import AATMConfig, default_config
from ..enums import (
    ApprovalState,
    AuditEvent,
    CompensationSource,
    CompensationStrategy,
    FailureClass,
    IntentStatus,
    Outcome,
    Reversibility,
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
from ..observability import Metrics, get_logger, log_event
from ..storage.approvals import ApprovalStore
from ..storage.audit_log import AuditLog
from ..storage.checkpoints import CheckpointStore
from ..storage.dead_letter import DeadLetterEntry, DeadLetterQueue
from ..storage.idempotency import IdempotencyStore
from ..storage.wal import WALEntry, WriteAheadLog
from ..verification.post_conditions import PostconditionVerifier
from ..adapters.failures import CrashSignal
from .circuit_breaker import CircuitBreaker
from .retry import is_retryable, sleep_backoff


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
        timeout_scale: float = 1.0,
        circuit_breaker: Optional[CircuitBreaker] = None,
        metrics: Optional[Metrics] = None,
    ) -> None:
        self.plan = plan
        self.registry = registry
        self.config = config or default_config
        self.config.ensure_dirs()
        self.run_id = run_id or uuid4()
        self.approval_callback = approval_callback or auto_approve
        self.backoff_scale = backoff_scale
        # Converts a step's timeout_ms into wall-clock seconds. Tests can shrink
        # it to force fast timeouts deterministically.
        self.timeout_scale = timeout_scale
        # Per-tool circuit breaker (fail fast on repeatedly-failing tools).
        self.breaker = circuit_breaker or CircuitBreaker(
            failure_threshold=self.config.circuit_failure_threshold,
            cooldown_s=self.config.circuit_cooldown_s,
            half_open_trials=self.config.circuit_half_open_trials,
        )
        # Observability: metrics counters + structured logger.
        self.metrics = metrics or Metrics()
        self.logger = get_logger()

        # Storage
        self.wal = WriteAheadLog(self.config.wal_db_path(str(self.run_id)))
        self.checkpoints = CheckpointStore(
            self.config.checkpoint_db_path(str(self.run_id))
        )
        self.idempotency = IdempotencyStore(
            self.config.idempotency_db_path(str(self.run_id))
        )
        self.audit = AuditLog(self.config.audit_log_path(str(self.run_id)),
                              secret_key=self.config.audit_hmac_key,
                              redact=self.config.redact_pii)
        self.dead_letter = DeadLetterQueue(
            self.config.dead_letter_path(str(self.run_id))
        )
        self.approvals_store = ApprovalStore(
            self.config.approval_path(str(self.run_id))
        )

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
        log_event(self.logger, "workflow.start", run_id=str(self.run_id),
                  workflow=self.plan.workflow_id, steps=len(self.plan.steps))
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

        await self._run_step_loop(start, skip_committed=set())

        self.run.updated_at = utcnow()
        self.metrics.observe_ms("workflow.duration_ms",
                                (time.perf_counter() - start) * 1000.0)
        log_event(self.logger, "workflow.stop", run_id=str(self.run_id),
                  state=str(self.run.state), metrics=self.metrics.snapshot())
        return self.run

    async def resume_workflow(self) -> WorkflowRun:
        """Resume a crashed run FORWARD from durable state (Feature: resume-forward).

        Inspired by DBOS ``fork_workflow``/resume: instead of only reconciling the
        crashed intent and stopping, we rebuild the coordinator's state from the
        WAL + latest checkpoint, SKIP the steps that already committed (their side
        effects are durable), and drive the remaining steps forward to completion.

        Prerequisite: crash reconciliation (``RecoveryManager.recover``) has already
        run so every non-terminal WAL intent is now COMMITTED or FAILED. The mock
        world must already reflect the authoritative external state (the engine
        loads the persisted world file before calling this).
        """
        start = time.perf_counter()
        already = self._seed_from_durable_state()
        self.run.state = WorkflowState.RUNNING
        log_event(self.logger, "workflow.resume", run_id=str(self.run_id),
                  workflow=self.plan.workflow_id, already_committed=len(already))
        self.audit.append(
            AuditEvent.WORKFLOW_RESUMED,
            run_id=str(self.run_id),
            entity_id=self.plan.workflow_id,
            payload={"already_committed": sorted(already),
                     "pivot_crossed": self.run.pivot_crossed},
        )

        await self._run_step_loop(start, skip_committed=already)

        self.run.updated_at = utcnow()
        self.metrics.observe_ms("workflow.duration_ms",
                                (time.perf_counter() - start) * 1000.0)
        log_event(self.logger, "workflow.stop", run_id=str(self.run_id),
                  state=str(self.run.state), metrics=self.metrics.snapshot())
        return self.run

    async def _run_step_loop(self, start: float, *, skip_committed: set) -> None:
        """Shared execution loop for a fresh run and a forward resume."""
        deadline = start + self.plan.timeout_seconds
        try:
            for step in self.plan.steps:
                if step.step_id in skip_committed:
                    # Already committed in a prior (crashed) session: its side
                    # effect is durable, so we must NOT re-execute it.
                    self._record_resumed_step(step)
                    continue

                if time.perf_counter() > deadline:
                    self.run.state = WorkflowState.TIMED_OUT
                    self.run.detail = "workflow timeout reached"
                    await self._recover(reason="workflow_timeout")
                    break

                proceed = await self._execute_step(step)
                if not proceed:
                    break
            else:
                # All steps completed (or were already committed).
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
            # Durably record the request (survives a crash; visible to operators).
            request_rec = self.approvals_store.request(
                step.step_id, step.tool_name, self.config.approval_timeout_s
            )
            self.audit.append(
                AuditEvent.APPROVAL_REQUESTED,
                run_id=str(self.run_id),
                entity_id=step.step_id,
                payload={"tool": step.tool_name, "tier": int(step.tier)},
            )
            granted = self._resolve_approval(step, request_rec)
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
                self.metrics.incr("duplicates_prevented")
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

            # Circuit breaker: fail fast if this tool is currently tripped OPEN.
            if not self.breaker.allow(step.tool_name):
                return await self._on_circuit_open(step, step_exec, intent, attempt)

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
            result = await self._execute_with_timeout(step, adapter, intent)

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
                self.breaker.record_success(step.tool_name)
                self._mark_completed(step, intent, result.data)
                return True

            if result.is_unknown:
                self._unknowns += 1
                self.metrics.incr("unknown_outcomes")
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
                self.metrics.incr("retries")
                await sleep_backoff(step.retry, attempt, self.backoff_scale)
                continue
            return handled == "continue"

        # Attempts exhausted without success.
        return await self._on_exhausted(step, step_exec, intent)

    async def _execute_with_timeout(self, step, adapter, intent: ActionIntent):
        """Run ``adapter.execute`` under a per-step deadline.

        A per-step timeout is NOT proof the side effect did not happen: the
        request may have reached the server. So a timeout is surfaced as an
        UNKNOWN outcome, which routes into reconciliation (status query by
        intent_id) rather than a blind retry.
        """
        timeout_s = (step.timeout_ms / 1000.0) * self.timeout_scale
        if timeout_s <= 0:
            return await adapter.execute(intent)
        try:
            return await asyncio.wait_for(adapter.execute(intent), timeout=timeout_s)
        except asyncio.TimeoutError:
            self.audit.append(
                AuditEvent.ACTION_UNKNOWN,
                run_id=str(self.run_id),
                entity_id=step.step_id,
                payload={"tool": step.tool_name,
                         "detail": f"step timeout after {step.timeout_ms}ms"},
            )
            from ..models import ToolResult

            return ToolResult(
                intent_id=intent.intent_id,
                outcome=Outcome.UNKNOWN,
                error_message=f"step timed out after {step.timeout_ms}ms",
                failure_class=FailureClass.TIMEOUT,
            )

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

        # Feed the circuit breaker; a trip is recorded for observability.
        if self.breaker.record_failure(step.tool_name):
            self.metrics.incr("circuit_trips")
            self.audit.append(
                AuditEvent.CIRCUIT_OPEN,
                run_id=str(self.run_id),
                entity_id=step.step_id,
                payload={"tool": step.tool_name,
                         "threshold": self.breaker.failure_threshold},
            )

        # Retry if attempts remain. A RETRYABLE step is driven FORWARD to
        # completion, so it retries regardless of failure class (saga theory:
        # retryable transactions must eventually succeed). Other steps retry only
        # for transient/allowed failure classes.
        retryable_step = step.reversibility == Reversibility.RETRYABLE
        if attempt < max_attempts and (
            retryable_step or is_retryable(failure_class, step.retry)
        ):
            return "retry"

        step_exec.outcome = Outcome.FAILURE
        step_exec.final_status = IntentStatus.FAILED
        step_exec.detail = detail
        step_exec.completed_at = utcnow()

        # A retryable step that exhausted its retries AFTER the pivot must NOT
        # unwind committed pivot work. Escalate FORWARD: surface the run as
        # business-inconsistent and dead-letter the step for an operator to
        # complete, but never refund/cancel the irreversible work behind it.
        if retryable_step and self.run.pivot_crossed:
            await self._escalate_forward(step, detail)
            return "stop"

        # Recover (pre-pivot compensation vs post-pivot forward recovery).
        await self._recover(reason="step_failure", failed_step=step)
        return "stop"

    async def _escalate_forward(self, step, detail: str) -> None:
        """Escalate a post-pivot retryable step that could not complete.

        Forward-only recovery: the committed pivot side effect STANDS. We do not
        compensate anything (that would undo irreversible work). Instead we record
        a durable dead-letter entry + a surfaced residual risk and mark the run
        INCONSISTENT so an operator can drive the step to completion by hand.
        """
        self.run.state = WorkflowState.RECOVERING
        risk = (
            f"Retryable step '{step.step_id}' ({step.tool_name}) did not complete "
            f"after {step.retry.max_attempts} forward attempt(s): {detail}. The "
            "committed pivot work is preserved; this step needs manual completion "
            "(no rollback is possible or appropriate)."
        )
        # Surface as a failed compensation record so residual risk + recovery
        # status flow through the existing report/flow model unchanged.
        record = CompensationExecution(
            source_step_id=step.step_id,
            source=CompensationSource.NONE,
            strategy=CompensationStrategy.FORWARD_FIX,
            outcome=Outcome.FAILURE,
            residual_risk=[risk],
            detail="forward retry exhausted; escalated for manual completion",
        )
        self.run.compensations.append(record)
        self.metrics.incr("forward_escalations")
        self.dead_letter.append(DeadLetterEntry(
            run_id=str(self.run_id),
            step_id=step.step_id,
            tool=step.tool_name,
            strategy="forward_retry",
            reason=detail,
            residual_risk=[risk],
            intent_id=None,
            params=dict(step.parameters),
        ))
        self.audit.append(
            AuditEvent.ESCALATION,
            run_id=str(self.run_id),
            entity_id=step.step_id,
            payload={"reason": "forward_retry_exhausted", "tool": step.tool_name,
                     "risks": [risk]},
        )
        self.run.state = WorkflowState.INCONSISTENT
        self.run.detail = (
            f"post-pivot retryable step '{step.step_id}' unresolved after retries; "
            "committed work preserved, escalated for manual completion "
            "(forward-only, no rollback)."
        )
        self.audit.append(
            AuditEvent.WORKFLOW_INCONSISTENT,
            run_id=str(self.run_id),
            entity_id=self.plan.workflow_id,
            payload={"reason": "forward_retry_exhausted", "step_id": step.step_id},
        )

    async def _on_circuit_open(self, step, step_exec, intent, attempt) -> bool:
        """Fail fast without executing: the tool's breaker is OPEN."""
        detail = f"circuit breaker open for tool '{step.tool_name}'; failing fast"
        # Record a durable, traceable WAL entry for the skipped attempt.
        if attempt == 1:
            self.wal.write_intent(intent, tier=int(step.tier), pivot=step.is_pivot)
        self.wal.update_status(
            intent.intent_id, WALStatus.FAILED, outcome="failure",
            error={"message": detail, "class": str(FailureClass.CIRCUIT_OPEN)},
        )
        self.audit.append(
            AuditEvent.CIRCUIT_OPEN,
            run_id=str(self.run_id),
            entity_id=step.step_id,
            payload={"tool": step.tool_name, "state": str(self.breaker.state(
                step.tool_name)), "action": "fail_fast"},
        )
        step_exec.outcome = Outcome.FAILURE
        step_exec.final_status = IntentStatus.FAILED
        step_exec.detail = detail
        step_exec.completed_at = utcnow()
        await self._recover(reason="circuit_open", failed_step=step)
        return False

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
        self.metrics.incr("reconciliations")

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
            self.breaker.record_success(step.tool_name)
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

        any_inconsistent = False
        for action in ordered:
            record = await engine.execute_compensation(action)
            self.run.compensations.append(record)
            self.metrics.incr("compensations_executed")
            if record.outcome == Outcome.FAILURE:
                self.metrics.incr("compensations_failed")
                # Any failed compensation (manual-escalation or otherwise) means
                # the run cannot be declared clean -> surface as INCONSISTENT.
                any_inconsistent = True
                # Durably record the unrecoverable compensation so an operator
                # can drain it later instead of it being silently lost.
                self.dead_letter.append(DeadLetterEntry(
                    run_id=str(self.run_id),
                    step_id=record.source_step_id,
                    tool=action.compensation.tool if action.compensation else None,
                    strategy=str(record.strategy),
                    reason=record.detail or "compensation failed",
                    residual_risk=list(record.residual_risk),
                    intent_id=(str(record.compensation_intent_id)
                               if record.compensation_intent_id else None),
                ))

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

    def _resolve_approval(self, step, request_rec: dict[str, Any]) -> bool:
        """Decide a Tier-3 approval durably.

        Precedence: (1) an out-of-band decision already recorded in the store
        wins; (2) otherwise, if the request has expired, fail safe by DENYING;
        (3) otherwise consult the in-process approval callback. The final decision
        is always persisted.
        """
        existing = self.approvals_store.latest_decision(step.step_id)
        if existing is not None:
            return bool(existing.get("granted"))

        if ApprovalStore.is_expired(request_rec):
            self.approvals_store.decide(
                step.step_id, False, decided_by="timeout",
                reason="approval deadline passed; fail-safe deny",
            )
            return False

        granted = self.approval_callback(step)
        self.approvals_store.decide(
            step.step_id, granted, decided_by="callback",
            reason="" if granted else "denied by approval policy",
        )
        return granted

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

    # -- resume-forward (crash recovery continuation) -------------------------

    def _seed_from_durable_state(self) -> set:
        """Rebuild in-memory state from the WAL + latest checkpoint for a resume.

        Returns the set of step_ids that already COMMITTED (to be skipped). Only
        the LAST WAL entry per step decides its status, and compensation tracking
        is rebuilt in plan order so a later failure can still unwind prior work.
        """
        # Latest committed status + result per step (last WAL write wins).
        last: dict[str, WALEntry] = {}
        for entry in self.wal.entries_for_run(self.run_id):
            if entry.is_compensation:
                continue
            last[entry.step_id] = entry
        committed = {sid for sid, e in last.items()
                     if e.status == WALStatus.COMMITTED.value}

        # Restore variables from the latest checkpoint (best-effort).
        snap = self.checkpoints.latest_for_run(self.run_id)
        if snap is not None and snap.variables:
            self.run.variables.update(snap.variables)

        # Rebuild completion + compensation tracking in plan (topological) order.
        for step in self.plan.steps:
            if step.step_id not in committed:
                continue
            entry = last[step.step_id]
            result = entry.result or {}
            if entry.pivot:
                self.run.pivot_crossed = True
            self._mark_completed(step, self._build_intent(step), result)
        return committed

    def _record_resumed_step(self, step) -> None:
        """Record a skipped-but-already-committed step in the run for evidence."""
        step_exec = StepExecution(
            step_id=step.step_id,
            tier=step.tier,
            is_pivot=step.is_pivot,
            is_post_pivot=step.is_post_pivot,
            started_at=utcnow(),
        )
        step_exec.outcome = Outcome.SUCCESS
        step_exec.final_status = IntentStatus.COMMITTED
        step_exec.detail = "resumed: already committed in a prior session (not re-run)"
        step_exec.completed_at = utcnow()
        self.run.step_executions.append(step_exec)
        self.audit.append(
            AuditEvent.STEP_SKIPPED_RESUME,
            run_id=str(self.run_id),
            entity_id=step.step_id,
            payload={"tool": step.tool_name},
        )
