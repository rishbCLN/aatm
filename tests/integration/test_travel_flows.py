"""Integration tests: full coordinator runs against mock adapters."""

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


def _coordinator(tmp_config, plan, injector=None, approval=None):
    registry = AdapterRegistry(injector=injector or FailureInjector())
    return TransactionCoordinator(
        plan, registry, config=tmp_config, approval_callback=approval,
        backoff_scale=0.0,
    )


async def test_travel_happy_path(tmp_config):
    plan = _plan(tmp_config)
    coord = _coordinator(tmp_config, plan)
    try:
        run = await coord.run_workflow()
        assert run.state == WorkflowState.COMPLETED
        assert run.completed_step_ids == [
            "step-1", "step-2", "step-3", "step-4", "step-5", "step-6", "step-7"
        ]
        # World state: one flight, hotel, car, payment captured, email, crm.
        w = coord.registry.world
        assert len(w.flights) == 1
        assert len(w.hotels) == 1
        assert len(w.cars) == 1
        assert len(w.payments) == 1
        assert list(w.payments.values())[0]["status"] == "captured"
        assert len(w.emails) == 1
        assert len(w.crm_records) == 1
        # No compensations on the happy path.
        assert run.compensations == []
        # Audit chain valid.
        assert coord.audit.verify().valid
    finally:
        coord.close()


async def test_failure_at_step2_compensates_step1(tmp_config):
    plan = _plan(tmp_config)
    injector = FailureInjector([
        FailureRule(mode="error", step_id="step-2",
                    failure_class="business_rule", message="HOTEL_FULL")
    ])
    coord = _coordinator(tmp_config, plan, injector=injector)
    try:
        run = await coord.run_workflow()
        assert run.state == WorkflowState.ABORTED
        w = coord.registry.world
        # Flight was booked then cancelled.
        assert len(w.flights) == 1
        assert list(w.flights.values())[0]["status"] == "cancelled"
        # No hotel created; no payment.
        assert len(w.payments) == 0
        # Exactly one compensation (cancel_flight).
        assert len(run.compensations) == 1
        assert coord.audit.verify().valid
    finally:
        coord.close()


async def test_failure_at_step3_compensates_step2_then_step1(tmp_config):
    plan = _plan(tmp_config)
    injector = FailureInjector([
        FailureRule(mode="error", step_id="step-3",
                    failure_class="resource_unavailable", message="CAR_UNAVAILABLE")
    ])
    coord = _coordinator(tmp_config, plan, injector=injector)
    try:
        run = await coord.run_workflow()
        assert run.state == WorkflowState.ABORTED
        w = coord.registry.world
        assert list(w.flights.values())[0]["status"] == "cancelled"
        assert list(w.hotels.values())[0]["status"] == "cancelled"
        assert len(w.cars) == 0
        assert len(w.payments) == 0
        # Two compensations in reverse completion order: hotel then flight.
        assert len(run.compensations) == 2
        assert run.compensations[0].source_step_id == "step-2"
        assert run.compensations[1].source_step_id == "step-1"
        assert coord.audit.verify().valid
    finally:
        coord.close()


async def test_approval_denied_at_pivot(tmp_config):
    plan = _plan(tmp_config)
    # Deny approval for the payment pivot (step-4).
    def deny(step):
        return step.step_id != "step-4"
    coord = _coordinator(tmp_config, plan, approval=deny)
    try:
        run = await coord.run_workflow()
        # No payment; previous reversible work compensated.
        w = coord.registry.world
        assert len(w.payments) == 0
        assert list(w.flights.values())[0]["status"] == "cancelled"
        assert list(w.hotels.values())[0]["status"] == "cancelled"
        assert list(w.cars.values())[0]["status"] == "cancelled"
        assert run.state == WorkflowState.ABORTED
        assert coord.audit.verify().valid
    finally:
        coord.close()
