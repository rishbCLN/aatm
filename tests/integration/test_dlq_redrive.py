"""Integration tests: dead-letter redrive (Feature: DLQ redrive).

When a run leaves an unrecoverable item in the dead-letter queue (a failed
compensation, or a post-pivot retryable step escalated forward), an operator can
later re-attempt that work with :meth:`AATMEngine.redrive`. Redrive is:

  * dedup-safe   - a compensation carries its STABLE comp ``intent_id``; if the
                   effect already happened server-side, the adapter replays the
                   recorded result instead of duplicating it.
  * append-only  - a resolved entry is marked with a resolution record; history
                   (and the audit chain) is never mutated. An entry that still
                   fails stays OPEN for a later attempt.
  * authoritative - it reloads the persisted world + WAL for the run, so it acts
                    on real durable state, not in-memory test scaffolding.
"""

from __future__ import annotations

import uuid as _uuid

from aatm.adapters import AdapterRegistry, FailureInjector, FailureRule
from aatm.enums import AuditEvent, WorkflowState
from aatm.engine import AATMEngine
from aatm.planner import SagaPlanner, WorkflowParser
from aatm.runtime import TransactionCoordinator

# asyncio_mode="auto" auto-detects the async tests; no module-level mark needed.

ORDER_WF = "workflows/order_fulfillment.yaml"
TRAVEL_WF = "workflows/travel_booking.yaml"


def _plan(tmp_config, path):
    parsed = WorkflowParser(tmp_config).parse(path)
    return SagaPlanner(tmp_config).plan(parsed)


def _audit(config, run_id):
    from aatm.storage.audit_log import AuditLog

    log = AuditLog(config.audit_log_path(str(run_id)),
                   secret_key=config.audit_hmac_key)
    verify = log.verify()
    assert verify.valid, "audit chain must stay valid across redrive"
    return [e["event"] for e in log.entries()]


async def _run_order_escalation(tmp_config, run_id):
    """Drive order_fulfillment so step-3 (retryable) exhausts and dead-letters."""
    world_path = tmp_config.world_state_path(str(run_id))
    injector = FailureInjector([
        FailureRule(mode="error", step_id="step-3", message="CRM_DOWN")
    ])
    reg = AdapterRegistry(injector=injector, world_persist_path=world_path)
    coord = TransactionCoordinator(_plan(tmp_config, ORDER_WF), reg,
                                   config=tmp_config, run_id=run_id,
                                   backoff_scale=0.0)
    try:
        run = await coord.run_workflow()
        assert run.state == WorkflowState.INCONSISTENT
        assert coord.dead_letter.open_count() == 1
        entry = coord.dead_letter.open_entries()[0]
        assert entry["step_id"] == "step-3"
        assert entry["strategy"] == "forward_retry"
        assert entry["intent_id"] is None  # forward escalation: no comp intent
        # Charge stands; the CRM sync never happened (only the order record exists).
        assert len(reg.world.payments) == 1
        assert len(reg.world.crm_records) == 1
    finally:
        coord.close()


async def test_redrive_resolves_forward_escalation(tmp_config):
    """An escalated forward step is re-driven forward and the DLQ entry clears."""
    run_id = _uuid.uuid4()
    await _run_order_escalation(tmp_config, run_id)

    engine = AATMEngine(tmp_config)
    report = await engine.redrive(run_id)

    assert report.open_before == 1
    assert report.resolved == 1
    assert report.still_failed == 0
    assert report.attempts[0].step_id == "step-3"
    assert report.attempts[0].tool == "update_crm"
    assert report.attempts[0].resolution == "resolved"

    # The dead-letter queue is now drained for this run.
    from aatm.storage.dead_letter import DeadLetterQueue
    dlq = DeadLetterQueue(tmp_config.dead_letter_path(str(run_id)))
    assert dlq.open_count() == 0
    assert dlq.count() == 1  # original entry preserved (append-only)

    # The forward action actually ran against the persisted world: the CRM sync
    # record now exists alongside the original order (no double charge).
    reg = AdapterRegistry(world_persist_path=tmp_config.world_state_path(str(run_id)))
    assert len(reg.world.payments) == 1
    assert len(reg.world.crm_records) == 2

    events = _audit(tmp_config, run_id)
    assert AuditEvent.REDRIVE_START.value in events
    assert AuditEvent.REDRIVE_RESULT.value in events


async def test_redrive_is_noop_when_queue_empty(tmp_config):
    """Redriving a clean, completed run resolves nothing and reports empty."""
    engine = AATMEngine(tmp_config)
    run_id = _uuid.uuid4()
    out = await engine.run(ORDER_WF, run_id=run_id, backoff_scale=0.0)
    assert out.run.state == WorkflowState.COMPLETED

    report = await engine.redrive(run_id)
    assert report.open_before == 0
    assert report.resolved == 0
    assert report.still_failed == 0
    assert report.attempts == []


async def test_redrive_reexecutes_failed_compensation_dedup_safe(tmp_config):
    """A failed compensation is re-driven using its STABLE comp intent_id.

    travel_booking: step-3 (reserve_car) fails pre-pivot, so hotel+flight are
    compensated. cancel_hotel is injected to fail, dead-lettering the hotel
    compensation with its comp intent_id. Redrive (no injector) re-executes that
    exact intent against the persisted world -> the hotel is cancelled for real.
    """
    run_id = _uuid.uuid4()
    world_path = tmp_config.world_state_path(str(run_id))
    injector = FailureInjector([
        FailureRule(mode="error", step_id="step-3",
                    failure_class="resource_unavailable", message="CAR_UNAVAILABLE"),
        FailureRule(mode="error", tool="cancel_hotel",
                    failure_class="server_error", message="CANCEL_HOTEL_DOWN"),
    ])
    reg = AdapterRegistry(injector=injector, world_persist_path=world_path)
    coord = TransactionCoordinator(_plan(tmp_config, TRAVEL_WF), reg,
                                   config=tmp_config, run_id=run_id,
                                   backoff_scale=0.0)
    try:
        run = await coord.run_workflow()
        assert run.state == WorkflowState.INCONSISTENT
        # The hotel comp was dead-lettered with a real comp intent_id.
        entry = coord.dead_letter.open_entries()[0]
        assert entry["step_id"] == "step-2"
        assert entry["intent_id"] is not None
        # The hotel is still confirmed (its cancellation failed).
        hotels = list(reg.world.hotels.values())
        assert len(hotels) == 1
        assert hotels[0]["status"] == "confirmed"
    finally:
        coord.close()

    engine = AATMEngine(tmp_config)
    report = await engine.redrive(run_id)

    assert report.open_before == 1
    assert report.resolved == 1
    assert report.still_failed == 0
    assert report.attempts[0].tool == "cancel_hotel"

    # Re-loading the world proves the hotel was actually cancelled by the redrive.
    reg2 = AdapterRegistry(world_persist_path=world_path)
    hotels = list(reg2.world.hotels.values())
    assert hotels[0]["status"] == "cancelled"
    # No duplicate hotel booking/cancel was created.
    assert len(reg2.world.hotels) == 1
    _audit(tmp_config, run_id)


async def test_redrive_leaves_entry_open_when_it_still_fails(tmp_config):
    """If a redrive attempt fails, the entry stays OPEN for a later attempt.

    A subsequent clean redrive then resolves it - proving redrive is repeatable
    and never loses the work item (append-only DLQ).
    """
    run_id = _uuid.uuid4()
    await _run_order_escalation(tmp_config, run_id)

    engine = AATMEngine(tmp_config)
    # First redrive with an injector that keeps update_crm failing.
    failing_reg = AdapterRegistry(
        injector=FailureInjector([
            FailureRule(mode="error", tool="update_crm", message="STILL_DOWN")
        ]),
        world_persist_path=tmp_config.world_state_path(str(run_id)),
    )
    report1 = await engine.redrive(run_id, registry=failing_reg)
    assert report1.open_before == 1
    assert report1.resolved == 0
    assert report1.still_failed == 1
    assert report1.attempts[0].resolution == "failed"

    # Entry is still open - the work was not lost.
    from aatm.storage.dead_letter import DeadLetterQueue
    dlq = DeadLetterQueue(tmp_config.dead_letter_path(str(run_id)))
    assert dlq.open_count() == 1

    # Second redrive, clean this time, resolves it.
    report2 = await engine.redrive(run_id)
    assert report2.open_before == 1
    assert report2.resolved == 1
    assert dlq.open_count() == 0
    _audit(tmp_config, run_id)
