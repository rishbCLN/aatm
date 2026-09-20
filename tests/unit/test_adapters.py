"""Unit tests for mock adapters, world state, and failure injection."""

from __future__ import annotations

import pytest
from uuid import uuid4

from aatm.adapters import (
    AdapterRegistry,
    CrashSignal,
    FailureInjector,
    FailureRule,
    MockWorldState,
)
from aatm.enums import FailureClass, Outcome
from aatm.models import ActionIntent

pytestmark = pytest.mark.asyncio


def _intent(run_id, step_id, tool, params=None):
    return ActionIntent(
        run_id=run_id, workflow_id="wf-1", step_id=step_id,
        tool_name=tool, parameters=params or {},
    )


async def test_book_and_cancel_flight():
    reg = AdapterRegistry()
    run_id = uuid4()
    adapter = reg.get("book_flight")
    intent = _intent(run_id, "step-1", "book_flight", {"user_id": "emp-1"})
    result = await adapter.execute(intent)
    assert result.is_success
    booking_id = result.data["booking_id"]
    assert reg.world.flights[booking_id]["status"] == "confirmed"

    # Cancel it (semantic inverse).
    cancel = _intent(run_id, "comp-1", "cancel_flight", {"booking_id": booking_id})
    cres = await reg.get("cancel_flight").execute(cancel)
    assert cres.is_success
    assert reg.world.flights[booking_id]["status"] == "cancelled"


async def test_charge_payment_is_idempotent_by_intent():
    reg = AdapterRegistry()
    run_id = uuid4()
    adapter = reg.get("charge_payment")
    intent = _intent(run_id, "step-4", "charge_payment", {"amount": 45200})

    r1 = await adapter.execute(intent)
    r2 = await adapter.execute(intent)  # retry same intent
    assert r1.is_success and r2.is_success
    # Same transaction id -> no double charge.
    assert r1.data["transaction_id"] == r2.data["transaction_id"]
    assert len(reg.world.payments) == 1


async def test_refund_is_new_transaction_not_undo():
    reg = AdapterRegistry()
    run_id = uuid4()
    charge = _intent(run_id, "step-4", "charge_payment", {"amount": 100})
    cres = await reg.get("charge_payment").execute(charge)
    txn_id = cres.data["transaction_id"]

    refund = _intent(run_id, "comp-4", "refund_payment", {"transaction_id": txn_id})
    rres = await reg.get("refund_payment").execute(refund)
    assert rres.is_success
    # A separate refund record exists; the original payment still exists.
    assert len(reg.world.refunds) == 1
    assert txn_id in reg.world.payments
    assert reg.world.payments[txn_id]["refunded"] is True


async def test_injected_error():
    injector = FailureInjector([
        FailureRule(mode="error", step_id="step-3",
                    failure_class="resource_unavailable", message="CAR_UNAVAILABLE")
    ])
    reg = AdapterRegistry(injector=injector)
    run_id = uuid4()
    intent = _intent(run_id, "step-3", "reserve_car", {})
    result = await reg.get("reserve_car").execute(intent)
    assert result.is_failure
    assert result.failure_class == FailureClass.RESOURCE_UNAVAILABLE
    assert result.error_message == "CAR_UNAVAILABLE"
    # No car created.
    assert len(reg.world.cars) == 0


async def test_injected_unknown_with_hidden_effect_discoverable():
    """Unknown outcome where the server actually acted -> query finds it."""
    injector = FailureInjector([
        FailureRule(mode="unknown", step_id="step-4", effect_applied=True)
    ])
    reg = AdapterRegistry(injector=injector)
    run_id = uuid4()
    intent = _intent(run_id, "step-4", "charge_payment", {"amount": 100})
    result = await reg.get("charge_payment").execute(intent)
    assert result.is_unknown
    # The payment WAS applied server-side; a status query discovers it.
    q = await reg.get("charge_payment").query_status(intent.intent_id)
    assert q.found is True
    assert q.outcome == Outcome.SUCCESS


async def test_injected_unknown_without_effect_not_found():
    injector = FailureInjector([
        FailureRule(mode="unknown", step_id="step-4", effect_applied=False)
    ])
    reg = AdapterRegistry(injector=injector)
    run_id = uuid4()
    intent = _intent(run_id, "step-4", "charge_payment", {"amount": 100})
    result = await reg.get("charge_payment").execute(intent)
    assert result.is_unknown
    q = await reg.get("charge_payment").query_status(intent.intent_id)
    assert q.found is False


async def test_phantom_success_fails_postcondition():
    injector = FailureInjector([
        FailureRule(mode="phantom_success", step_id="step-1")
    ])
    reg = AdapterRegistry(injector=injector)
    run_id = uuid4()
    intent = _intent(run_id, "step-1", "book_flight", {})
    result = await reg.get("book_flight").execute(intent)
    assert result.is_success  # claims success
    # But no real booking -> postcondition must fail.
    v = await reg.get("book_flight").verify_postcondition(
        intent, {"result": result.data}
    )
    assert v.passed is False


async def test_crash_before_side_effect():
    injector = FailureInjector([
        FailureRule(mode="crash", step_id="step-4", phase="before")
    ])
    reg = AdapterRegistry(injector=injector)
    run_id = uuid4()
    intent = _intent(run_id, "step-4", "charge_payment", {"amount": 100})
    with pytest.raises(CrashSignal) as exc:
        await reg.get("charge_payment").execute(intent)
    assert exc.value.effect_applied is False
    assert len(reg.world.payments) == 0


async def test_crash_after_side_effect():
    injector = FailureInjector([
        FailureRule(mode="crash", tool="charge_payment", phase="after")
    ])
    reg = AdapterRegistry(injector=injector)
    run_id = uuid4()
    intent = _intent(run_id, "step-4", "charge_payment", {"amount": 100})
    with pytest.raises(CrashSignal) as exc:
        await reg.get("charge_payment").execute(intent)
    assert exc.value.effect_applied is True
    # The payment WAS applied before the crash; recovery must discover it.
    assert len(reg.world.payments) == 1


async def test_transient_rate_limit_clears_after_attempt():
    injector = FailureInjector([
        FailureRule(mode="rate_limit", step_id="step-1", until_attempt=1)
    ])
    reg = AdapterRegistry(injector=injector)
    run_id = uuid4()
    intent = _intent(run_id, "step-1", "book_flight", {})
    r1 = await reg.get("book_flight").execute(intent)
    assert r1.is_failure and r1.failure_class == FailureClass.RATE_LIMIT
    # Second attempt (same step) should succeed.
    r2 = await reg.get("book_flight").execute(intent)
    assert r2.is_success


async def test_world_snapshot_and_restore():
    reg = AdapterRegistry()
    run_id = uuid4()
    await reg.get("book_flight").execute(_intent(run_id, "s1", "book_flight", {}))
    snap = reg.world.snapshot()
    assert len(snap["flights"]) == 1

    reg.world.flights.clear()
    assert len(reg.world.flights) == 0
    reg.world.restore(snap)
    assert len(reg.world.flights) == 1


async def test_unknown_tool_not_registered():
    reg = AdapterRegistry()
    assert reg.has("book_flight") is True
    assert reg.has("launch_missiles") is False
    assert reg.get("launch_missiles") is None
