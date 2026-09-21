"""Integration tests: retryable-transaction tier (forward-only recovery).

A step declared ``reversibility: retryable`` is a saga *retryable transaction*:
it must eventually succeed and is driven FORWARD on failure. Contrast with
``test_recovery_flows`` where a post-pivot failure unwinds everything.

Two behaviors are proven here:
  1. A transient failure on a retryable post-pivot step is retried to success;
     the run COMPLETES and the committed charge is never touched.
  2. A persistent failure exhausts the retries and is escalated FORWARD:
     the run is INCONSISTENT with a dead-letter entry, but the committed charge
     is preserved (NO refund) - the step needs manual completion, not a rollback.
"""

from __future__ import annotations

from aatm.adapters import AdapterRegistry, FailureInjector, FailureRule
from aatm.enums import CompensationStrategy, Outcome, Reversibility, Tier, WorkflowState
from aatm.planner import SagaPlanner, WorkflowParser
from aatm.runtime import TransactionCoordinator

# asyncio_mode="auto" auto-detects the async tests; the planning test is sync, so
# no module-level asyncio mark (which would warn on the sync test).

WORKFLOW = "workflows/order_fulfillment.yaml"


def _plan(tmp_config):
    parsed = WorkflowParser(tmp_config).parse(WORKFLOW)
    return SagaPlanner(tmp_config).plan(parsed)


def test_retryable_step_planned_as_tier1_no_compensation(tmp_config):
    """The retryable step classifies to Tier-1 with NO compensation strategy."""
    plan = _plan(tmp_config)
    assert plan.critical_issues == []
    step3 = next(s for s in plan.steps if s.step_id == "step-3")
    assert step3.reversibility == Reversibility.RETRYABLE
    assert step3.tier == Tier.ONE
    assert step3.is_pivot is False
    assert step3.compensation.strategy == CompensationStrategy.NONE


async def test_retryable_transient_failure_retries_forward_to_success(tmp_config):
    """A retryable post-pivot step fails twice (non-retryable class!) then succeeds.

    ``mode='error'`` defaults to failure_class=business_rule, which is NOT in the
    globally-retryable set. A normal step would give up immediately; a RETRYABLE
    step retries forward regardless of failure class until it succeeds.
    """
    plan = _plan(tmp_config)
    injector = FailureInjector([
        FailureRule(mode="error", step_id="step-3", message="CRM_BLIP",
                    until_attempt=2)
    ])
    reg = AdapterRegistry(injector=injector)
    coord = TransactionCoordinator(plan, reg, config=tmp_config, backoff_scale=0.0)
    try:
        run = await coord.run_workflow()

        assert run.pivot_crossed is True
        assert run.state == WorkflowState.COMPLETED

        # Charge captured exactly once, never refunded.
        assert len(reg.world.payments) == 1
        assert list(reg.world.payments.values())[0]["status"] == "captured"
        assert len(reg.world.refunds) == 0

        # Two CRM records: the order (step-1) + the CRM sync (step-3), both active
        # (the forward retry eventually succeeded and created the sync record).
        assert len(reg.world.crm_records) == 2
        assert all(r["status"] == "active" for r in reg.world.crm_records.values())

        # Step-3 succeeded after 2 retries; nothing dead-lettered.
        step3 = next(s for s in run.step_executions if s.step_id == "step-3")
        assert step3.outcome == Outcome.SUCCESS
        assert step3.retries == 2
        assert coord.dead_letter.is_empty()
        assert coord.audit.verify().valid
    finally:
        coord.close()


async def test_retryable_exhausted_escalates_forward_without_unwind(tmp_config):
    """A retryable post-pivot step that never succeeds is escalated FORWARD.

    The committed charge is preserved (NO refund, NO order deletion). The run is
    INCONSISTENT with a dead-letter entry so an operator can finish the step by
    hand. This is the key contrast with travel_booking's full unwind.
    """
    plan = _plan(tmp_config)
    injector = FailureInjector([
        FailureRule(mode="error", step_id="step-3", message="CRM_DOWN")
    ])
    reg = AdapterRegistry(injector=injector)
    coord = TransactionCoordinator(plan, reg, config=tmp_config, backoff_scale=0.0)
    try:
        run = await coord.run_workflow()

        assert run.pivot_crossed is True
        assert run.state == WorkflowState.INCONSISTENT

        # Committed work PRESERVED: charge stands, order record stays active.
        assert len(reg.world.payments) == 1
        assert list(reg.world.payments.values())[0]["status"] == "captured"
        assert len(reg.world.refunds) == 0, "forward escalation must NOT refund"
        assert list(reg.world.crm_records.values() or [{}])
        # Order (step-1) NOT deleted - no unwind happened.
        active_orders = [r for r in reg.world.crm_records.values()
                         if r["status"] == "active"]
        assert len(active_orders) >= 1

        # A forward_fix FAILURE compensation record surfaces the residual risk.
        comp = [c for c in run.compensations if c.source_step_id == "step-3"]
        assert len(comp) == 1
        assert comp[0].strategy == CompensationStrategy.FORWARD_FIX
        assert comp[0].outcome == Outcome.FAILURE
        assert comp[0].residual_risk

        # No pivot (step-2) compensation was attempted - the charge was untouched.
        assert not any(c.source_step_id == "step-2" for c in run.compensations)

        # Durable dead-letter entry for the operator.
        entries = coord.dead_letter.entries()
        assert len(entries) == 1
        assert entries[0]["step_id"] == "step-3"
        assert entries[0]["strategy"] == "forward_retry"
        assert entries[0]["residual_risk"]

        assert coord.metrics.snapshot()["counters"].get("forward_escalations") == 1
        assert coord.audit.verify().valid
    finally:
        coord.close()
