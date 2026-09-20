"""Unit tests for the write-ahead log."""

from __future__ import annotations

from uuid import uuid4


from aatm.enums import WALStatus
from aatm.models import ActionIntent
from aatm.storage.wal import WriteAheadLog


def _intent(run_id, step_id="step-1", tool="book_flight", params=None, idem=None,
            is_comp=False):
    return ActionIntent(
        run_id=run_id,
        workflow_id="wf-1",
        step_id=step_id,
        tool_name=tool,
        parameters=params or {"x": 1},
        idempotency_key=idem,
        is_compensation=is_comp,
    )


def test_write_entry_is_durable_and_pending(tmp_path):
    run_id = uuid4()
    wal = WriteAheadLog(tmp_path / "wal.db")
    intent = _intent(run_id)
    seq = wal.write_intent(intent, tier=2, pivot=False)
    assert seq == 1

    entry = wal.get(intent.intent_id)
    assert entry is not None
    assert entry.status == WALStatus.PENDING.value
    assert entry.tool == "book_flight"
    assert entry.tier == 2
    assert entry.pivot is False
    wal.close()


def test_update_state_transitions(tmp_path):
    run_id = uuid4()
    wal = WriteAheadLog(tmp_path / "wal.db")
    intent = _intent(run_id)
    wal.write_intent(intent, tier=2)

    wal.update_status(intent.intent_id, WALStatus.RUNNING)
    assert wal.get(intent.intent_id).status == WALStatus.RUNNING.value

    wal.update_status(
        intent.intent_id,
        WALStatus.COMMITTED,
        outcome="success",
        result={"booking_id": "BK-1"},
    )
    entry = wal.get(intent.intent_id)
    assert entry.status == WALStatus.COMMITTED.value
    assert entry.outcome == "success"
    assert entry.result == {"booking_id": "BK-1"}
    wal.close()


def test_replay_pending_entries(tmp_path):
    run_id = uuid4()
    wal = WriteAheadLog(tmp_path / "wal.db")
    i1 = _intent(run_id, "step-1")
    i2 = _intent(run_id, "step-2")
    i3 = _intent(run_id, "step-3")
    wal.write_intent(i1)
    wal.write_intent(i2)
    wal.write_intent(i3)

    wal.update_status(i1.intent_id, WALStatus.COMMITTED, outcome="success")
    wal.update_status(i2.intent_id, WALStatus.UNKNOWN)
    # i3 stays PENDING

    pending = wal.pending_entries(run_id)
    pending_steps = {e.step_id for e in pending}
    assert pending_steps == {"step-2", "step-3"}
    wal.close()


def test_durability_across_reopen(tmp_path):
    """Committed rows survive closing and reopening the WAL (crash simulation)."""
    run_id = uuid4()
    db = tmp_path / "wal.db"
    wal = WriteAheadLog(db)
    intent = _intent(run_id)
    wal.write_intent(intent, tier=3, pivot=True)
    wal.update_status(intent.intent_id, WALStatus.RUNNING)
    wal.close()

    # Reopen - simulates a crash + restart.
    wal2 = WriteAheadLog(db)
    entry = wal2.get(intent.intent_id)
    assert entry is not None
    assert entry.status == WALStatus.RUNNING.value
    assert entry.pivot is True
    assert entry.tier == 3
    # RUNNING is a pending state that requires reconciliation.
    assert any(e.intent_id == str(intent.intent_id) for e in wal2.pending_entries())
    wal2.close()


def test_idempotency_key_lookup(tmp_path):
    run_id = uuid4()
    wal = WriteAheadLog(tmp_path / "wal.db")
    intent = _intent(run_id, idem="user-1|2025-01-01")
    wal.write_intent(intent)

    found = wal.find_by_idempotency_key(run_id, "user-1|2025-01-01")
    assert found is not None
    assert found.intent_id == str(intent.intent_id)

    missing = wal.find_by_idempotency_key(run_id, "nope")
    assert missing is None
    wal.close()


def test_seq_is_monotonic(tmp_path):
    run_id = uuid4()
    wal = WriteAheadLog(tmp_path / "wal.db")
    seqs = [wal.write_intent(_intent(run_id, f"step-{i}")) for i in range(5)]
    assert seqs == [1, 2, 3, 4, 5]
    assert wal.max_seq() == 5
    wal.close()
