"""Unit tests for the checkpoint store."""

from __future__ import annotations

from uuid import uuid4

from aatm.storage.checkpoints import CheckpointStore


def test_create_and_load_checkpoint(tmp_path):
    run_id = uuid4()
    store = CheckpointStore(tmp_path / "ckpt.db")
    cid = store.create_checkpoint(
        run_id,
        "step-3",
        wal_seq=7,
        pivot_crossed=False,
        completed_step_ids=["step-1", "step-2"],
        variables={"user_id": "emp-1"},
        saga_state="RUNNING",
        world_state={"bookings": {"BK-1": "confirmed"}},
        compensation_plan=[{"tool": "cancel_flight"}],
    )
    snap = store.load_checkpoint(cid)
    assert snap is not None
    assert snap.step_id == "step-3"
    assert snap.wal_seq == 7
    assert snap.completed_step_ids == ["step-1", "step-2"]
    assert snap.variables == {"user_id": "emp-1"}
    assert snap.world_state == {"bookings": {"BK-1": "confirmed"}}
    assert snap.compensation_plan == [{"tool": "cancel_flight"}]
    store.close()


def test_latest_for_run(tmp_path):
    run_id = uuid4()
    store = CheckpointStore(tmp_path / "ckpt.db")
    store.create_checkpoint(run_id, "step-1", wal_seq=1)
    store.create_checkpoint(run_id, "step-2", wal_seq=2)
    last = store.create_checkpoint(run_id, "step-3", wal_seq=3, pivot_crossed=True)

    snap = store.latest_for_run(run_id)
    assert snap is not None
    assert snap.checkpoint_id == last
    assert snap.step_id == "step-3"
    assert snap.pivot_crossed is True
    store.close()


def test_all_for_run_ordered(tmp_path):
    run_id = uuid4()
    store = CheckpointStore(tmp_path / "ckpt.db")
    for i in range(1, 4):
        store.create_checkpoint(run_id, f"step-{i}", wal_seq=i)
    snaps = store.all_for_run(run_id)
    assert [s.step_id for s in snaps] == ["step-1", "step-2", "step-3"]
    store.close()


def test_checkpoint_survives_reopen(tmp_path):
    run_id = uuid4()
    db = tmp_path / "ckpt.db"
    store = CheckpointStore(db)
    cid = store.create_checkpoint(
        run_id, "step-4", wal_seq=9, pivot_crossed=True,
        world_state={"payment": "captured"},
    )
    store.close()

    store2 = CheckpointStore(db)
    snap = store2.restore_checkpoint(cid)
    assert snap is not None
    assert snap.pivot_crossed is True
    assert snap.world_state == {"payment": "captured"}
    store2.close()
