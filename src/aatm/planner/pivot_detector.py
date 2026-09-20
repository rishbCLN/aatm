"""Pivot detection.

The pivot is the first point after which the entire pre-pivot state cannot be
restored exactly (spec section 2.4).

Rules:
    1. Explicit ``is_pivot: true`` wins.
    2. Otherwise, the first Tier-3 action is the default pivot.
    3. If no Tier-3 action exists, there is no pivot.
    4. Crossing a pivot changes recovery from rollback-equivalent compensation to
       forward recovery.
"""

from __future__ import annotations

from typing import Optional

from ..enums import Tier
from ..models import PlannedStep


def detect_pivot(steps: list[PlannedStep]) -> Optional[str]:
    """Return the step_id of the pivot, or ``None`` if there is no pivot."""
    # Rule 1: explicit pivot wins (first explicit one, in order).
    for step in steps:
        if step.is_pivot:
            return step.step_id

    # Rule 2: first Tier-3 action.
    for step in steps:
        if step.tier == Tier.THREE:
            return step.step_id

    # Rule 3: no Tier-3 -> no pivot.
    return None


def mark_pivot(steps: list[PlannedStep], pivot_step_id: Optional[str]) -> None:
    """Annotate steps with ``is_pivot`` / ``is_post_pivot`` in list order.

    Everything strictly after the pivot index is post-pivot. The pivot step
    itself is marked ``is_pivot`` but not ``is_post_pivot``.
    """
    if pivot_step_id is None:
        for step in steps:
            step.is_pivot = False
            step.is_post_pivot = False
        return

    pivot_index = next(
        (i for i, s in enumerate(steps) if s.step_id == pivot_step_id), None
    )
    for i, step in enumerate(steps):
        step.is_pivot = step.step_id == pivot_step_id
        step.is_post_pivot = pivot_index is not None and i > pivot_index
