"""Compensation registry: verified compensation templates (authority level 2).

Loads ``knowledge_base/compensation_templates.json``. A template resolved here is
an engineering-owned contract that outranks any LLM suggestion. The registry
never fabricates a compensation for a tool it does not know about.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from ..config import AATMConfig, default_config
from ..enums import CompensationSource, CompensationStrategy
from ..models import CompensationPlan


class CompensationRegistry:
    def __init__(self, config: Optional[AATMConfig] = None) -> None:
        self.config = config or default_config
        self._templates: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        kb = self.config.knowledge_base_dir
        if kb is None:
            return
        path = Path(kb) / "compensation_templates.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self._templates = data.get("templates", {})

    def has(self, tool_name: str) -> bool:
        return tool_name in self._templates

    def template_for(self, tool_name: str) -> Optional[CompensationPlan]:
        raw = self._templates.get(tool_name)
        if raw is None:
            return None
        return CompensationPlan(
            strategy=CompensationStrategy(raw.get("strategy", "compensate")),
            tool=raw.get("tool"),
            mapping=raw.get("mapping", {}),
            confidence=float(raw.get("confidence", 1.0)),
            source=CompensationSource.REGISTRY,
            requires_approval=bool(raw.get("requires_approval", False)),
            verification=raw.get("verification"),
            risks=list(raw.get("risks", [])),
        )

    def all_tools(self) -> list[str]:
        return sorted(self._templates.keys())
