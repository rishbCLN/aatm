"""Integration tests: post-pivot forward recovery + unknown outcome reconciliation."""

from __future__ import annotations

import pytest

from aatm.adapters import AdapterRegistry, FailureInjector, FailureRule
from aatm.enums import Outcome, WorkflowState
from aatm.planner import SagaPlanner, WorkflowParser
from aatm.runtime import TransactionCoordinator

pytestmark = pytest.mark.asyncio


def _plan(tmp_config, path="workflows/travel_booking.yaml"):
    parsed = WorkflowParser(tmp_config).parse(path)
    return SagaPlanner(tmp_config).plan(parsed)


async def test_post_pivot_failure_uses_forward_recovery(tmp_config):
    """F07: confirm_bookings (step-5) fails after payment pivot.

    Recovery must NOT pretend to roll back the payment. It issues a refund (new
    transaction) and cancels the reservations - business-consistent, not exact.
    """
    plan = _plan(tmp_config)
    injector = FailureInjector([
        FailureRule(mode="timeout", step_id="step-5", as_unknown=False,
                    until_attempt=5)
    ])
    reg = AdapterRegistry(injector=injector)
    coord = TransactionCoordinator(plan, reg, config=tmp_config, backoff_scale=0.0)
    try:
        run = await coord.run_workflow()
        assert run.pivot_crossed is True
        assert run.state == WorkflowState.ABORTED
        assert "Exact rollback impossible" in run.detail

        w = reg.world
        # Payment was captured then refunded (a NEW refund transaction exists).
        assert len(w.payments) == 1
        assert len(w.refunds) == 1
        assert list(w.payments.values())[0]["status"] == "refunded"
        # Reservations cancelled.
        assert list(w.flights.values())[0]["status"] == "cancelled"
        assert list(w.hotels.values())[0]["status"] == "cancelled"
        assert list(w.cars.values())[0]["status"] == "cancelled"

        # The payment compensation is a forward_fix (refund), not a "restore".
        pay_comp = [c for c in run.compensations if c.source_step_id == "step-4"]
        assert len(pay_comp) == 1
        assert pay_comp[0].strategy.value == "forward_fix"
        assert coord.audit.verify().valid
    finally:
        coord.close()


async def test_unknown_outcome_reconciled_to_success(tmp_config):
    """F05/F10-adjacent: unknown outcome where the server actually acted.

    The coordinator must query by intent_id and commit the discovered effect
    WITHOUT re-issuing the charge.
    """
    plan = _plan(tmp_config)
    injector = FailureInjector([
        FailureRule(mode="unknown", step_id="step-4", effect_applied=True)
    ])
    reg = AdapterRegistry(injector=injector)
    coord = TransactionCoordinator(plan, reg, config=tmp_config, backoff_scale=0.0)
    try:
        run = await coord.run_workflow()
        # Payment resolved to success via reconciliation; run completes.
        assert len(reg.world.payments) == 1  # no duplicate charge
        assert list(reg.world.payments.values())[0]["status"] == "captured"
        step4 = next(s for s in run.step_executions if s.step_id == "step-4")
        assert step4.reconciled is True
        assert step4.outcome == Outcome.SUCCESS
        assert run.state == WorkflowState.COMPLETED
        assert coord.audit.verify().valid
    finally:
        coord.close()


async def test_unknown_outcome_no_effect_then_recover(tmp_config):
    """Unknown outcome where the server did NOT act -> safe to fail + compensate."""
    plan = _plan(tmp_config)
    injector = FailureInjector([
        FailureRule(mode="unknown", step_id="step-4", effect_applied=False)
    ])
    reg = AdapterRegistry(injector=injector)
    coord = TransactionCoordinator(plan, reg, config=tmp_config, backoff_scale=0.0)
    try:
        run = await coord.run_workflow()
        # No payment ever committed; pre-pivot compensation runs.
        assert len(reg.world.payments) == 0
        assert run.pivot_crossed is False
        assert run.state == WorkflowState.ABORTED
        # Flight/hotel/car cancelled.
        assert list(reg.world.flights.values())[0]["status"] == "cancelled"
        assert coord.audit.verify().valid
    finally:
        coord.close()


async def test_phantom_success_detected(tmp_config):
    """F06: tool claims success but no real effect -> postcondition fails."""
    plan = _plan(tmp_config)
    injector = FailureInjector([
        FailureRule(mode="phantom_success", step_id="step-2")
    ])
    reg = AdapterRegistry(injector=injector)
    coord = TransactionCoordinator(plan, reg, config=tmp_config, backoff_scale=0.0)
    try:
        run = await coord.run_workflow()
        step2 = next(s for s in run.step_executions if s.step_id == "step-2")
        assert step2.postcondition is not None
        assert step2.postcondition.passed is False
        assert step2.outcome == Outcome.FAILURE
        # Flight (step-1) compensated; no hotel created.
        assert list(reg.world.flights.values())[0]["status"] == "cancelled"
        assert run.state == WorkflowState.ABORTED
        assert coord.audit.verify().valid
    finally:
        coord.close()
