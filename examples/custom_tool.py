"""Example: registering a custom tool + adapter with AATM.

This shows how a developer plugs a new side-effecting tool into the engine while
keeping the deterministic safety guarantees (WAL-first, compensation, idempotency
by intent_id). Run directly:  python examples/custom_tool.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from uuid import uuid4

# Make ``src`` importable when running from a checkout.
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aatm.adapters.base import BaseAdapter, MockWorldState  # noqa: E402
from aatm.adapters.registry import AdapterRegistry  # noqa: E402
from aatm.enums import Outcome  # noqa: E402
from aatm.models import ActionIntent, ToolResult, VerificationResult  # noqa: E402


class InventoryAdapter(BaseAdapter):
    """A custom adapter that reserves and releases inventory items.

    ``reserve_inventory`` is a Tier-2 (compensatable) side effect; its semantic
    inverse is ``release_inventory``. State lives in the shared MockWorldState via
    a plain dict attached at runtime.
    """

    tools = ("reserve_inventory", "release_inventory")

    def _store(self) -> dict:
        # Attach a namespace to the shared world on first use.
        if not hasattr(self.world, "inventory"):
            self.world.inventory = {}  # type: ignore[attr-defined]
        return self.world.inventory  # type: ignore[attr-defined]

    async def _do_execute(self, intent: ActionIntent) -> ToolResult:
        store = self._store()
        if intent.tool_name == "reserve_inventory":
            res_id = self.world.next_id("INV")
            store[res_id] = {"id": res_id, "status": "reserved",
                             "sku": intent.parameters.get("sku"),
                             "intent_id": str(intent.intent_id)}
            data = {"reservation_id": res_id, "booking_id": res_id,
                    "status": "reserved"}
            self.world.record_effect(str(intent.intent_id), "inventory", res_id, data)
            return ToolResult(intent_id=intent.intent_id, outcome=Outcome.SUCCESS,
                              data=data)
        # release_inventory
        target = intent.parameters.get("reservation_id") or intent.parameters.get(
            "booking_id"
        )
        if not target or target not in store:
            return ToolResult(intent_id=intent.intent_id, outcome=Outcome.FAILURE,
                              error_message=f"reservation '{target}' not found")
        store[target]["status"] = "released"
        return ToolResult(intent_id=intent.intent_id, outcome=Outcome.SUCCESS,
                          data={"reservation_id": target, "status": "released"})

    def _postcondition(self, intent: ActionIntent, state: dict) -> VerificationResult:
        result = state.get("result", {}) or {}
        store = self._store()
        if intent.tool_name == "reserve_inventory":
            rid = result.get("reservation_id")
            ok = bool(rid) and store.get(rid, {}).get("status") == "reserved"
            return VerificationResult(passed=ok, expression="reservation == reserved",
                                      detail="reserved" if ok else "not reserved")
        rid = result.get("reservation_id")
        ok = bool(rid) and store.get(rid, {}).get("status") == "released"
        return VerificationResult(passed=ok, expression="reservation == released",
                                  detail="released" if ok else "not released")


async def _main() -> None:
    world = MockWorldState()
    registry = AdapterRegistry(world=world)
    registry.register(InventoryAdapter(world, registry.injector))

    run_id = uuid4()
    intent = ActionIntent(run_id=run_id, workflow_id="wf-inv", step_id="s1",
                          tool_name="reserve_inventory", parameters={"sku": "WIDGET-1"})
    adapter = registry.get("reserve_inventory")
    result = await adapter.execute(intent)
    print("reserve ->", result.outcome, result.data)

    verification = await adapter.verify_postcondition(intent, {"result": result.data})
    print("postcondition passed:", verification.passed)

    # Compensate (semantic inverse).
    comp = ActionIntent(run_id=run_id, workflow_id="wf-inv", step_id="comp-s1",
                        tool_name="release_inventory",
                        parameters={"reservation_id": result.data["reservation_id"]},
                        is_compensation=True)
    cres = await registry.get("release_inventory").execute(comp)
    print("release ->", cres.outcome, cres.data)


if __name__ == "__main__":
    asyncio.run(_main())
