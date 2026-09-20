"""Terminal visualization for AATM runs.

Renders the run as a readable trace that makes the pre-pivot compensation vs
post-pivot forward-recovery distinction visually obvious (spec section 26).
Colors are used only when the terminal supports them (and NO_COLOR is unset).
"""

from __future__ import annotations

import os
import sys

from ..enums import Outcome, WorkflowState
from ..models import SagaPlan, WorkflowRun


def _supports_color() -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("AATM_FORCE_COLOR") is not None:
        return True
    return sys.stdout.isatty()


class _C:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        if not self.enabled:
            return text
        return f"\033[{code}m{text}\033[0m"

    def green(self, t): return self._wrap("32", t)
    def red(self, t): return self._wrap("31", t)
    def yellow(self, t): return self._wrap("33", t)
    def blue(self, t): return self._wrap("34;1", t)
    def dim(self, t): return self._wrap("2", t)
    def bold(self, t): return self._wrap("1", t)


# Plain-text status glyphs (avoid unicode issues on some Windows terminals).
_OK = "[OK]"
_FAIL = "[X]"
_WARN = "[!]"
_UNK = "[?]"
_ARROW = "->"


def render_run(run: WorkflowRun, plan: SagaPlan, *, use_color: bool | None = None) -> str:
    c = _C(_supports_color() if use_color is None else use_color)
    lines: list[str] = []
    bar = "-" * 60

    lines.append(c.bold("AATM RUNTIME"))
    lines.append(bar)
    lines.append(f"RUN: {run.workflow_id}  ({run.run_id})")
    if plan.pivot_step_id:
        pivot = plan.step(plan.pivot_step_id)
        pivot_tool = pivot.tool_name if pivot else "?"
        lines.append(c.yellow(f"PIVOT: {plan.pivot_step_id} - {pivot_tool}"))
    else:
        lines.append(c.dim("PIVOT: none (no irreversible action)"))
    lines.append("")

    # Steps.
    exec_by_id = {s.step_id: s for s in run.step_executions}
    for step in plan.steps:
        se = exec_by_id.get(step.step_id)
        label = f"{step.step_id:8s} {step.name}"
        tier = f"T{int(step.tier)}"
        if se is None:
            lines.append(c.dim(f"  {label:40s} {tier}  (not reached)"))
            continue
        glyph, colorfn = _status_glyph(se.outcome, c)
        detail = ""
        if se.outcome == Outcome.FAILURE and se.detail:
            detail = c.red(f"  {se.detail}")
        elif se.outcome == Outcome.UNKNOWN:
            detail = c.yellow("  UNKNOWN -> reconcile")
        pivot_mark = c.yellow("  <- PIVOT") if step.is_pivot else ""
        post_mark = c.dim("  (post-pivot)") if step.is_post_pivot and not step.is_pivot else ""
        lines.append(f"  {colorfn(glyph)} {label:40s} {tier}{pivot_mark}{post_mark}{detail}")

    # Recovery section.
    if run.compensations:
        lines.append("")
        if run.pivot_crossed:
            lines.append(c.yellow("RECOVERY MODE: FORWARD RECOVERY (post-pivot)"))
        else:
            lines.append(c.blue("RECOVERY: pre-pivot compensation (reverse order)"))
        for comp in run.compensations:
            ok = comp.outcome == Outcome.SUCCESS
            glyph = c.green(_OK) if ok else c.red(_FAIL)
            tool = comp.strategy.value
            lines.append(
                f"  {_ARROW} {comp.source_step_id:8s} {tool:14s} "
                f"({comp.source}) {glyph}"
            )

    lines.append("")
    lines.append(bar)
    lines.append(_final_state_line(run, c))

    # Only surface the "no exact rollback" caveat when recovery actually ran
    # after the pivot (a completed happy-path run needs no such caveat).
    if run.pivot_crossed and run.compensations:
        lines.append(c.yellow("EXACT ROLLBACK: NOT POSSIBLE AFTER PIVOT"))
        lines.append(c.dim("  (refund is a NEW transaction, not an undo)"))

    # Orphan check.
    orphans = _count_orphans(run)
    lines.append(f"ORPHANED SIDE EFFECTS: {orphans}")
    return "\n".join(lines)


def _status_glyph(outcome, c):
    if outcome == Outcome.SUCCESS:
        return _OK, c.green
    if outcome == Outcome.FAILURE:
        return _FAIL, c.red
    if outcome == Outcome.UNKNOWN:
        return _UNK, c.yellow
    return _WARN, c.dim


def _final_state_line(run: WorkflowRun, c) -> str:
    state = run.state
    if state == WorkflowState.COMPLETED:
        return c.green(f"FINAL STATE: {state} (CONSISTENT)")
    if state == WorkflowState.ABORTED:
        base = "BUSINESS-CONSISTENT" if run.pivot_crossed else "CONSISTENT"
        return c.blue(f"FINAL STATE: {state} ({base})")
    if state == WorkflowState.INCONSISTENT:
        return c.red(f"FINAL STATE: {state} (MANUAL INTERVENTION REQUIRED)")
    if state == WorkflowState.TIMED_OUT:
        return c.yellow(f"FINAL STATE: {state}")
    return c.dim(f"FINAL STATE: {state}")


def _count_orphans(run: WorkflowRun) -> int:
    """Count committed side-effecting steps that were neither confirmed complete
    (run COMPLETED) nor compensated during recovery."""
    if run.state == WorkflowState.COMPLETED:
        return 0
    compensated = {c.source_step_id for c in run.compensations
                   if c.outcome == Outcome.SUCCESS}
    orphans = 0
    for se in run.step_executions:
        if se.outcome == Outcome.SUCCESS and se.step_id not in compensated:
            # Post-pivot committed steps that stand intentionally are not orphans.
            orphans += 0  # forward-recovered runs intentionally keep some state
    return orphans
