"""Integration tests: deterministic replay + time-travel (Feature: replay).

Replay folds a run's immutable, hash-chained audit log back into a timeline of
state snapshots. It is a pure function of the log, so it is deterministic and
strictly read-only (no adapter is ever called). ``to_seq`` reconstructs the state
as it was at any point in the run's history.
"""

from __future__ import annotations

import uuid as _uuid

from aatm.adapters import AdapterRegistry, FailureInjector, FailureRule
from aatm.enums import WorkflowState
from aatm.engine import AATMEngine
from aatm.planner import SagaPlanner, WorkflowParser
from aatm.replay import ReplayEngine
from aatm.runtime import TransactionCoordinator

ORDER_WF = "workflows/order_fulfillment.yaml"


def _plan(tmp_config, path):
    parsed = WorkflowParser(tmp_config).parse(path)
    return SagaPlanner(tmp_config).plan(parsed)


async def _run_clean(tmp_config, run_id):
    engine = AATMEngine(tmp_config)
    out = await engine.run(ORDER_WF, run_id=run_id, backoff_scale=0.0)
    assert out.run.state == WorkflowState.COMPLETED
    return out


async def test_replay_reconstructs_completed_run(tmp_config):
    run_id = _uuid.uuid4()
    await _run_clean(tmp_config, run_id)

    result = ReplayEngine(tmp_config).replay(run_id)
    assert result.chain_valid
    assert result.total_events > 0
    assert len(result.frames) == result.total_events

    final = result.final_frame
    assert final.workflow_state == "COMPLETED"
    assert final.pivot_crossed is True
    # All three steps succeeded; each committed side effect was counted.
    assert final.steps["step-1"]["status"] == "success"
    assert final.steps["step-2"]["status"] == "success"
    assert final.steps["step-3"]["status"] == "success"
    assert final.side_effects >= 3


async def test_replay_is_deterministic(tmp_config):
    """Folding the same immutable log twice yields byte-identical timelines."""
    run_id = _uuid.uuid4()
    await _run_clean(tmp_config, run_id)

    a = ReplayEngine(tmp_config).replay(run_id).to_dict()
    b = ReplayEngine(tmp_config).replay(run_id).to_dict()
    assert a == b


async def test_replay_to_seq_is_time_travel(tmp_config):
    """`to_seq` reconstructs the state as of a point in time, not the end state."""
    run_id = _uuid.uuid4()
    await _run_clean(tmp_config, run_id)

    full = ReplayEngine(tmp_config).replay(run_id)
    # Find the seq where the pivot (step-2) committed via ACTION_COMPLETE.
    commit_seqs = [f.seq for f in full.frames
                   if f.event == "ACTION_COMPLETE" and f.entity_id == "step-2"]
    assert commit_seqs
    pivot_seq = commit_seqs[0]

    partial = ReplayEngine(tmp_config).replay(run_id, to_seq=pivot_seq)
    # Truncated: no frames past the requested seq.
    assert all(f.seq <= pivot_seq for f in partial.frames)
    assert partial.frames[-1].seq == pivot_seq

    frame = partial.final_frame
    # At the pivot commit, step-2 is done but step-3 has not started yet.
    assert frame.pivot_crossed is True
    assert frame.steps["step-2"]["status"] == "success"
    assert "step-3" not in frame.steps
    # The run is still RUNNING at this historical point - NOT yet completed.
    assert frame.workflow_state == "RUNNING"

    # `.at(seq)` returns the frame in effect at an arbitrary seq.
    assert full.at(pivot_seq).steps["step-2"]["status"] == "success"


async def test_replay_captures_failure_and_escalation(tmp_config):
    """A run that escalates forward is faithfully reconstructed as INCONSISTENT."""
    run_id = _uuid.uuid4()
    injector = FailureInjector([
        FailureRule(mode="error", step_id="step-3", message="CRM_DOWN")
    ])
    reg = AdapterRegistry(injector=injector,
                          world_persist_path=tmp_config.world_state_path(str(run_id)))
    coord = TransactionCoordinator(_plan(tmp_config, ORDER_WF), reg,
                                   config=tmp_config, run_id=run_id,
                                   backoff_scale=0.0)
    try:
        run = await coord.run_workflow()
        assert run.state == WorkflowState.INCONSISTENT
    finally:
        coord.close()

    result = ReplayEngine(tmp_config).replay(run_id)
    final = result.final_frame
    assert final.workflow_state == "INCONSISTENT"
    assert final.steps["step-3"]["status"] == "failure"
    assert final.steps["step-3"]["detail"] == "CRM_DOWN"
    # step-3 was attempted 3 times before escalation (max_attempts).
    assert final.steps["step-3"]["attempts"] == 3
    # The pivot charge stands as a committed side effect.
    assert final.steps["step-2"]["status"] == "success"
    events = [f.event for f in result.frames]
    assert "ESCALATION" in events
    assert "WORKFLOW_INCONSISTENT" in events


async def test_replay_missing_run_is_empty(tmp_config):
    """Replaying an unknown run yields an empty (valid) timeline, not an error."""
    result = ReplayEngine(tmp_config).replay(_uuid.uuid4())
    assert result.frames == []
    assert result.total_events == 0
    assert result.final_frame is None
    assert result.chain_valid  # an empty chain is trivially valid
