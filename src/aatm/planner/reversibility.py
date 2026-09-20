"""Deterministic reversibility classifier.

Priority (spec section 12):

    1. explicit workflow declaration
    2. tool registry
    3. adapter metadata
    4. fail-closed unknown classification

Unknown tools MUST default to reversibility=unknown, risk=critical,
approval_required=true. The LLM never silently decides an unknown side effect is
safe; it may only attach an advisory explanation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from ..config import AATMConfig, default_config
from ..enums import (
    REVERSIBILITY_TO_TIER,
    IdempotencyMode,
    Reversibility,
    RiskLevel,
    SideEffectScope,
    Tier,
)
from ..models import ToolDefinition


class Classification:
    """Result of classifying a single tool for a step."""

    def __init__(
        self,
        tool_name: str,
        reversibility: Reversibility,
        tier: Tier,
        side_effect_scope: SideEffectScope,
        risk_level: RiskLevel,
        idempotent: bool,
        idempotency_mode: IdempotencyMode,
        approval_required: bool,
        source: str,
        explanation: str = "",
    ) -> None:
        self.tool_name = tool_name
        self.reversibility = reversibility
        self.tier = tier
        self.side_effect_scope = side_effect_scope
        self.risk_level = risk_level
        self.idempotent = idempotent
        self.idempotency_mode = idempotency_mode
        self.approval_required = approval_required
        self.source = source  # explicit | registry | adapter | fail_closed
        self.explanation = explanation

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "reversibility": str(self.reversibility),
            "tier": int(self.tier),
            "side_effect_scope": str(self.side_effect_scope),
            "risk_level": str(self.risk_level),
            "idempotent": self.idempotent,
            "idempotency_mode": str(self.idempotency_mode),
            "approval_required": self.approval_required,
            "source": self.source,
            "explanation": self.explanation,
        }


class ReversibilityClassifier:
    """Loads the tool registry + rules and classifies tools deterministically."""

    def __init__(self, config: Optional[AATMConfig] = None) -> None:
        self.config = config or default_config
        self._registry: dict[str, dict[str, Any]] = {}
        self._rules: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        kb = self.config.knowledge_base_dir
        if kb is None:
            return
        reg_path = Path(kb) / "tool_registry.json"
        rules_path = Path(kb) / "reversibility_rules.json"
        if reg_path.exists():
            self._registry = json.loads(reg_path.read_text(encoding="utf-8")).get(
                "tools", {}
            )
        if rules_path.exists():
            self._rules = json.loads(rules_path.read_text(encoding="utf-8"))

    # -- registry access ------------------------------------------------------

    def registry_tool(self, tool_name: str) -> Optional[dict[str, Any]]:
        return self._registry.get(tool_name)

    def tool_definition(self, tool_name: str) -> Optional[ToolDefinition]:
        raw = self._registry.get(tool_name)
        if raw is None:
            return None
        return ToolDefinition(name=tool_name, **_coerce_tool(raw))

    def is_known(self, tool_name: str) -> bool:
        return tool_name in self._registry

    # -- classification -------------------------------------------------------

    def classify(
        self,
        tool_name: str,
        *,
        explicit: Optional[dict[str, Any]] = None,
        adapter_meta: Optional[dict[str, Any]] = None,
    ) -> Classification:
        """Classify a tool using the priority order.

        ``explicit`` is a dict of workflow-declared overrides (from the ``tools``
        section or a step). ``adapter_meta`` is optional adapter-provided metadata.
        """
        # 1) Explicit workflow declaration wins if it fully specifies reversibility.
        if explicit and explicit.get("reversibility"):
            rev = Reversibility(explicit["reversibility"])
            return self._build(tool_name, rev, explicit, source="explicit")

        # 2) Tool registry.
        reg = self._registry.get(tool_name)
        if reg is not None:
            rev = Reversibility(reg.get("reversibility", "unknown"))
            merged = dict(reg)
            if explicit:
                merged.update({k: v for k, v in explicit.items() if v is not None})
            return self._build(tool_name, rev, merged, source="registry")

        # 3) Adapter metadata.
        if adapter_meta and adapter_meta.get("reversibility"):
            rev = Reversibility(adapter_meta["reversibility"])
            return self._build(tool_name, rev, adapter_meta, source="adapter")

        # 4) Fail closed.
        return self._fail_closed(tool_name)

    def _build(
        self,
        tool_name: str,
        reversibility: Reversibility,
        meta: dict[str, Any],
        source: str,
    ) -> Classification:
        tier = REVERSIBILITY_TO_TIER.get(reversibility, Tier.THREE)
        scope = SideEffectScope(meta.get("side_effect_scope", "external"))
        # No side effect => tier 0.
        if scope == SideEffectScope.NONE and reversibility == Reversibility.FULLY_REVERSIBLE:
            tier = Tier.NONE if not meta.get("force_tier") else tier
        risk = RiskLevel(meta.get("risk_level", "medium"))
        idempotent = bool(meta.get("idempotent", False))
        idem_mode = IdempotencyMode(meta.get("idempotency_mode", "none"))
        approval = bool(meta.get("approval_required", False))
        # Tier-3 always needs approval consideration (planner enforces globally too).
        if reversibility == Reversibility.IRREVERSIBLE and meta.get(
            "approval_required"
        ) is None:
            approval = approval  # leave as declared; planner enforces the gate
        return Classification(
            tool_name=tool_name,
            reversibility=reversibility,
            tier=tier,
            side_effect_scope=scope,
            risk_level=risk,
            idempotent=idempotent,
            idempotency_mode=idem_mode,
            approval_required=approval,
            source=source,
        )

    def _fail_closed(self, tool_name: str) -> Classification:
        defaults = self._rules.get("unknown_tool_defaults", {})
        return Classification(
            tool_name=tool_name,
            reversibility=Reversibility.UNKNOWN,
            tier=Tier.THREE,
            side_effect_scope=SideEffectScope(
                defaults.get("side_effect_scope", "external")
            ),
            risk_level=RiskLevel(defaults.get("risk_level", "critical")),
            idempotent=bool(defaults.get("idempotent", False)),
            idempotency_mode=IdempotencyMode(defaults.get("idempotency_mode", "none")),
            approval_required=bool(defaults.get("approval_required", True)),
            source="fail_closed",
            explanation=(
                "Tool is not in the verified registry; classified as unknown and "
                "treated as maximally dangerous (approval required)."
            ),
        )


def _coerce_tool(raw: dict[str, Any]) -> dict[str, Any]:
    """Coerce a registry entry into ToolDefinition kwargs."""
    out = dict(raw)
    comp = out.get("compensation_contract")
    if comp is not None and not isinstance(comp, dict):
        out.pop("compensation_contract", None)
    return out
