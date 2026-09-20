"""Integration tests for production-hardening features (items 1-12).

Each section is grouped by the hardening item it exercises so the suite doubles
as living documentation of the guarantees.
"""

from __future__ import annotations

import asyncio

import pytest

from aatm.adapters import AdapterRegistry, FailureInjector, FailureRule
from aatm.adapters.base import BaseAdapter
from aatm.enums import AuditEvent, Outcome, WorkflowState
from aatm.models import ToolResult
from aatm.planner import SagaPlanner, WorkflowParser
from aatm.runtime import TransactionCoordinator
from aatm.runtime.circuit_breaker import BreakerState, CircuitBreaker

# asyncio_mode = "auto" (see pyproject) runs async tests automatically; no mark
# needed. Sync tests in this module must NOT carry the asyncio mark.


def _plan(tmp_config, path="workflows/travel_booking.yaml"):
    parsed = WorkflowParser(tmp_config).parse(path)
    return SagaPlanner(tmp_config).plan(parsed)


# ---------------------------------------------------------------------------
# Item 1: per-step timeout enforcement
# ---------------------------------------------------------------------------


class _SlowCarAdapter(BaseAdapter):
    """reserve_car that applies the effect, then hangs past the step deadline.

    Models a request that reached the server (effect committed) but whose
    response is lost/slow. The per-step timeout must fire and the coordinator
    must reconcile by intent_id rather than double-booking.
    """

    tools = ("reserve_car", "cancel_car")

    async def _do_execute(self, intent):
        if intent.tool_name == "cancel_car":
            cars = self.world.cars
            for c in cars.values():
                c["status"] = "cancelled"
            return ToolResult(intent_id=intent.intent_id, outcome=Outcome.SUCCESS,
                              data={"status": "cancelled"})
        # reserve_car: record the effect FIRST (server acted), then hang.
        cid = self.world.next_id("CAR")
        data = {"reservation_id": cid, "booking_id": cid, "car_id": cid,
                "status": "confirmed", "booking_status": "confirmed"}
        self.world.cars[cid] = {"id": cid, "status": "confirmed", **data}
        self.world.record_effect(str(intent.intent_id), "car", cid, data)
        await asyncio.sleep(5.0)  # far beyond the (scaled) deadline
        return ToolResult(intent_id=intent.intent_id, outcome=Outcome.SUCCESS,
                          data=data)

    def _postcondition(self, intent, state):
        from aatm.models import VerificationResult
        return VerificationResult(passed=True, detail="ok")


async def test_per_step_timeout_routes_to_reconciliation(tmp_config):
    plan = _plan(tmp_config)
    # Only step-3 gets a tiny deadline; other steps keep their normal timeout.
    plan.step("step-3").timeout_ms = 20
    reg = AdapterRegistry()
    reg.register(_SlowCarAdapter(reg.world, reg.injector))

    coord = TransactionCoordinator(plan, reg, config=tmp_config, backoff_scale=0.0)
    try:
        run = await coord.run_workflow()
        step3 = next(s for s in run.step_executions if s.step_id == "step-3")
        # Timed out -> unknown -> reconciled to success (effect existed).
        assert step3.reconciled is True
        assert step3.outcome == Outcome.SUCCESS
        # Exactly one car booking despite the timeout (no duplicate).
        assert len(reg.world.cars) == 1
        # Run proceeds past the timed-out step and completes.
        assert run.state == WorkflowState.COMPLETED
        assert coord.audit.verify().valid
    finally:
        coord.close()


async def test_per_step_timeout_no_effect_is_safe(tmp_config):
    """Timeout where the server never acted -> reconciled as not-found, safe."""

    class _HangCarAdapter(BaseAdapter):
        tools = ("reserve_car",)

        async def _do_execute(self, intent):
            await asyncio.sleep(5.0)  # never records an effect
            return ToolResult(intent_id=intent.intent_id, outcome=Outcome.SUCCESS,
                              data={})

    plan = _plan(tmp_config)
    plan.step("step-3").timeout_ms = 20
    reg = AdapterRegistry()
    reg.register(_HangCarAdapter(reg.world, reg.injector))
    coord = TransactionCoordinator(plan, reg, config=tmp_config, backoff_scale=0.0)
    try:
        run = await coord.run_workflow()
        # No car booked; pre-pivot compensation ran (no payment).
        assert len(reg.world.cars) == 0
        assert len(reg.world.payments) == 0
        assert run.pivot_crossed is False
        assert run.state == WorkflowState.ABORTED
        assert coord.audit.verify().valid
    finally:
        coord.close()


# ---------------------------------------------------------------------------
# Item 2: circuit breaker (per-tool, fail fast on repeated failures)
# ---------------------------------------------------------------------------


async def test_circuit_breaker_trips_and_fails_fast(tmp_config):
    plan = _plan(tmp_config)
    # Give step-1 several retry attempts so the breaker (threshold=2) trips
    # before the retries are naturally exhausted.
    plan.step("step-1").retry.max_attempts = 5
    injector = FailureInjector([
        FailureRule(mode="error", step_id="step-1",
                    failure_class="rate_limit", message="RATE_LIMITED"),
    ])
    reg = AdapterRegistry(injector=injector)
    breaker = CircuitBreaker(failure_threshold=2, cooldown_s=1000.0)
    coord = TransactionCoordinator(plan, reg, config=tmp_config, backoff_scale=0.0,
                                   circuit_breaker=breaker)
    try:
        run = await coord.run_workflow()
        # The breaker opened for book_flight and cut retries short.
        assert breaker.state("book_flight") is BreakerState.OPEN
        assert breaker.trips.get("book_flight", 0) >= 1
        step1 = next(s for s in run.step_executions if s.step_id == "step-1")
        assert step1.attempt < 5  # failed fast, did not exhaust all attempts
        assert "circuit breaker open" in step1.detail
        assert run.state == WorkflowState.ABORTED
        # A CIRCUIT_OPEN event is on the tamper-evident audit chain.
        events = [e["event"] for e in coord.audit.entries()]
        assert AuditEvent.CIRCUIT_OPEN.value in events
        assert coord.audit.verify().valid
    finally:
        coord.close()


async def test_circuit_breaker_half_open_recovers():
    """Unit-level: after cooldown, a HALF_OPEN trial success closes the breaker."""
    clock = {"t": 0.0}
    cb = CircuitBreaker(failure_threshold=2, cooldown_s=10.0,
                        clock=lambda: clock["t"])
    assert cb.allow("pay") is True
    cb.record_failure("pay")
    cb.record_failure("pay")            # trips OPEN
    assert cb.state("pay") is BreakerState.OPEN
    assert cb.allow("pay") is False     # still cooling down
    clock["t"] = 11.0                   # cooldown elapsed
    assert cb.allow("pay") is True      # HALF_OPEN trial permitted
    assert cb.state("pay") is BreakerState.HALF_OPEN
    cb.record_success("pay")            # trial succeeded -> closed
    assert cb.state("pay") is BreakerState.CLOSED


# ---------------------------------------------------------------------------
# Item 7: durable approval workflow
# ---------------------------------------------------------------------------


def test_approval_store_out_of_band_decision(tmp_path):
    from aatm.storage.approvals import ApprovalStore

    store = ApprovalStore(tmp_path / "run.approvals.jsonl")
    store.request("step-4", "charge_payment", timeout_s=3600)
    assert [p["step_id"] for p in store.pending()] == ["step-4"]
    store.decide("step-4", True, decided_by="alice", reason="ok")
    assert store.pending() == []
    latest = store.latest_decision("step-4")
    assert latest["granted"] is True and latest["decided_by"] == "alice"


def test_approval_store_timeout_is_expired(tmp_path):
    from aatm.storage.approvals import ApprovalStore

    store = ApprovalStore(tmp_path / "run.approvals.jsonl")
    rec = store.request("step-4", "charge_payment", timeout_s=-1)  # already past
    assert ApprovalStore.is_expired(rec) is True


async def test_durable_approval_precedes_callback(tmp_config):
    """A pre-recorded out-of-band decision wins over the in-process callback."""
    from aatm.storage.approvals import ApprovalStore

    plan = _plan(tmp_config)
    pivot = plan.step(plan.pivot_step_id)
    pivot.approval_required = True

    reg = AdapterRegistry()
    # Pre-record an approval for the pivot BEFORE running.
    from uuid import uuid4
    run_id = uuid4()
    store = ApprovalStore(tmp_config.approval_path(str(run_id)))
    store.decide(pivot.step_id, True, decided_by="operator")

    called = {"n": 0}

    def _cb(step):
        called["n"] += 1
        return False  # callback would DENY, but the stored GRANT must win

    coord = TransactionCoordinator(plan, reg, config=tmp_config, run_id=run_id,
                                   approval_callback=_cb, backoff_scale=0.0)
    try:
        run = await coord.run_workflow()
        assert called["n"] == 0                    # callback never consulted
        assert run.pivot_crossed is True           # pivot proceeded on stored grant
        assert coord.audit.verify().valid
    finally:
        coord.close()


async def test_expired_approval_fails_safe_deny(tmp_config):
    """An expired request denies without consulting the callback (fail-safe)."""
    plan = _plan(tmp_config)
    pivot = plan.step(plan.pivot_step_id)
    pivot.approval_required = True

    tmp_config.approval_timeout_s = -1  # every request is instantly expired
    reg = AdapterRegistry()
    coord = TransactionCoordinator(plan, reg, config=tmp_config, backoff_scale=0.0,
                                   approval_callback=lambda s: True)
    try:
        run = await coord.run_workflow()
        assert run.pivot_crossed is False
        assert run.state == WorkflowState.ABORTED
    finally:
        coord.close()


# ---------------------------------------------------------------------------
# Item 8: HTTP adapter (idempotency-key propagation, honest UNKNOWN)
# ---------------------------------------------------------------------------


async def test_http_adapter_propagates_idempotency_key(monkeypatch):
    import json as _json
    from aatm.adapters.http_adapter import HTTPToolAdapter
    from aatm.models import ActionIntent

    captured = {}

    class _Resp:
        status = 200
        headers = {}

        def read(self):
            return _json.dumps({"ok": True}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=None):
        captured["idem"] = req.get_header("Idempotency-key")
        captured["method"] = req.get_method()
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    from uuid import uuid4
    adapter = HTTPToolAdapter("http://svc.local", ("book",))
    intent = ActionIntent(tool_name="book", parameters={"x": 1}, run_id=uuid4(),
                          workflow_id="wf", step_id="s1")
    result = await adapter.execute(intent)
    assert result.outcome == Outcome.SUCCESS
    assert captured["idem"] == str(intent.intent_id)  # stable dedupe key sent
    assert captured["method"] == "POST"


async def test_http_adapter_transport_error_is_unknown(monkeypatch):
    from aatm.adapters.http_adapter import HTTPToolAdapter
    from aatm.models import ActionIntent

    def _boom(req, timeout=None):
        raise TimeoutError("connection timed out")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    from uuid import uuid4
    adapter = HTTPToolAdapter("http://svc.local", ("book",))
    intent = ActionIntent(tool_name="book", parameters={}, run_id=uuid4(),
                          workflow_id="wf", step_id="s1")
    result = await adapter.execute(intent)
    # A lost response must NOT be reported as a clean failure.
    assert result.outcome == Outcome.UNKNOWN


# ---------------------------------------------------------------------------
# Item 10: pluggable storage backend selection
# ---------------------------------------------------------------------------


def test_backend_selection_defaults_to_sqlite():
    from aatm.storage.backend import (PostgresBackend, SQLiteBackend,
                                      backend_from_url)

    assert isinstance(backend_from_url(None), SQLiteBackend)
    assert isinstance(backend_from_url("sqlite"), SQLiteBackend)
    assert isinstance(backend_from_url("/tmp/foo.db"), SQLiteBackend)
    pg = backend_from_url("postgresql://user@host/db")
    assert isinstance(pg, PostgresBackend)


def test_sqlite_backend_roundtrip(tmp_path):
    from aatm.storage.backend import SQLiteBackend

    conn = SQLiteBackend().connect(tmp_path / "t.db")
    conn.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT);")
    conn.execute("INSERT INTO t (v) VALUES (?)", ("hello",))
    row = conn.execute("SELECT v FROM t WHERE id = ?", (1,)).fetchone()
    assert row["v"] == "hello"
    conn.close()


# ---------------------------------------------------------------------------
# Item 11: versioning & migrations
# ---------------------------------------------------------------------------


def test_schema_version_recorded_and_guarded(tmp_path):
    from aatm.storage.backend import SQLiteBackend
    from aatm.storage.migrations import (SQL_SCHEMA_VERSIONS, SchemaVersionError,
                                         ensure_schema)

    conn = SQLiteBackend().connect(tmp_path / "wal.db")
    v = ensure_schema(conn, "wal")
    assert v == SQL_SCHEMA_VERSIONS["wal"]
    # Idempotent on re-open.
    assert ensure_schema(conn, "wal") == v
    # Simulate a database from a newer build -> fail closed.
    conn.execute("UPDATE schema_meta SET version = ? WHERE component = ?",
                 (v + 1, "wal"))
    with pytest.raises(SchemaVersionError):
        ensure_schema(conn, "wal")
    conn.close()


def test_workflow_format_version_rejected_when_newer(tmp_config):
    from aatm.planner.parser import WorkflowParseError, WorkflowParser

    parser = WorkflowParser(tmp_config)
    parsed = parser.parse("workflows/travel_booking.yaml")
    data = dict(parsed.data)
    data["schema_version"] = 999
    with pytest.raises(WorkflowParseError):
        parser.validate(data)


# ---------------------------------------------------------------------------
# Item 12: PII / secret redaction
# ---------------------------------------------------------------------------


def test_redactor_masks_pii_and_secrets():
    from aatm.redaction import REDACTED, Redactor

    r = Redactor(enabled=True)
    out = r.redact({
        "email": "jane.doe@example.com",
        "password": "hunter2",
        "card_number": "4111 1111 1111 1234",
        "nested": {"api_key": "abc", "note": "fine"},
        "list": [{"ssn": "123-45-6789"}],
    })
    assert out["email"].endswith("@example.com") and REDACTED in out["email"]
    assert out["password"] == REDACTED
    assert out["card_number"] == REDACTED
    assert out["nested"]["api_key"] == REDACTED
    assert out["nested"]["note"] == "fine"
    assert out["list"][0]["ssn"] == REDACTED


def test_redactor_preserves_hashes_and_uuids():
    from aatm.redaction import Redactor

    r = Redactor(enabled=True)
    h = "a" * 64                       # sha256-like hex digest
    u = "b1b649a2-76c1-4868-b992-9c7342a4f850"
    out = r.redact({"hash": h, "run_id": u, "seq": 42})
    assert out["hash"] == h            # digests must survive intact
    assert out["run_id"] == u          # UUID identifiers must survive intact
    assert out["seq"] == 42


def test_redactor_disabled_is_passthrough():
    from aatm.redaction import Redactor

    data = {"password": "hunter2"}
    assert Redactor(enabled=False).redact(data) == data
