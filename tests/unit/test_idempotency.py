"""Unit tests for the idempotency store."""

from __future__ import annotations

from uuid import uuid4

from aatm.storage.idempotency import IdempotencyStore


def test_same_intent_repeated_returns_existing(tmp_path):
    run_id = uuid4()
    store = IdempotencyStore(tmp_path / "idem.db")
    intent_id = uuid4()

    is_new, existing = store.claim(run_id, "key-1", intent_id, "charge_payment", "h1")
    assert is_new is True
    assert existing is None

    # Second claim of same key must NOT create a new claim.
    is_new2, existing2 = store.claim(run_id, "key-1", uuid4(), "charge_payment", "h1")
    assert is_new2 is False
    assert existing2 is not None
    # The originally claimed intent_id is preserved (not the new one).
    assert existing2.intent_id == str(intent_id)
    store.close()


def test_duplicate_execution_blocked_via_recorded_outcome(tmp_path):
    run_id = uuid4()
    store = IdempotencyStore(tmp_path / "idem.db")
    intent_id = uuid4()
    store.claim(run_id, "key-1", intent_id, "charge_payment", "h1")
    store.record_outcome(run_id, "key-1", "success", {"txn_id": "T-1"})

    rec = store.get(run_id, "key-1")
    assert rec is not None
    assert rec.outcome == "success"
    assert rec.result == {"txn_id": "T-1"}

    # A duplicate submission finds the recorded success and can short-circuit.
    is_new, existing = store.claim(run_id, "key-1", uuid4(), "charge_payment", "h1")
    assert is_new is False
    assert existing.outcome == "success"
    assert existing.result == {"txn_id": "T-1"}
    store.close()


def test_timeout_then_reconciliation_keeps_same_intent(tmp_path):
    run_id = uuid4()
    store = IdempotencyStore(tmp_path / "idem.db")
    intent_id = uuid4()
    store.claim(run_id, "key-1", intent_id, "charge_payment", "h1")

    # Simulate unknown outcome (timeout) then reconciliation -> success.
    store.record_outcome(run_id, "key-1", "unknown", None)
    assert store.get(run_id, "key-1").outcome == "unknown"

    store.record_outcome(run_id, "key-1", "success", {"txn_id": "T-9"})
    rec = store.get(run_id, "key-1")
    assert rec.outcome == "success"
    # Same intent id throughout.
    assert rec.intent_id == str(intent_id)
    store.close()


def test_get_by_intent(tmp_path):
    run_id = uuid4()
    store = IdempotencyStore(tmp_path / "idem.db")
    intent_id = uuid4()
    store.claim(run_id, "key-1", intent_id, "book_flight", "h1")
    rec = store.get_by_intent(intent_id)
    assert rec is not None
    assert rec.idem_key == "key-1"
    store.close()


def test_different_runs_isolated(tmp_path):
    store = IdempotencyStore(tmp_path / "idem.db")
    run_a, run_b = uuid4(), uuid4()
    store.claim(run_a, "key-1", uuid4(), "book_flight", "h1")
    is_new, _ = store.claim(run_b, "key-1", uuid4(), "book_flight", "h1")
    # Same key in a different run is independent.
    assert is_new is True
    store.close()
