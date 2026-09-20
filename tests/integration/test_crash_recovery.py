"""Integration tests: crash during payment + WAL replay recovery.

These simulate a process crash mid-run, then a fresh restart that replays the WAL
and reconciles by intent_id - never issuing a duplicate side effect.
"""

from __future__ import annotations

import pytest

from aatm.adapters import AdapterRegistry, CrashSignal, FailureInjector, FailureRule
from aatm.enums import WALStatus, WorkflowState
from aatm.planner import SagaPlanner, WorkflowParser
from aatm.runtime import RecoveryManager, TransactionCoordinator

pytestmark = pytest.mark.asyncio


def _plan(tmp_config, path="workflows/travel_booking.yaml"):
    parsed = WorkflowParser(tmp_config).parse(path)
    return SagaPlanner(tmp_config).plan(parsed)


async def test_crash_after_payment_then_recover_no_duplicate(tmp_config):
    """F10: crash AFTER the charge side effect, before response.

    Restart with a FRESH registry (the crashed process's in-memory world is
    gone). The external payment system's durable state is modeled by the
    persisted world file. WAL replay finds the pending payment intent, queries by
    intent_id, discovers the charge succeeded, commits WITHOUT re-charging.
    """
    import uuid as _uuid

    run_id = _uuid.uuid4()
    world_path = tmp_config.world_state_path(str(run_id))

    injector = FailureInjector([
        FailureRule(mode="crash", tool="charge_payment", phase="after")
    ])
    reg = AdapterRegistry(injector=injector, world_persist_path=world_path)
    coord = TransactionCoordinator(plan_and_reg := _plan(tmp_config), reg,
                                   config=tmp_config, run_id=run_id,
                                   backoff_scale=0.0)
    try:
        with pytest.raises(CrashSignal) as exc:
            await coord.run_workflow()
        assert exc.value.effect_applied is True
        assert len(reg.world.payments) == 1
        entries = reg_wal_entries(coord)
        pay_entries = [e for e in entries if e.tool == "charge_payment"]
        assert pay_entries
        assert pay_entries[-1].status in (
            WALStatus.RUNNING.value, WALStatus.PENDING.value
        )
    finally:
        coord.close()

    # --- Restart: FRESH registry that reloads authoritative external state.
    fresh_reg = AdapterRegistry(world_persist_path=world_path)
    assert len(fresh_reg.world.payments) == 1  # external system persisted it
    recovery = RecoveryManager(run_id, fresh_reg, config=tmp_config)
    try:
        report = await recovery.recover()
    finally:
        recovery.close()

    # Duplicate prevented: still exactly one payment.
    assert len(fresh_reg.world.payments) == 1
    assert report.pending_found >= 1
    assert report.duplicates_prevented >= 1
    committed = [r for r in report.reconciliations if r.resolution == "committed"]
    assert any(r.tool == "charge_payment" for r in committed)


async def test_crash_before_payment_no_effect(tmp_config):
    """F09: crash BEFORE the charge side effect.

    Restart -> WAL replay finds pending payment intent -> query finds NOTHING ->
    marks FAILED (safe). No charge exists; no duplicate is produced.
    """
    plan = _plan(tmp_config)
    injector = FailureInjector([
        FailureRule(mode="crash", step_id="step-4", phase="before")
    ])
    reg = AdapterRegistry(injector=injector)
    coord = TransactionCoordinator(plan, reg, config=tmp_config, backoff_scale=0.0)
    run_id = coord.run_id
    try:
        with pytest.raises(CrashSignal) as exc:
            await coord.run_workflow()
        assert exc.value.effect_applied is False
        assert len(reg.world.payments) == 0
    finally:
        coord.close()

    recovery = RecoveryManager(run_id, reg, config=tmp_config)
    try:
        report = await recovery.recover()
    finally:
        recovery.close()

    # No payment was ever created; recovery marks the intent failed, no dup.
    assert len(reg.world.payments) == 0
    failed = [r for r in report.reconciliations if r.resolution == "failed"]
    assert any(r.tool == "charge_payment" for r in failed)


def reg_wal_entries(coord):
    """Helper: read all WAL entries for the coordinator's run."""
    return coord.wal.entries_for_run(coord.run_id)
