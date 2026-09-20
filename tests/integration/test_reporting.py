"""Unit tests for scoring + report generation (JSON schema + HTML render)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aatm.adapters import AdapterRegistry, FailureInjector, FailureRule
from aatm.enums import ReportStatus, WorkflowState
from aatm.planner import SagaPlanner, WorkflowParser
from aatm.reporting import EvidenceReport, Scorer
from aatm.runtime import TransactionCoordinator
from aatm.storage.audit_log import AuditLog

pytestmark = pytest.mark.asyncio

try:
    import jsonschema
    _HAS_JSONSCHEMA = True
except Exception:
    _HAS_JSONSCHEMA = False


def _plan(tmp_config):
    parsed = WorkflowParser(tmp_config).parse("workflows/travel_booking.yaml")
    return SagaPlanner(tmp_config).plan(parsed)


async def _run(tmp_config, injector=None, approval=None):
    plan = _plan(tmp_config)
    reg = AdapterRegistry(injector=injector or FailureInjector())
    coord = TransactionCoordinator(plan, reg, config=tmp_config,
                                   approval_callback=approval, backoff_scale=0.0)
    run = await coord.run_workflow()
    coord.close()
    return run, plan, reg


async def test_happy_path_report_pass(tmp_config):
    run, plan, reg = await _run(tmp_config)
    reporter = EvidenceReport(tmp_config)
    report = reporter.generate(run, plan=plan, world_summary=reg.world.summary())
    assert report["score"]["status"] == ReportStatus.PASS.value
    assert report["score"]["total"] == 100.0
    assert report["result"]["audit_chain_valid"] is True
    # HTML + JSON files exist and are non-trivial.
    assert Path(report["_paths"]["html"]).stat().st_size > 2000
    assert Path(report["_paths"]["json"]).exists()
    # No unresolved template placeholders.
    html = Path(report["_paths"]["html"]).read_text(encoding="utf-8")
    assert "{{" not in html and "{%" not in html


async def test_post_pivot_report_marks_no_exact_rollback(tmp_config):
    injector = FailureInjector([
        FailureRule(mode="timeout", step_id="step-5", as_unknown=False,
                    until_attempt=5)
    ])
    run, plan, reg = await _run(tmp_config, injector=injector)
    reporter = EvidenceReport(tmp_config)
    report = reporter.generate(run, plan=plan, world_summary=reg.world.summary())
    assert report["result"]["pivot_crossed"] is True
    assert report["result"]["exact_rollback_possible"] is False
    assert report["score"]["floor_violations"] == []
    # The report explicitly explains the pivot refund semantics.
    html = Path(report["_paths"]["html"]).read_text(encoding="utf-8")
    assert "NEW transaction" in html or "new transaction" in html


async def test_inconsistent_report_status(tmp_config):
    injector = FailureInjector([
        FailureRule(mode="error", step_id="step-3",
                    failure_class="resource_unavailable", message="X"),
        FailureRule(mode="error", tool="cancel_hotel",
                    failure_class="server_error", message="DOWN"),
    ])
    run, plan, reg = await _run(tmp_config, injector=injector)
    reporter = EvidenceReport(tmp_config)
    report = reporter.generate(run, plan=plan, world_summary=reg.world.summary())
    assert report["result"]["state"] == WorkflowState.INCONSISTENT.value
    assert report["score"]["status"] == ReportStatus.INCONSISTENT.value


@pytest.mark.skipif(not _HAS_JSONSCHEMA, reason="jsonschema not installed")
async def test_result_validates_against_schema(tmp_config):
    run, plan, reg = await _run(tmp_config)
    reporter = EvidenceReport(tmp_config)
    report = reporter.generate(run, plan=plan, world_summary=reg.world.summary())
    schema = json.loads(
        (Path("schemas") / "result.schema.json").read_text(encoding="utf-8")
    )
    import jsonschema
    jsonschema.validate(report["result"], schema)  # raises on failure


async def test_report_never_claims_certification(tmp_config):
    run, plan, reg = await _run(tmp_config)
    reporter = EvidenceReport(tmp_config)
    report = reporter.generate(run, plan=plan, world_summary=reg.world.summary())
    html = Path(report["_paths"]["html"]).read_text(encoding="utf-8").lower()
    for banned in ["regulator-approved", "regulator approved", "legally compliant",
                   "zero risk", "guaranteed safe"]:
        assert banned not in html
