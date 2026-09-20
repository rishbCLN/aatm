"""Property-based invariant tests (spec section 23.3).

Uses Hypothesis to generate small random linear sagas and verify the core safety
invariants A-G hold across many configurations.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from aatm.adapters import AdapterRegistry, FailureInjector, FailureRule
from aatm.config import AATMConfig
from aatm.enums import AuditEvent, Outcome, WorkflowState
from aatm.planner import SagaPlanner, WorkflowParser
from aatm.runtime import TransactionCoordinator

# Reversible/compensatable tool pool for generated pre-pivot sagas.
_TOOL_POOL = [
    ("book_flight", "flights"),
    ("book_hotel", "hotels"),
    ("reserve_car", "cars"),
    ("create_database_record", "crm_records"),
]


def _make_config(tmp_path_factory) -> AATMConfig:
    base = tmp_path_factory.mktemp("prop")
    cfg = AATMConfig(project_root=base)
    cfg.ensure_dirs()
    root = Path(__file__).resolve().parent.parent.parent
    cfg.knowledge_base_dir = root / "knowledge_base"
    cfg.schemas_dir = root / "schemas"
    return cfg


def _build_workflow(tool_names: list[str], fail_index: int | None) -> dict:
    steps = []
    for i, tool in enumerate(tool_names):
        step = {
            "id": f"step-{i+1}",
            "name": f"step-{i+1}",
            "tool": tool,
            "retry": {"max_attempts": 1, "retry_on": []},
        }
        if i > 0:
            step["depends_on"] = [f"step-{i}"]
        steps.append(step)
    return {
        "workflow": {"id": "wf-prop", "name": "Property Saga", "timeout_seconds": 60},
        "steps": steps,
    }


settings_profile = settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


class TestInvariants:
    @settings_profile
    @given(
        n=st.integers(min_value=1, max_value=4),
        fail_at=st.integers(min_value=0, max_value=4),
        seed=st.integers(min_value=0, max_value=10),
    )
    def test_invariants_linear_saga(self, tmp_path_factory, n, fail_at, seed):
        cfg = _make_config(tmp_path_factory)
        tools = [(_TOOL_POOL[(seed + i) % len(_TOOL_POOL)])[0] for i in range(n)]
        data = _build_workflow(tools, None)

        parsed = WorkflowParser(cfg).parse_dict(data)
        plan = SagaPlanner(cfg).plan(parsed)
        if not plan.is_valid:
            return  # skip invalid generated plans

        # Optionally inject a failure at a valid step index.
        injector = FailureInjector()
        fail_step = None
        if fail_at < n:
            fail_step = f"step-{fail_at+1}"
            injector.add_rule(
                FailureRule(mode="error", step_id=fail_step,
                            failure_class="business_rule", message="INJECTED")
            )

        reg = AdapterRegistry(injector=injector)
        coord = TransactionCoordinator(plan, reg, config=cfg, backoff_scale=0.0)
        try:
            run = asyncio.run(coord.run_workflow())
            audit_entries = coord.audit.entries()

            # Invariant A: no pre-intent side effect.
            self._check_invariant_A(audit_entries)
            # Invariant B: no duplicate non-idempotent effects.
            self._check_invariant_B(reg)
            # Invariant C: LIFO compensation for linear pre-pivot saga.
            self._check_invariant_C(run)
            # Invariant G: audit chain integrity.
            assert coord.audit.verify().valid
        finally:
            coord.close()

    def _check_invariant_A(self, entries):
        """For every ACTION_START (side effect begins), an ACTION_INTENT_CREATED
        for the same step must appear earlier in the log."""
        intents_seen: set[str] = set()
        for e in entries:
            if e["event"] == AuditEvent.ACTION_INTENT_CREATED.value:
                intents_seen.add(e["entity_id"])
            if e["event"] == AuditEvent.ACTION_START.value:
                assert e["entity_id"] in intents_seen, (
                    f"side effect for {e['entity_id']} started before durable intent"
                )

    def _check_invariant_B(self, reg):
        """No non-idempotent effect is duplicated: each side-effect entity id is
        unique in the world."""
        payments = list(reg.world.payments.keys())
        assert len(payments) == len(set(payments))
        # An intent may appear once per effect kind (book + cancel), which is fine;
        # what matters is no duplicate CREATE for the same intent.
        creates = [e for e in reg.world.side_effect_log
                   if e["kind"] in ("flight", "hotel", "car", "payment", "crm")]
        create_intents = [e["intent_id"] for e in creates]
        assert len(create_intents) == len(set(create_intents))

    def _check_invariant_C(self, run):
        """Completed compensations execute in reverse completion order."""
        comps = [c for c in run.compensations if c.outcome == Outcome.SUCCESS]
        # The source step ids should be in decreasing step order for a linear saga.
        step_nums = [int(c.source_step_id.split("-")[1])
                     for c in comps if c.source_step_id.startswith("step-")]
        assert step_nums == sorted(step_nums, reverse=True), (
            f"compensations not in reverse completion order: {step_nums}"
        )


class TestPivotHonesty:
    """Invariant D: once a Tier-3 action commits, exact rollback is not claimed."""

    def test_pivot_honesty(self, tmp_path_factory):
        cfg = _make_config(tmp_path_factory)
        parsed = WorkflowParser(cfg).parse(
            str(Path(cfg.knowledge_base_dir).parent / "workflows" / "travel_booking.yaml")
        )
        plan = SagaPlanner(cfg).plan(parsed)
        injector = FailureInjector([
            FailureRule(mode="timeout", step_id="step-5", as_unknown=False,
                        until_attempt=5)
        ])
        reg = AdapterRegistry(injector=injector)
        coord = TransactionCoordinator(plan, reg, config=cfg, backoff_scale=0.0)
        try:
            run = asyncio.run(coord.run_workflow())
            assert run.pivot_crossed is True
            # The engine must NOT claim exact rollback; refund is a new txn.
            assert "Exact rollback impossible" in run.detail
            assert len(reg.world.refunds) == 1
        finally:
            coord.close()


class TestUnknownIsNotFailed:
    """Invariant E: unknown outcome remains distinguishable from failure."""

    def test_unknown_distinct_from_failure(self, tmp_path_factory):
        cfg = _make_config(tmp_path_factory)
        parsed = WorkflowParser(cfg).parse(
            str(Path(cfg.knowledge_base_dir).parent / "workflows" / "travel_booking.yaml")
        )
        plan = SagaPlanner(cfg).plan(parsed)
        injector = FailureInjector([
            FailureRule(mode="unknown", step_id="step-4", effect_applied=False)
        ])
        reg = AdapterRegistry(injector=injector)
        coord = TransactionCoordinator(plan, reg, config=cfg, backoff_scale=0.0)
        try:
            asyncio.run(coord.run_workflow())
            events = {e["event"] for e in coord.audit.entries()}
            # An explicit ACTION_UNKNOWN event exists (not just ACTION_FAILED).
            assert AuditEvent.ACTION_UNKNOWN.value in events
        finally:
            coord.close()


class TestCompensationFailureVisible:
    """Invariant F: a failed compensation can never silently produce CONSISTENT."""

    def test_failed_compensation_is_visible(self, tmp_path_factory):
        cfg = _make_config(tmp_path_factory)
        parsed = WorkflowParser(cfg).parse(
            str(Path(cfg.knowledge_base_dir).parent / "workflows" / "travel_booking.yaml")
        )
        plan = SagaPlanner(cfg).plan(parsed)
        injector = FailureInjector([
            FailureRule(mode="error", step_id="step-3",
                        failure_class="resource_unavailable", message="X"),
            FailureRule(mode="error", tool="cancel_hotel",
                        failure_class="server_error", message="DOWN"),
        ])
        reg = AdapterRegistry(injector=injector)
        coord = TransactionCoordinator(plan, reg, config=cfg, backoff_scale=0.0)
        try:
            run = asyncio.run(coord.run_workflow())
            assert run.state == WorkflowState.INCONSISTENT
            failed_comps = [c for c in run.compensations
                            if c.outcome == Outcome.FAILURE]
            assert failed_comps  # visibly failed
        finally:
            coord.close()
