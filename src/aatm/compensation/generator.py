"""Optional compensation suggestion generator (advisory only).

This is authority level 4 in the hierarchy (spec 3.1) - it NEVER executes
automatically for production paths. When no LLM is configured (the default), it
runs a deterministic heuristic that proposes a plausible inverse but ALWAYS marks
the proposal as low-confidence, ``source=llm``, ``requires_approval=true``.

For unknown/irreversible tools with no verified compensation it emits the exact
manual-escalation stub required by spec section 27.
"""

from __future__ import annotations

from typing import Any, Optional

from ..enums import CompensationSource, CompensationStrategy, Reversibility
from ..models import CompensationPlan


# Deterministic naming heuristics for a "plausible inverse" verb.
_INVERSE_PREFIXES = {
    "book_": "cancel_",
    "reserve_": "cancel_",
    "create_": "delete_",
    "add_": "remove_",
    "charge_": "refund_",
    "send_": "send_correction_",
    "enable_": "disable_",
    "start_": "stop_",
    "open_": "close_",
    "allocate_": "deallocate_",
}


class CompensationGenerator:
    """Advisory generator. Deterministic heuristic by default; LLM optional."""

    def __init__(self, enable_llm: bool = False) -> None:
        self.enable_llm = enable_llm

    def suggest(
        self,
        tool_name: str,
        *,
        reversibility: Reversibility,
        parameters_schema: Optional[dict[str, Any]] = None,
        returns_schema: Optional[dict[str, Any]] = None,
        side_effect_scope: str = "external",
        workflow_goal: str = "",
    ) -> CompensationPlan:
        """Return an ADVISORY compensation proposal.

        The result is never authoritative: confidence is capped, source is llm,
        and requires_approval is forced true.
        """
        # Unknown / irreversible with no verified contract -> manual escalation.
        if reversibility in (Reversibility.UNKNOWN, Reversibility.IRREVERSIBLE):
            # Payment/email have a known forward-fix shape; still advisory here.
            forward = self._forward_fix_guess(tool_name)
            if forward is not None:
                return CompensationPlan(
                    strategy=CompensationStrategy.FORWARD_FIX,
                    tool=forward,
                    confidence=0.3,
                    source=CompensationSource.LLM,
                    requires_approval=True,
                    risks=[
                        "Advisory forward-fix suggestion; irreversible action "
                        "cannot be undone. Requires human approval.",
                    ],
                )
            return CompensationPlan(
                strategy=CompensationStrategy.MANUAL_ESCALATION,
                confidence=0.0,
                source=CompensationSource.LLM,
                requires_approval=True,
                risks=["No verified semantic compensation contract"],
            )

        # Compensatable -> propose a plausible inverse verb.
        inverse = self._inverse_tool_name(tool_name)
        if inverse is None:
            return CompensationPlan(
                strategy=CompensationStrategy.MANUAL_ESCALATION,
                confidence=0.0,
                source=CompensationSource.LLM,
                requires_approval=True,
                risks=["Could not infer a plausible inverse tool name"],
            )

        # Guess a mapping from the returns schema (id-like fields).
        mapping = self._guess_mapping(returns_schema or {})
        return CompensationPlan(
            strategy=CompensationStrategy.COMPENSATE,
            tool=inverse,
            mapping=mapping,
            confidence=0.4,
            source=CompensationSource.LLM,
            requires_approval=True,
            risks=[
                "Advisory suggestion inferred from tool naming/schema. Not a "
                "verified contract; requires human approval before execution.",
            ],
        )

    # -- heuristics -----------------------------------------------------------

    def _inverse_tool_name(self, tool_name: str) -> Optional[str]:
        for prefix, inverse_prefix in _INVERSE_PREFIXES.items():
            if tool_name.startswith(prefix):
                return inverse_prefix + tool_name[len(prefix):]
        return None

    def _forward_fix_guess(self, tool_name: str) -> Optional[str]:
        if tool_name.startswith("charge_"):
            return "refund_" + tool_name[len("charge_"):]
        if tool_name.startswith("send_"):
            return "send_correction_" + tool_name[len("send_"):]
        return None

    def _guess_mapping(self, returns_schema: dict[str, Any]) -> dict[str, str]:
        props = {}
        if isinstance(returns_schema, dict):
            props = returns_schema.get("properties", {}) or {}
        for field in props:
            if field.endswith("_id") or field == "id" or field.endswith("booking_id"):
                # Map the inverse's likely id parameter to this result field.
                param = "booking_id" if "booking" in field else field
                return {param: f"result.{field}"}
        # Fallback: common id field.
        return {"booking_id": "result.booking_id"}
