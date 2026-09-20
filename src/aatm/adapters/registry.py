"""Adapter registry: routes tool names to adapter instances.

The coordinator resolves each step's tool through this registry. Unknown tools
are rejected (fail-closed) rather than executed. This is also where compensation
tool calls are dispatched (compensation is always an adapter call).
"""

from __future__ import annotations

from typing import Optional

from .base import BaseAdapter, MockWorldState, ToolAdapter
from .failures import FailureInjector
from .mock_crm import CRMAdapter
from .mock_email import EmailAdapter
from .mock_payment import PaymentAdapter
from .mock_travel import TravelAdapter


class AdapterRegistry:
    """Maps tool name -> adapter. All adapters share one world + injector."""

    def __init__(self, world: Optional[MockWorldState] = None,
                 injector: Optional[FailureInjector] = None,
                 world_persist_path: "Optional[object]" = None) -> None:
        self.world = world or MockWorldState(persist_path=world_persist_path)
        self.injector = injector or FailureInjector()
        self._by_tool: dict[str, BaseAdapter] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        for adapter_cls in (TravelAdapter, PaymentAdapter, EmailAdapter, CRMAdapter):
            adapter = adapter_cls(self.world, self.injector)
            for tool in adapter.tools:
                self._by_tool[tool] = adapter

    def register(self, adapter: BaseAdapter) -> None:
        for tool in adapter.tools:
            self._by_tool[tool] = adapter

    def register_tool(self, tool: str, adapter: BaseAdapter) -> None:
        self._by_tool[tool] = adapter

    def get(self, tool: str) -> Optional[BaseAdapter]:
        return self._by_tool.get(tool)

    def has(self, tool: str) -> bool:
        return tool in self._by_tool

    def known_tools(self) -> list[str]:
        return sorted(self._by_tool.keys())


def default_registry(
    world: Optional[MockWorldState] = None,
    injector: Optional[FailureInjector] = None,
) -> AdapterRegistry:
    return AdapterRegistry(world=world, injector=injector)


__all__ = [
    "AdapterRegistry",
    "default_registry",
    "BaseAdapter",
    "MockWorldState",
    "ToolAdapter",
    "TravelAdapter",
    "PaymentAdapter",
    "EmailAdapter",
    "CRMAdapter",
    "FailureInjector",
]
