"""Integration tests: compensation failure -> INCONSISTENT, and duplicate run."""

from __future__ import annotations

import pytest

from aatm.adapters import AdapterRegistry, FailureInjector, FailureRule
from aatm.enums import WorkflowState
from aatm.planner import SagaPlanner, WorkflowParser
from aatm.runtime import TransactionCoordinator

pytestmark = pytest.mark.asyncio


def _plan(tmp_config, path="workflows/travel_booking.yaml"):
    parsed = WorkflowParser(tmp_config).parse(path)
    return SagaPlanner(tmp_config).plan(parsed)


async def test_compensation_failure_enters_inconsistent(tmp_config):
    """F08: step-3 fails AND the compensation for step-2 (cancel_hotel) keeps
    failing -> retries exhausted -> INCONSISTENT, escalation recorded.
    """
    plan = _plan(tmp_config)
    injector = FailureInjector([
        # Original failure at step-3.
        FailureRule(mode="error", step_id="step-3",
                    failure_class="resource_unavailable", message="CAR_UNAVAILABLE"),
        # Compensation cancel_hotel always fails.
        FailureRule(mode="error", tool="cancel_hotel",
                    failure_class="server_error", message="CANCEL_HOTEL_DOWN"),
    ])
    reg = AdapterRegistry(injector=injector)
    coord = TransactionCoordinator(plan, reg, config=tmp_config, backoff_scale=0.0)
    try:
        run = await coord.run_workflow()
        assert run.state == WorkflowState.INCONSISTENT
        # The hotel compensation failed after retries.
        hotel_comp = [c for c in run.compensations if c.source_step_id == "step-2"]
        assert hotel_comp
        assert hotel_comp[0].outcome.value == "failure"
        assert hotel_comp[0].attempts >= 1
        # Escalation / inconsistent audit events present.
        events = {e["event"] for e in coord.audit.entries()}
        assert "WORKFLOW_INCONSISTENT" in events
        assert coord.audit.verify().valid
    finally:
        coord.close()


async def test_duplicate_run_dedup_via_idempotency(tmp_config):
    """F16: the same logical charge intent is not double-executed within a run.

    We drive the payment step twice through the same coordinator idempotency
    store by replaying step-4 - the second claim finds the recorded success.
    """
    plan = _plan(tmp_config)
    reg = AdapterRegistry()
    coord = TransactionCoordinator(plan, reg, config=tmp_config, backoff_scale=0.0)
    try:
        run = await coord.run_workflow()
        assert run.state == WorkflowState.COMPLETED
        # Exactly one payment for the single logical charge.
        assert len(reg.world.payments) == 1

        # Simulate a duplicate submission of the same idempotency key.
        step4 = plan.step("step-4")
        intent = coord._build_intent(step4)
        is_new, existing = coord.idempotency.claim(
            coord.run_id, intent.idempotency_key, intent.intent_id,
            step4.tool_name, intent.parameters_hash,
        )
        # Not new; the prior success is found -> no new charge would be issued.
        assert is_new is False
        assert existing is not None
        assert existing.outcome == "success"
        assert len(reg.world.payments) == 1
    finally:
        coord.close()


async def test_workflow_timeout_stops_new_work(tmp_config):
    """F17: a tiny workflow timeout stops new work and recovers completed work."""
    parsed = WorkflowParser(tmp_config).parse("workflows/travel_booking.yaml")
    plan = SagaPlanner(tmp_config).plan(parsed)
    plan.timeout_seconds = 0  # force immediate timeout on the loop guard
    reg = AdapterRegistry()
    coord = TransactionCoordinator(plan, reg, config=tmp_config, backoff_scale=0.0)
    try:
        run = await coord.run_workflow()
        # With a 0s budget the very first check trips timeout; nothing committed.
        assert run.state in (WorkflowState.TIMED_OUT, WorkflowState.ABORTED)
        assert coord.audit.verify().valid
    finally:
        coord.close()
