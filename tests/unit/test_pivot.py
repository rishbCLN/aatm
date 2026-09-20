"""Unit tests for the pivot detector."""

from __future__ import annotations

from aatm.enums import Tier
from aatm.models import PlannedStep
from aatm.planner.pivot_detector import detect_pivot, mark_pivot


def _step(step_id, tier, is_pivot=False):
    return PlannedStep(step_id=step_id, name=step_id, tool_name="t", tier=tier,
                       is_pivot=is_pivot)


def test_explicit_pivot_wins():
    steps = [
        _step("s1", Tier.TWO),
        _step("s2", Tier.THREE),  # would be inferred pivot
        _step("s3", Tier.TWO, is_pivot=True),  # explicit
    ]
    assert detect_pivot(steps) == "s3"


def test_inferred_first_tier3():
    steps = [
        _step("s1", Tier.TWO),
        _step("s2", Tier.TWO),
        _step("s3", Tier.THREE),
        _step("s4", Tier.THREE),
    ]
    assert detect_pivot(steps) == "s3"


def test_no_tier3_no_pivot():
    steps = [_step("s1", Tier.ONE), _step("s2", Tier.TWO)]
    assert detect_pivot(steps) is None


def test_mark_pivot_sets_post_pivot():
    steps = [
        _step("s1", Tier.TWO),
        _step("s2", Tier.THREE),
        _step("s3", Tier.TWO),
        _step("s4", Tier.TWO),
    ]
    pivot = detect_pivot(steps)
    mark_pivot(steps, pivot)
    assert steps[0].is_post_pivot is False
    assert steps[1].is_pivot is True
    assert steps[1].is_post_pivot is False
    assert steps[2].is_post_pivot is True
    assert steps[3].is_post_pivot is True


def test_mark_pivot_none_clears_all():
    steps = [_step("s1", Tier.ONE), _step("s2", Tier.TWO)]
    mark_pivot(steps, None)
    assert all(not s.is_pivot and not s.is_post_pivot for s in steps)
