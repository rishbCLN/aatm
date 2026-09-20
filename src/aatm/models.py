"""Core data models for AATM.

Strongly-validated Pydantic models. These are the single source of truth for the
shapes that flow through the engine: tool definitions, action intents, step and
compensation executions, plans, results, and the report.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator

from .enums import (
    ApprovalState,
    CompensationSource,
    CompensationStrategy,
    FailureClass,
    IdempotencyMode,
    IntentStatus,
    Outcome,
    Reversibility,
    RiskLevel,
    SideEffectScope,
    Tier,
    WorkflowState,
)


def utcnow() -> datetime:
    """Timezone-aware UTC now (avoids naive datetimes across the codebase)."""
    return datetime.now(timezone.utc)


def canonical_json(data: Any) -> str:
    """Deterministic JSON serialization for hashing (sorted keys, no spaces)."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def hash_params(parameters: dict[str, Any]) -> str:
    """Stable SHA-256 hash of a parameter dict."""
    return hashlib.sha256(canonical_json(parameters).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------


class CompensationContract(BaseModel):
    """Explicit compensation contract attached to a tool or step."""

    tool: str
    parameter_mapping: dict[str, str] = Field(default_factory=dict)
    strategy: CompensationStrategy = CompensationStrategy.COMPENSATE
    verification: Optional[dict[str, Any]] = None
    requires_approval: bool = False


class PostconditionContract(BaseModel):
    """A checkable postcondition expression + failure policy."""

    expression: str
    on_failure: Literal["compensate", "retry", "fail", "ignore"] = "compensate"
    description: str = ""


class ToolDefinition(BaseModel):
    """Deterministic metadata that governs how AATM treats a tool."""

    name: str
    description: str = ""
    side_effect_scope: SideEffectScope = SideEffectScope.NONE
    reversibility: Reversibility = Reversibility.UNKNOWN
    idempotent: bool = False
    idempotency_mode: IdempotencyMode = IdempotencyMode.NONE
    parameters_schema: dict[str, Any] = Field(default_factory=dict)
    returns_schema: dict[str, Any] = Field(default_factory=dict)
    compensation_contract: Optional[CompensationContract] = None
    postcondition_contract: Optional[PostconditionContract] = None
    timeout_ms: int = 5000
    risk_level: RiskLevel = RiskLevel.LOW

    @field_validator("timeout_ms")
    @classmethod
    def _positive_timeout(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("timeout_ms must be positive")
        return v


# ---------------------------------------------------------------------------
# Action intent
# ---------------------------------------------------------------------------


class ActionIntent(BaseModel):
    """A durable record of intent to invoke a tool, created BEFORE the effect."""

    intent_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    workflow_id: str
    step_id: str
    tool_name: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    parameters_hash: str = ""
    idempotency_key: Optional[str] = None
    is_compensation: bool = False
    status: IntentStatus = IntentStatus.PENDING
    created_at: datetime = Field(default_factory=utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None
    outcome: Optional[Outcome] = None

    def model_post_init(self, __context: object) -> None:
        if not self.parameters_hash:
            self.parameters_hash = hash_params(self.parameters)


# ---------------------------------------------------------------------------
# Tool execution results (adapter contract)
# ---------------------------------------------------------------------------


class ToolResult(BaseModel):
    """Result returned by a tool adapter's ``execute``."""

    intent_id: UUID
    outcome: Outcome
    data: dict[str, Any] = Field(default_factory=dict)
    error_message: Optional[str] = None
    failure_class: Optional[FailureClass] = None
    latency_ms: float = 0.0

    @property
    def is_success(self) -> bool:
        return self.outcome == Outcome.SUCCESS

    @property
    def is_failure(self) -> bool:
        return self.outcome == Outcome.FAILURE

    @property
    def is_unknown(self) -> bool:
        return self.outcome == Outcome.UNKNOWN


class OutcomeQuery(BaseModel):
    """Result of querying an adapter for the status of a prior intent."""

    intent_id: UUID
    found: bool
    outcome: Outcome
    data: dict[str, Any] = Field(default_factory=dict)
    detail: str = ""


class VerificationResult(BaseModel):
    """Result of a postcondition / compensation verification."""

    passed: bool
    expression: str = ""
    detail: str = ""
    observed: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Compensation objects
# ---------------------------------------------------------------------------


class CompensationPlan(BaseModel):
    """A resolved compensation proposal for a single completed step.

    Mirrors the compensation object in the spec (section 3.3).
    """

    strategy: CompensationStrategy
    tool: Optional[str] = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    mapping: dict[str, str] = Field(default_factory=dict)
    confidence: float = 1.0
    source: CompensationSource = CompensationSource.NONE
    requires_approval: bool = False
    verification: Optional[dict[str, Any]] = None
    risks: list[str] = Field(default_factory=list)

    # Bookkeeping
    source_step_id: Optional[str] = None
    source_intent_id: Optional[UUID] = None


class CompensationExecution(BaseModel):
    """Record of an executed compensation."""

    source_step_id: str
    compensation_intent_id: Optional[UUID] = None
    source: CompensationSource
    strategy: CompensationStrategy
    approval_state: ApprovalState = ApprovalState.NOT_REQUIRED
    outcome: Optional[Outcome] = None
    verification: Optional[VerificationResult] = None
    residual_risk: list[str] = Field(default_factory=list)
    attempts: int = 0
    detail: str = ""


# ---------------------------------------------------------------------------
# Planned step + plan
# ---------------------------------------------------------------------------


class RetryPolicy(BaseModel):
    max_attempts: int = 1
    backoff: Literal["none", "fixed", "exponential"] = "exponential"
    backoff_ms: int = 250
    retry_on: list[FailureClass] = Field(default_factory=list)


class PlannedStep(BaseModel):
    """A single planned step after saga planning."""

    step_id: str
    name: str
    tool_name: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    preconditions: list[dict[str, Any]] = Field(default_factory=list)
    postconditions: list[PostconditionContract] = Field(default_factory=list)

    tier: Tier = Tier.NONE
    reversibility: Reversibility = Reversibility.UNKNOWN
    side_effect_scope: SideEffectScope = SideEffectScope.NONE
    risk_level: RiskLevel = RiskLevel.LOW

    is_pivot: bool = False
    is_post_pivot: bool = False
    approval_required: bool = False
    idempotency_key_expr: Optional[str] = None
    idempotent: bool = False
    idempotency_mode: IdempotencyMode = IdempotencyMode.NONE

    compensation: Optional[CompensationPlan] = None
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    risk_flags: list[str] = Field(default_factory=list)


class FailurePolicy(BaseModel):
    on_step_failure: Literal[
        "compensate_and_abort", "abort", "escalate_human", "retry_then_compensate"
    ] = "compensate_and_abort"
    on_compensation_failure: Literal["escalate_human", "abort", "retry"] = "escalate_human"
    on_unknown_outcome: Literal["query_and_reconcile", "escalate_human", "fail"] = (
        "query_and_reconcile"
    )
    max_compensation_retries: int = 3


class SagaPlan(BaseModel):
    """Full transaction/saga plan produced by the planner."""

    workflow_id: str
    workflow_name: str
    agent: str = ""
    description: str = ""
    max_steps: int = 100
    timeout_seconds: int = 120
    failure_policy: FailurePolicy = Field(default_factory=FailurePolicy)

    steps: list[PlannedStep] = Field(default_factory=list)
    pivot_step_id: Optional[str] = None
    variables: dict[str, Any] = Field(default_factory=dict)

    # Validation output
    critical_issues: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.critical_issues

    def step(self, step_id: str) -> Optional[PlannedStep]:
        for s in self.steps:
            if s.step_id == step_id:
                return s
        return None

    def pivot_index(self) -> Optional[int]:
        if self.pivot_step_id is None:
            return None
        for i, s in enumerate(self.steps):
            if s.step_id == self.pivot_step_id:
                return i
        return None


# ---------------------------------------------------------------------------
# Step execution record
# ---------------------------------------------------------------------------


class StepExecution(BaseModel):
    """Record of a single step's execution attempt(s) at runtime."""

    step_id: str
    attempt: int = 0
    intent_id: Optional[UUID] = None
    tier: Tier = Tier.NONE
    is_pivot: bool = False
    is_post_pivot: bool = False
    precondition_passed: Optional[bool] = None
    action_result: Optional[dict[str, Any]] = None
    postcondition: Optional[VerificationResult] = None
    compensation_strategy: Optional[CompensationStrategy] = None
    outcome: Optional[Outcome] = None
    final_status: IntentStatus = IntentStatus.PENDING
    retries: int = 0
    reconciled: bool = False
    approval_state: ApprovalState = ApprovalState.NOT_REQUIRED
    detail: str = ""
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Workflow run + result
# ---------------------------------------------------------------------------


class WorkflowRun(BaseModel):
    """Live/persisted state of a run."""

    run_id: UUID
    workflow_id: str
    workflow_name: str = ""
    agent: str = ""
    state: WorkflowState = WorkflowState.PLANNED
    pivot_step_id: Optional[str] = None
    pivot_crossed: bool = False
    completed_step_ids: list[str] = Field(default_factory=list)
    variables: dict[str, Any] = Field(default_factory=dict)
    step_executions: list[StepExecution] = Field(default_factory=list)
    compensations: list[CompensationExecution] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    detail: str = ""


class RunResult(BaseModel):
    """Machine-readable run result (validates against result.schema.json)."""

    run_id: str
    workflow_id: str
    workflow_name: str = ""
    agent: str = ""
    build_version: str = "0.1.0"
    state: WorkflowState
    pivot_step_id: Optional[str] = None
    pivot_crossed: bool = False
    started_at: str = ""
    finished_at: str = ""
    duration_ms: float = 0.0

    steps: list[StepExecution] = Field(default_factory=list)
    compensations: list[CompensationExecution] = Field(default_factory=list)
    retries: int = 0
    unknown_outcomes: int = 0
    approvals: list[dict[str, Any]] = Field(default_factory=list)

    final_world_state: dict[str, Any] = Field(default_factory=dict)
    audit_chain_valid: bool = False
    audit_event_count: int = 0
    residual_risks: list[str] = Field(default_factory=list)
    failed_assertions: list[str] = Field(default_factory=list)

    consistent: bool = False
    exact_rollback_possible: bool = True
    detail: str = ""
