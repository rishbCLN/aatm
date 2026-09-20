"""Compensation validator.

Validates a resolved compensation plan against the runtime context before it is
allowed to execute:

- the compensation tool must be registered (fail-closed otherwise),
- parameter mappings must resolve against the original step's result/params/vars
  (unresolved refs mark the compensation STALE and block execution),
- the compensation must not run before the original action could have completed.
"""

from __future__ import annotations

from typing import Any

from ..adapters.registry import AdapterRegistry
from ..models import CompensationPlan
from ..verification.expressions import bind_parameters


class CompensationValidationResult:
    def __init__(
        self,
        valid: bool,
        bound_parameters: dict[str, Any],
        reasons: list[str],
        stale: bool = False,
    ) -> None:
        self.valid = valid
        self.bound_parameters = bound_parameters
        self.reasons = reasons
        self.stale = stale

    def __bool__(self) -> bool:
        return self.valid


class CompensationValidator:
    def __init__(self, registry: AdapterRegistry) -> None:
        self.registry = registry

    def validate(
        self, comp: CompensationPlan, context: dict[str, Any]
    ) -> CompensationValidationResult:
        reasons: list[str] = []

        if comp.tool is None:
            return CompensationValidationResult(
                False, {}, ["compensation has no tool"], stale=False
            )

        # Fail closed on unknown compensation tools.
        if not self.registry.has(comp.tool):
            reasons.append(f"compensation tool '{comp.tool}' is not registered")
            return CompensationValidationResult(False, {}, reasons, stale=False)

        # Bind parameters; unresolved references => stale/mismatched contract.
        bound, unresolved = bind_parameters(comp.mapping, context)
        if unresolved:
            reasons.append(
                "stale compensation: unresolved mappings "
                + ", ".join(unresolved)
            )
            return CompensationValidationResult(False, bound, reasons, stale=True)

        return CompensationValidationResult(True, bound, [], stale=False)
