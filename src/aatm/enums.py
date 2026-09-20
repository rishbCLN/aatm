"""Enumerations used across the AATM engine.

All enums inherit from ``str`` so they serialize cleanly to JSON and compare
transparently with string literals coming from YAML / JSON documents.
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """A string enum whose ``str()`` is the plain value (JSON friendly)."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)


class SideEffectScope(StrEnum):
    """How far the observable effect of a tool reaches."""

    NONE = "none"
    LOCAL = "local"
    INTERNAL = "internal"
    EXTERNAL = "external"


class Reversibility(StrEnum):
    """Semantic reversibility of a tool's side effect."""

    FULLY_REVERSIBLE = "fully_reversible"
    COMPENSATABLE = "compensatable"
    IRREVERSIBLE = "irreversible"
    # Fail-closed default for tools we cannot classify.
    UNKNOWN = "unknown"


class Tier(int, Enum):
    """Reversibility tier.

    Tier 1: fully reversible / local  -> restore checkpoint/snapshot.
    Tier 2: compensatable             -> execute semantic inverse.
    Tier 3: irreversible externally   -> approval gate + forward recovery.
    Tier 0 is used for pure/no-side-effect steps and for unknown fail-closed
    handling is represented separately (see reversibility == unknown).
    """

    NONE = 0
    ONE = 1
    TWO = 2
    THREE = 3

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)


class IdempotencyMode(StrEnum):
    NATIVE = "native"
    WRAPPER = "wrapper"
    NONE = "none"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class IntentStatus(StrEnum):
    """Lifecycle of a single action intent (mirrors WAL states)."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    COMPENSATING = "COMPENSATING"
    COMPENSATED = "COMPENSATED"
    ESCALATED = "ESCALATED"


class WALStatus(StrEnum):
    """Durable write-ahead-log record states."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    COMPENSATING = "COMPENSATING"
    COMPENSATED = "COMPENSATED"
    ESCALATED = "ESCALATED"


class Outcome(StrEnum):
    """Classification of a tool execution result."""

    SUCCESS = "success"
    FAILURE = "failure"
    UNKNOWN = "unknown"


class WorkflowState(StrEnum):
    """High-level run state machine."""

    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    RECOVERING = "RECOVERING"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"
    FAILED = "FAILED"
    INCONSISTENT = "INCONSISTENT"
    UNKNOWN = "UNKNOWN"
    TIMED_OUT = "TIMED_OUT"


class CompensationStrategy(StrEnum):
    RESTORE = "restore"
    COMPENSATE = "compensate"
    FORWARD_FIX = "forward_fix"
    MANUAL_ESCALATION = "manual_escalation"
    NONE = "none"


class CompensationSource(StrEnum):
    """Authority hierarchy for a compensation (highest first)."""

    EXPLICIT = "explicit"
    REGISTRY = "registry"
    ADAPTER = "adapter"
    LLM = "llm"
    NONE = "none"


class ApprovalState(StrEnum):
    NOT_REQUIRED = "not_required"
    REQUESTED = "requested"
    GRANTED = "granted"
    DENIED = "denied"


class FailureClass(StrEnum):
    """Classification of a tool failure for retry decisions."""

    # Retryable (transient)
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    RATE_LIMIT = "rate_limit"
    SERVER_ERROR = "server_error"  # 5xx-equivalent
    # Non-retryable
    VALIDATION = "validation"
    AUTHORIZATION = "authorization"
    BUSINESS_RULE = "business_rule"
    RESOURCE_UNAVAILABLE = "resource_unavailable"
    MALFORMED = "malformed"
    UNKNOWN = "unknown"


RETRYABLE_FAILURES = frozenset(
    {
        FailureClass.TIMEOUT,
        FailureClass.CONNECTION,
        FailureClass.RATE_LIMIT,
        FailureClass.SERVER_ERROR,
    }
)

NON_RETRYABLE_FAILURES = frozenset(
    {
        FailureClass.VALIDATION,
        FailureClass.AUTHORIZATION,
        FailureClass.BUSINESS_RULE,
        FailureClass.RESOURCE_UNAVAILABLE,
        FailureClass.MALFORMED,
    }
)


class AuditEvent(StrEnum):
    """Canonical audit event names (append-only hash-chained log)."""

    WORKFLOW_START = "WORKFLOW_START"
    PLAN_CREATED = "PLAN_CREATED"
    CHECKPOINT_CREATED = "CHECKPOINT_CREATED"
    ACTION_INTENT_CREATED = "ACTION_INTENT_CREATED"
    ACTION_START = "ACTION_START"
    ACTION_COMPLETE = "ACTION_COMPLETE"
    ACTION_FAILED = "ACTION_FAILED"
    ACTION_UNKNOWN = "ACTION_UNKNOWN"
    POST_CONDITION_PASS = "POST_CONDITION_PASS"
    POST_CONDITION_FAIL = "POST_CONDITION_FAIL"
    RETRY = "RETRY"
    RECONCILIATION_START = "RECONCILIATION_START"
    RECONCILIATION_RESULT = "RECONCILIATION_RESULT"
    COMPENSATION_PLANNED = "COMPENSATION_PLANNED"
    COMPENSATION_START = "COMPENSATION_START"
    COMPENSATION_COMPLETE = "COMPENSATION_COMPLETE"
    COMPENSATION_FAILED = "COMPENSATION_FAILED"
    PIVOT_REACHED = "PIVOT_REACHED"
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    APPROVAL_GRANTED = "APPROVAL_GRANTED"
    APPROVAL_DENIED = "APPROVAL_DENIED"
    ESCALATION = "ESCALATION"
    WORKFLOW_COMPLETE = "WORKFLOW_COMPLETE"
    WORKFLOW_ABORTED = "WORKFLOW_ABORTED"
    WORKFLOW_INCONSISTENT = "WORKFLOW_INCONSISTENT"
    CRASH_RECOVERY_START = "CRASH_RECOVERY_START"
    CRASH_RECOVERY_COMPLETE = "CRASH_RECOVERY_COMPLETE"


class ReportStatus(StrEnum):
    """Overall assessment status for a run (not a raw numeric score)."""

    PASS = "PASS"
    PASS_WITH_CONDITIONS = "PASS_WITH_CONDITIONS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    INCONSISTENT = "INCONSISTENT"


# Mapping from reversibility to the canonical tier used by the planner.
REVERSIBILITY_TO_TIER = {
    Reversibility.FULLY_REVERSIBLE: Tier.ONE,
    Reversibility.COMPENSATABLE: Tier.TWO,
    Reversibility.IRREVERSIBLE: Tier.THREE,
}
