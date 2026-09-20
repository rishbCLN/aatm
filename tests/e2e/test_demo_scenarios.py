"""End-to-end tests: full demo scenarios A, B, C via the engine + CLI.

These run the real engine against mock adapters with no network/credentials.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from aatm.engine import AATMEngine
from aatm.enums import WorkflowState

pytestmark = pytest.mark.asyncio

WF = "workflows/travel_booking.yaml"


async def test_demo_case_a_failure_before_pivot(tmp_config):
    """Demo A: car unavailable at step 3 -> compensate hotel then flight."""
    engine = AATMEngine(tmp_config)
    out = await engine.run(WF, injection_path="injections/step3_failure.yaml",
                           backoff_scale=0.0)
    assert out.run.state == WorkflowState.ABORTED
    w = out.registry.world
    assert list(w.flights.values())[0]["status"] == "cancelled"
    assert list(w.hotels.values())[0]["status"] == "cancelled"
    assert len(w.cars) == 0
    assert len(w.payments) == 0
    # Compensation order: hotel (step-2) then flight (step-1).
    assert [c.source_step_id for c in out.run.compensations] == ["step-2", "step-1"]

    report = engine.report(out, experiment={"case": "A"})
    assert report["score"]["status"] in ("PASS", "PASS_WITH_CONDITIONS")
    assert report["result"]["consistent"] is True
    assert Path(report["_paths"]["html"]).exists()


async def test_demo_case_b_failure_after_pivot(tmp_config):
    """Demo B: timeout at step 5 after payment -> refund + cancellations."""
    engine = AATMEngine(tmp_config)
    out = await engine.run(WF, injection_path="injections/post_pivot_timeout.yaml",
                           backoff_scale=0.0)
    assert out.run.pivot_crossed is True
    w = out.registry.world
    assert len(w.payments) == 1
    assert len(w.refunds) == 1  # refund is a NEW transaction
    assert list(w.payments.values())[0]["status"] == "refunded"

    report = engine.report(out, experiment={"case": "B"})
    assert report["result"]["exact_rollback_possible"] is False
    html = Path(report["_paths"]["html"]).read_text(encoding="utf-8")
    assert "new transaction" in html.lower()


async def test_demo_case_c_crash_during_payment(tmp_config):
    """Demo C: crash after charge dispatched -> restart reconciles, no dup."""
    engine = AATMEngine(tmp_config)
    run_id = uuid.uuid4()
    out = await engine.run(WF, injection_path="injections/crash_mid_payment.yaml",
                           run_id=run_id, backoff_scale=0.0)
    assert out.crashed is True
    # External system persisted exactly one payment.
    world_path = tmp_config.world_state_path(str(run_id))
    assert world_path.exists()

    # Restart: fresh registry loads authoritative external state; reconcile.
    report, reg = await engine.recover(run_id)
    assert len(reg.world.payments) == 1  # no duplicate charge
    assert report.duplicates_prevented >= 1
    committed = [r for r in report.reconciliations if r.resolution == "committed"]
    assert any(r.tool == "charge_payment" for r in committed)


async def test_happy_path_audit_and_report(tmp_config):
    engine = AATMEngine(tmp_config)
    out = await engine.run(WF, backoff_scale=0.0)
    assert out.run.state == WorkflowState.COMPLETED
    report = engine.report(out)
    assert report["result"]["audit_chain_valid"] is True
    assert report["score"]["total"] == 100.0
