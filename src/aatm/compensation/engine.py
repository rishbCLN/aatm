"""Compensation engine: plan + execute compensations.

Planning (spec 17.1): for every completed side-effecting step before failure,
resolve a compensation and enqueue it in reverse **completion order**.

Execution (spec 17.2): for each compensation, create a compensation intent id,
write the WAL, execute via the adapter, verify the postcondition, mark
compensated, and append an audit event.

Compensation failure (spec 17.3): retry per policy, reconcile unknowns; if still
unresolved, enter INCONSISTENT and escalate. Never recursively "compensate the
compensation" in v1.
"""

from __future__ import annotations

from typing import Any, Optional
from uuid import UUID, uuid4

from ..adapters.registry import AdapterRegistry
from ..enums import (
    ApprovalState,
    AuditEvent,
    CompensationSource,
    CompensationStrategy,
    IntentStatus,
    Outcome,
    WALStatus,
)
from ..models import (
    ActionIntent,
    CompensationExecution,
    CompensationPlan,
    StepExecution,
    VerificationResult,
)
from ..storage.audit_log import AuditLog
from ..storage.wal import WriteAheadLog
from .validator import CompensationValidator


class CompletedAction:
    """A completed step's execution data needed to build its compensation."""

    def __init__(
        self,
        step_id: str,
        tool_name: str,
        parameters: dict[str, Any],
        result: dict[str, Any],
        compensation: Optional[CompensationPlan],
        completion_order: int,
    ) -> None:
        self.step_id = step_id
        self.tool_name = tool_name
        self.parameters = parameters
        self.result = result
        self.compensation = compensation
        self.completion_order = completion_order


class CompensationEngine:
    def __init__(
        self,
        registry: AdapterRegistry,
        wal: WriteAheadLog,
        audit: AuditLog,
        run_id: UUID,
        workflow_id: str,
        variables: Optional[dict[str, Any]] = None,
        max_retries: int = 3,
        backoff_scale: float = 0.001,
    ) -> None:
        self.registry = registry
        self.wal = wal
        self.audit = audit
        self.run_id = run_id
        self.workflow_id = workflow_id
        self.variables = variables or {}
        self.max_retries = max_retries
        self.backoff_scale = backoff_scale
        self.validator = CompensationValidator(registry)

    # -- planning -------------------------------------------------------------

    def plan_compensations(
        self, completed: list[CompletedAction]
    ) -> list[CompletedAction]:
        """Order completed actions for compensation: reverse completion order."""
        ordered = sorted(completed, key=lambda c: c.completion_order, reverse=True)
        for action in ordered:
            if action.compensation and action.compensation.strategy not in (
                CompensationStrategy.NONE,
            ):
                self.audit.append(
                    AuditEvent.COMPENSATION_PLANNED,
                    run_id=str(self.run_id),
                    entity_id=action.step_id,
                    payload={
                        "tool": action.compensation.tool,
                        "strategy": str(action.compensation.strategy),
                        "source": str(action.compensation.source),
                    },
                )
        return ordered

    # -- execution ------------------------------------------------------------

    async def execute_compensation(
        self, action: CompletedAction
    ) -> CompensationExecution:
        """Execute a single compensation with validation, retries, reconciliation."""
        comp = action.compensation
        record = CompensationExecution(
            source_step_id=action.step_id,
            source=comp.source if comp else CompensationSource.NONE,
            strategy=comp.strategy if comp else CompensationStrategy.NONE,
        )

        if comp is None or comp.strategy in (
            CompensationStrategy.NONE,
        ):
            record.outcome = Outcome.SUCCESS
            record.detail = "no compensation required"
            return record

        if comp.strategy == CompensationStrategy.MANUAL_ESCALATION:
            record.approval_state = ApprovalState.REQUESTED
            record.outcome = Outcome.FAILURE
            record.residual_risk = list(comp.risks) or [
                "manual escalation required; no verified compensation"
            ]
            record.detail = "manual escalation required"
            self.audit.append(
                AuditEvent.ESCALATION,
                run_id=str(self.run_id),
                entity_id=action.step_id,
                payload={"reason": "manual_escalation", "risks": record.residual_risk},
            )
            return record

        # Build binding context from the original action.
        context = {
            "result": action.result,
            "params": action.parameters,
            "variables": self.variables,
        }
        validation = self.validator.validate(comp, context)
        if not validation.valid:
            record.outcome = Outcome.FAILURE
            record.residual_risk = validation.reasons
            record.detail = (
                "stale compensation blocked" if validation.stale
                else "compensation validation failed"
            )
            self.audit.append(
                AuditEvent.COMPENSATION_FAILED,
                run_id=str(self.run_id),
                entity_id=action.step_id,
                payload={"reasons": validation.reasons, "stale": validation.stale},
            )
            return record

        adapter = self.registry.get(comp.tool)

        # Retry loop (compensation policy).
        from ..runtime.retry import sleep_backoff
        from ..models import RetryPolicy

        policy = RetryPolicy(max_attempts=self.max_retries, backoff="exponential",
                             backoff_ms=100)
        attempt = 0
        last_detail = ""
        while attempt < self.max_retries:
            attempt += 1
            record.attempts = attempt

            comp_intent = ActionIntent(
                run_id=self.run_id,
                workflow_id=self.workflow_id,
                step_id=f"comp:{action.step_id}",
                tool_name=comp.tool,
                parameters=validation.bound_parameters,
                is_compensation=True,
            )
            record.compensation_intent_id = comp_intent.intent_id

            # WAL BEFORE side effect.
            self.wal.write_intent(comp_intent, tier=2)
            self.wal.update_status(comp_intent.intent_id, WALStatus.COMPENSATING)
            self.audit.append(
                AuditEvent.COMPENSATION_START,
                run_id=str(self.run_id),
                entity_id=action.step_id,
                payload={"tool": comp.tool, "attempt": attempt,
                         "intent_id": str(comp_intent.intent_id)},
            )

            try:
                result = await adapter.execute(comp_intent)
            except Exception as exc:  # noqa: BLE001 - convert to failure record
                last_detail = f"compensation raised: {exc}"
                self.wal.update_status(comp_intent.intent_id, WALStatus.FAILED,
                                       outcome="failure")
                await sleep_backoff(policy, attempt, self.backoff_scale)
                continue

            if result.is_unknown:
                # Reconcile: query the adapter for the true outcome.
                query = await adapter.query_status(comp_intent.intent_id)
                if query.found and query.outcome == Outcome.SUCCESS:
                    result_data = query.data
                    outcome = Outcome.SUCCESS
                else:
                    self.wal.update_status(comp_intent.intent_id, WALStatus.UNKNOWN,
                                           outcome="unknown")
                    last_detail = "compensation outcome unknown after reconcile"
                    await sleep_backoff(policy, attempt, self.backoff_scale)
                    continue
            elif result.is_failure:
                self.wal.update_status(comp_intent.intent_id, WALStatus.FAILED,
                                       outcome="failure",
                                       error={"message": result.error_message})
                last_detail = result.error_message or "compensation failed"
                await sleep_backoff(policy, attempt, self.backoff_scale)
                continue
            else:
                result_data = result.data
                outcome = Outcome.SUCCESS

            # Verify compensation postcondition.
            verification = await adapter.verify_postcondition(
                comp_intent, {"result": result_data}
            )
            record.verification = verification
            if not verification.passed:
                self.wal.update_status(comp_intent.intent_id, WALStatus.FAILED,
                                       outcome="failure")
                last_detail = f"compensation postcondition failed: {verification.detail}"
                await sleep_backoff(policy, attempt, self.backoff_scale)
                continue

            # Success.
            self.wal.update_status(comp_intent.intent_id, WALStatus.COMPENSATED,
                                   outcome="success", result=result_data)
            record.outcome = Outcome.SUCCESS
            record.detail = "compensation verified"
            record.residual_risk = list(comp.risks)
            self.audit.append(
                AuditEvent.COMPENSATION_COMPLETE,
                run_id=str(self.run_id),
                entity_id=action.step_id,
                payload={"tool": comp.tool, "attempt": attempt,
                         "result": result_data},
            )
            return record

        # Exhausted retries -> failure.
        record.outcome = Outcome.FAILURE
        record.detail = last_detail or "compensation failed after retries"
        if not record.residual_risk:
            record.residual_risk = [record.detail]
        self.audit.append(
            AuditEvent.COMPENSATION_FAILED,
            run_id=str(self.run_id),
            entity_id=action.step_id,
            payload={"tool": comp.tool, "attempts": attempt, "detail": record.detail},
        )
        return record
