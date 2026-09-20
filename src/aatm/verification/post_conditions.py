"""Postcondition verification.

Two layers of postcondition checking:

1. Adapter-level: the adapter inspects the real world state (authoritative for
   phantom-success detection - the tool claimed success but no effect exists).
2. Expression-level: the workflow's declared postcondition expressions are
   evaluated against the tool result via the safe evaluator.

Both must pass for a step to be considered successfully verified.
"""

from __future__ import annotations

from typing import Any

from ..models import PlannedStep, VerificationResult
from .expressions import evaluate


class PostconditionVerifier:
    """Evaluates declared postcondition expressions against a result context."""

    def verify_expressions(
        self, step: PlannedStep, result: dict[str, Any], variables: dict[str, Any]
    ) -> VerificationResult:
        """Evaluate all declared postcondition expressions for a step.

        Returns the first failing result, or a passing result if all pass (or if
        there are no declared postconditions).
        """
        context = {"result": result, "params": step.parameters, "variables": variables}
        for pc in step.postconditions:
            passed = evaluate(pc.expression, context)
            if not passed:
                return VerificationResult(
                    passed=False,
                    expression=pc.expression,
                    detail=f"postcondition failed: {pc.expression}",
                    observed={"result": result},
                )
        return VerificationResult(
            passed=True,
            expression=";".join(p.expression for p in step.postconditions),
            detail="all postconditions passed",
        )

    def combine(
        self, adapter_result: VerificationResult, expr_result: VerificationResult
    ) -> VerificationResult:
        """Both adapter-level and expression-level checks must pass."""
        if not adapter_result.passed:
            return adapter_result
        if not expr_result.passed:
            return expr_result
        return VerificationResult(
            passed=True,
            expression=expr_result.expression or adapter_result.expression,
            detail="adapter + expression postconditions passed",
        )
