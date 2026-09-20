"""Non-authoritative recovery explanation + evaluation (labeled ANALYSIS).

Produces human-readable narrative from the deterministic execution trace. When no
LLM is configured (default), a deterministic template generates the text. Nothing
here is ground truth: outputs are labeled ``ANALYSIS`` and never used to decide
whether a side effect occurred.
"""

from __future__ import annotations

from typing import Any

from ..enums import Outcome, WorkflowState
from ..models import WorkflowRun


class RecoveryExplainer:
    """Deterministic explanation generator (LLM optional, advisory)."""

    def __init__(self, enable_llm: bool = False) -> None:
        self.enable_llm = enable_llm

    def explain(self, run: WorkflowRun, *, pivot_step_id: str | None = None) -> str:
        lines: list[str] = []
        lines.append(f"[ANALYSIS] Run {run.run_id} for '{run.workflow_name}'.")
        lines.append(f"Final state: {run.state}.")

        completed = run.completed_step_ids
        lines.append(
            f"Completed {len(completed)} step(s): {', '.join(completed) or 'none'}."
        )

        if run.pivot_crossed:
            lines.append(
                "The pivot (irreversible Tier-3 side effect) was crossed. Recovery "
                "used forward/business-level reversal, not an exact rollback."
            )
        elif pivot_step_id:
            lines.append(
                f"The pivot ({pivot_step_id}) was NOT crossed; pre-pivot "
                "compensation could restore a consistent state."
            )

        if run.compensations:
            ok = [c for c in run.compensations if c.outcome == Outcome.SUCCESS]
            bad = [c for c in run.compensations if c.outcome == Outcome.FAILURE]
            lines.append(
                f"Compensations: {len(ok)} succeeded, {len(bad)} failed "
                f"(executed in reverse completion order)."
            )
            for c in run.compensations:
                lines.append(
                    f"  - {c.source_step_id}: {c.strategy} via "
                    f"{c.source} -> {c.outcome}"
                )
            if bad:
                lines.append(
                    "One or more compensations failed; the run entered a state that "
                    "requires manual intervention (INCONSISTENT)."
                )
        else:
            lines.append("No compensations were required.")

        return "\n".join(lines)

    def evaluate(self, run: WorkflowRun, failure_class: str = "") -> dict[str, Any]:
        """Return an ANALYSIS dict: what went wrong, policy match, residual risk."""
        went_wrong = []
        residual = []
        if run.state == WorkflowState.COMPLETED:
            went_wrong.append("Nothing; the workflow completed successfully.")
        elif run.state == WorkflowState.ABORTED:
            went_wrong.append(
                "A step failed; the run aborted after recovering completed work."
            )
        elif run.state == WorkflowState.INCONSISTENT:
            went_wrong.append(
                "A compensation could not be verified; the run is INCONSISTENT."
            )
            residual.append(
                "Manual intervention required to reconcile residual side effects."
            )
        elif run.state == WorkflowState.TIMED_OUT:
            went_wrong.append("The workflow exceeded its time budget.")

        for c in run.compensations:
            residual.extend(c.residual_risk)

        policy_match = (
            "matched" if run.state in (
                WorkflowState.COMPLETED, WorkflowState.ABORTED
            ) else "needs review"
        )

        return {
            "label": "ANALYSIS",
            "note": "Non-authoritative. Ground truth is the deterministic trace.",
            "what_went_wrong": went_wrong,
            "recovery_policy_match": policy_match,
            "residual_risks": sorted(set(residual)),
            "suggested_improvements": self._suggestions(run),
        }

    def _suggestions(self, run: WorkflowRun) -> list[str]:
        out: list[str] = []
        if run.state == WorkflowState.INCONSISTENT:
            out.append(
                "Add a verified secondary compensation path or a monitored manual "
                "escalation queue for the failing tool."
            )
        if run.pivot_crossed:
            out.append(
                "Consider moving reversible confirmation steps before the pivot to "
                "reduce post-pivot forward-recovery scope."
            )
        if not out:
            out.append("No changes suggested; recovery behaved as designed.")
        return out
