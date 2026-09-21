"""Integration tests: resume-forward after crash recovery (Feature: resume-forward).

Recovery alone only reconciles the crashed intent. Resume-forward goes further:
after reconciliation it rebuilds coordinator state from the WAL + checkpoint,
SKIPS already-committed steps (their side effects are durable), and drives the
REMAINING steps to completion - the DBOS ``fork_workflow``/resume pattern.
"""

from __future__ import annotations

import uuid as _uuid

import pytest

from aatm.adapters import AdapterRegistry, CrashSignal, FailureInjector, FailureRule
from aatm.enums import AuditEvent, Outcome, WALStatus, WorkflowState
from aatm.engine import AATMEngine
from aatm.planner import SagaPlanner, WorkflowParser
from aatm.runtime import TransactionCoordinator

pytestmark = pytest.mark.asyncio

WORKFLOW = "workflows/order_fulfillment.yaml"


def _plan(tmp_config):
    parsed = WorkflowParser(tmp_config).parse(WORKFLOW)
    return SagaPlanner(tmp_config).plan(parsed)


async def _crash_after_step1(tmp_config, run_id):
    """Run order_fulfillment but crash right after step-1's side effect commits."""
    world_path = tmp_config.world_state_path(str(run_id))
    injector = FailureInjector([
        FailureRule(mode="crash", step_id="step-1", phase="after")
    ])
    reg = AdapterRegistry(injector=injector, world_persist_path=world_path)
    coord = TransactionCoordinator(_plan(tmp_config), reg, config=tmp_config,
                                   run_id=run_id, backoff_scale=0.0)
    try:
        with pytest.raises(CrashSignal) as exc:
            await coord.run_workflow()
        assert exc.value.effect_applied is True
        # The order record was created before the crash.
        assert len(reg.world.crm_records) == 1
        # WAL entry for step-1 is still non-terminal (never marked COMMITTED).
        entries = coord.wal.entries_for_run(run_id)
        s1 = [e for e in entries if e.step_id == "step-1"][-1]
        assert s1.status in (WALStatus.RUNNING.value, WALStatus.PENDING.value)
    finally:
        coord.close()


async def test_resume_forward_completes_remaining_steps(tmp_config):
    """After a crash post step-1, resume reconciles it then finishes step-2 + step-3."""
    run_id = _uuid.uuid4()
    await _crash_after_step1(tmp_config, run_id)

    engine = AATMEngine(tmp_config)
    report, output = await engine.resume(WORKFLOW, run_id, backoff_scale=0.0)

    # Recovery reconciled the crashed step-1 intent (found the durable effect).
    assert report.pending_found >= 1
    assert report.duplicates_prevented >= 1

    run = output.run
    # The run now COMPLETES - forward progress past the crash point.
    assert run.state == WorkflowState.COMPLETED
    assert run.pivot_crossed is True

    # step-1 was NOT re-executed: still exactly one order record (no duplicate).
    reg = output.registry
    order_records = [r for r in reg.world.crm_records.values()
                     if r["data"].get("sku") == "WIDGET-42"]
    assert len(order_records) == 1

    # step-2 (charge) + step-3 (crm sync) ran during resume.
    assert len(reg.world.payments) == 1
    assert list(reg.world.payments.values())[0]["status"] == "captured"
    # Two CRM records now: the resumed order + the new sync.
    assert len(reg.world.crm_records) == 2

    # step-1 appears as a resumed/skipped step in the evidence.
    s1 = next(s for s in run.step_executions if s.step_id == "step-1")
    assert s1.outcome == Outcome.SUCCESS
    assert "resumed" in s1.detail.lower()

    # Audit chain across BOTH sessions remains valid and records the resume.
    events = [e["event"] for e in _audit_entries(tmp_config, run_id)]
    assert AuditEvent.WORKFLOW_RESUMED.value in events
    assert AuditEvent.STEP_SKIPPED_RESUME.value in events
    assert AuditEvent.WORKFLOW_COMPLETE.value in events


async def test_resume_after_pivot_crash_does_not_double_charge(tmp_config):
    """CRITICAL: crash right after the charge (pivot) commits, then resume.

    Reconciliation discovers the existing charge by intent_id; resume-forward
    treats the pivot as already-committed and finishes step-3 WITHOUT issuing a
    second charge. This is the core safety guarantee of forward resume.
    """
    run_id = _uuid.uuid4()
    world_path = tmp_config.world_state_path(str(run_id))
    injector = FailureInjector([
        FailureRule(mode="crash", step_id="step-2", phase="after")
    ])
    reg = AdapterRegistry(injector=injector, world_persist_path=world_path)
    coord = TransactionCoordinator(_plan(tmp_config), reg, config=tmp_config,
                                   run_id=run_id, backoff_scale=0.0)
    try:
        with pytest.raises(CrashSignal) as exc:
            await coord.run_workflow()
        assert exc.value.effect_applied is True
        assert len(reg.world.payments) == 1  # charge committed before crash
    finally:
        coord.close()

    engine = AATMEngine(tmp_config)
    report, output = await engine.resume(WORKFLOW, run_id, backoff_scale=0.0)

    # The pivot charge was reconciled, not re-issued.
    assert report.duplicates_prevented >= 1
    assert len(output.registry.world.payments) == 1, "must NOT double-charge"
    assert len(output.registry.world.refunds) == 0

    run = output.run
    assert run.pivot_crossed is True
    assert run.state == WorkflowState.COMPLETED
    # step-2 (pivot) skipped as resumed; step-3 completed fresh.
    s2 = next(s for s in run.step_executions if s.step_id == "step-2")
    assert "resumed" in s2.detail.lower()
    assert len(output.registry.world.crm_records) == 2


async def test_resume_is_idempotent_when_nothing_pending(tmp_config):
    """Resuming an already-complete run is a no-op that stays COMPLETED."""
    engine = AATMEngine(tmp_config)
    run_id = _uuid.uuid4()
    out = await engine.run(WORKFLOW, run_id=run_id, backoff_scale=0.0)
    assert out.run.state == WorkflowState.COMPLETED
    payments_before = len(out.registry.world.payments)

    # Resume again: everything already committed, so all steps are skipped.
    _report, output = await engine.resume(WORKFLOW, run_id, backoff_scale=0.0)
    assert output.run.state == WorkflowState.COMPLETED
    # No duplicate side effects.
    assert len(output.registry.world.payments) == payments_before
    assert all("resumed" in s.detail.lower()
               for s in output.run.step_executions)


def _audit_entries(config, run_id):
    from aatm.storage.audit_log import AuditLog

    log = AuditLog(config.audit_log_path(str(run_id)),
                   secret_key=config.audit_hmac_key)
    entries = log.entries()
    assert log.verify().valid
    return entries
