"""Unit tests for compensation planning + planner validation."""

from __future__ import annotations

from aatm.enums import CompensationSource, CompensationStrategy
from aatm.planner import SagaPlanner, WorkflowParser


def _plan_from_dict(tmp_config, data):
    parsed = WorkflowParser(tmp_config).parse_dict(data)
    return SagaPlanner(tmp_config).plan(parsed)


def test_registry_template_used(tmp_config):
    data = {
        "workflow": {"id": "wf", "name": "w"},
        "steps": [{"id": "s1", "tool": "book_hotel"}],
    }
    plan = _plan_from_dict(tmp_config, data)
    comp = plan.steps[0].compensation
    assert comp.tool == "cancel_hotel"
    assert comp.source == CompensationSource.REGISTRY


def test_explicit_override_beats_registry(tmp_config):
    data = {
        "workflow": {"id": "wf", "name": "w"},
        "steps": [
            {
                "id": "s1",
                "tool": "book_hotel",
                "compensation": {
                    "tool": "custom_cancel",
                    "strategy": "compensate",
                    "parameter_mapping": {"booking_id": "result.booking_id"},
                },
            }
        ],
    }
    plan = _plan_from_dict(tmp_config, data)
    comp = plan.steps[0].compensation
    assert comp.tool == "custom_cancel"
    assert comp.source == CompensationSource.EXPLICIT


def test_invalid_mapping_emits_warning(tmp_config):
    data = {
        "workflow": {"id": "wf", "name": "w"},
        "steps": [
            {
                "id": "s1",
                "tool": "book_hotel",
                "compensation": {
                    "tool": "cancel_hotel",
                    "parameter_mapping": {"booking_id": "nonsense.field"},
                },
            }
        ],
    }
    plan = _plan_from_dict(tmp_config, data)
    assert any("does not reference" in w for w in plan.warnings)


def test_unknown_tool_fails_closed(tmp_config):
    data = {
        "workflow": {"id": "wf", "name": "w"},
        "steps": [{"id": "s1", "tool": "totally_unknown_tool"}],
    }
    plan = _plan_from_dict(tmp_config, data)
    step = plan.steps[0]
    # Fail-closed: treated as Tier-3, critical, and gated behind approval.
    assert step.tier.value == 3
    assert step.approval_required is True
    assert "UNKNOWN_TOOL_FAIL_CLOSED" in step.risk_flags
    assert step.compensation.strategy == CompensationStrategy.MANUAL_ESCALATION


def test_unknown_tool_without_approval_blocks_plan(tmp_config):
    """If an unknown tool's approval gate is stripped, the plan must be rejected."""
    data = {
        "workflow": {"id": "wf", "name": "w"},
        "steps": [{"id": "s1", "tool": "totally_unknown_tool", "approval_required": False}],
    }
    parsed = WorkflowParser(tmp_config).parse_dict(data)
    plan = SagaPlanner(tmp_config).plan(parsed)
    assert not plan.is_valid
    assert any("fail-closed" in c or "approval" in c for c in plan.critical_issues)


def test_non_idempotent_retryable_without_mechanism_blocks(tmp_config):
    # Explicit tool with idempotency_mode none but retryable > 1 attempt.
    data = {
        "workflow": {"id": "wf", "name": "w"},
        "steps": [
            {
                "id": "s1",
                "tool": "risky_tool",
                "retry": {"max_attempts": 3, "retry_on": ["timeout"]},
            }
        ],
        "tools": [
            {
                "name": "risky_tool",
                "reversibility": "compensatable",
                "side_effect_scope": "external",
                "idempotent": False,
                "idempotency_mode": "none",
                "compensation_contract": {
                    "tool": "undo_risky",
                    "parameter_mapping": {"id": "result.id"},
                },
            }
        ],
    }
    plan = _plan_from_dict(tmp_config, data)
    assert not plan.is_valid
    assert any("idempotency" in c for c in plan.critical_issues)


def test_pre_pivot_tier2_without_compensation_blocks(tmp_config):
    data = {
        "workflow": {"id": "wf", "name": "w"},
        "steps": [
            {"id": "s1", "tool": "no_comp_tool"},
            {"id": "s2", "tool": "charge_payment", "approval_required": True},
        ],
        "tools": [
            {
                "name": "no_comp_tool",
                "reversibility": "compensatable",
                "side_effect_scope": "external",
                "idempotent": True,
                "idempotency_mode": "native",
            }
        ],
    }
    plan = _plan_from_dict(tmp_config, data)
    assert not plan.is_valid
    assert any("no verified compensation" in c for c in plan.critical_issues)


def test_travel_plan_is_valid(tmp_config):
    plan = WorkflowParser(tmp_config).parse("workflows/travel_booking.yaml")
    saga = SagaPlanner(tmp_config).plan(plan)
    assert saga.is_valid
    assert saga.pivot_step_id == "step-4"
    # Charge payment forward-fix.
    charge = saga.step("step-4")
    assert charge.compensation.strategy == CompensationStrategy.FORWARD_FIX
