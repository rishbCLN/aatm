"""Tool adapter contract + deterministic mock world.

Every tool AATM invokes goes through a :class:`ToolAdapter`. Adapters own a
deterministic, queryable :class:`MockWorldState`, so the whole engine is testable
without real APIs. Compensation is also an adapter call, never a hidden direct
function call inside the coordinator.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Optional, Protocol, runtime_checkable
from uuid import UUID

from ..enums import FailureClass, Outcome
from ..models import ActionIntent, OutcomeQuery, ToolResult, VerificationResult
from .failures import CrashSignal, FailureInjector, InjectedFailure


@runtime_checkable
class ToolAdapter(Protocol):
    """Structural contract every adapter must satisfy."""

    async def execute(self, intent: ActionIntent) -> ToolResult: ...

    async def query_status(self, intent_id: UUID) -> OutcomeQuery: ...

    async def verify_postcondition(
        self, intent: ActionIntent, state: dict[str, Any]
    ) -> VerificationResult: ...


class MockWorldState:
    """A single deterministic, queryable world shared by mock adapters.

    Holds every synthetic side effect (bookings, payments, emails, CRM records)
    plus an append-only side-effect log. Tests treat the final world state as the
    primary ground truth.
    """

    def __init__(self, persist_path: "Optional[Any]" = None) -> None:
        # Guards the id counter and the append-only effect log so concurrent
        # adapter calls (parallel branches / shared world) cannot collide on ids
        # or corrupt the log.
        self._lock = threading.RLock()
        self.flights: dict[str, dict[str, Any]] = {}
        self.hotels: dict[str, dict[str, Any]] = {}
        self.cars: dict[str, dict[str, Any]] = {}
        self.payments: dict[str, dict[str, Any]] = {}
        self.refunds: dict[str, dict[str, Any]] = {}
        self.emails: dict[str, dict[str, Any]] = {}
        self.crm_records: dict[str, dict[str, Any]] = {}
        # intent_id -> recorded server-side outcome (for status queries)
        self.intent_effects: dict[str, dict[str, Any]] = {}
        # append-only log of applied side effects
        self.side_effect_log: list[dict[str, Any]] = []
        self._counter = 0
        # Optional durable path modeling the EXTERNAL systems' own persistence.
        # When set, every applied side effect is flushed to disk so that a fresh
        # process (crash recovery) can query the authoritative external state.
        self._persist_path = persist_path
        if persist_path is not None:
            self._load_persisted()

    def next_id(self, prefix: str) -> str:
        with self._lock:
            self._counter += 1
            return f"{prefix}-{self._counter:04d}"

    def record_effect(self, intent_id: str, kind: str, entity_id: str,
                      data: dict[str, Any]) -> None:
        with self._lock:
            self.intent_effects[intent_id] = {
                "kind": kind,
                "entity_id": entity_id,
                "data": data,
            }
            self.side_effect_log.append(
                {"intent_id": intent_id, "kind": kind, "entity_id": entity_id}
            )
            self._flush()

    # -- durable external-system persistence ---------------------------------

    def _flush(self) -> None:
        if self._persist_path is None:
            return
        import json
        from pathlib import Path

        p = Path(self._persist_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = self.snapshot()
        payload["_counter"] = self._counter
        payload["side_effect_log"] = self.side_effect_log
        p.write_text(json.dumps(payload, default=str), encoding="utf-8")

    def _load_persisted(self) -> None:
        import json
        from pathlib import Path

        p = Path(self._persist_path)
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        self.restore(data)
        self._counter = data.get("_counter", self._counter)
        self.side_effect_log = data.get("side_effect_log", [])

    def snapshot(self) -> dict[str, Any]:
        """Deep-ish snapshot of the world for checkpoints / reports."""
        import copy

        return {
            "flights": copy.deepcopy(self.flights),
            "hotels": copy.deepcopy(self.hotels),
            "cars": copy.deepcopy(self.cars),
            "payments": copy.deepcopy(self.payments),
            "refunds": copy.deepcopy(self.refunds),
            "emails": copy.deepcopy(self.emails),
            "crm_records": copy.deepcopy(self.crm_records),
            "intent_effects": copy.deepcopy(self.intent_effects),
            "side_effect_count": len(self.side_effect_log),
        }

    def restore(self, snap: dict[str, Any]) -> None:
        """Restore Tier-1 style state from a snapshot (used by recovery)."""
        import copy

        self.flights = copy.deepcopy(snap.get("flights", {}))
        self.hotels = copy.deepcopy(snap.get("hotels", {}))
        self.cars = copy.deepcopy(snap.get("cars", {}))
        self.payments = copy.deepcopy(snap.get("payments", {}))
        self.refunds = copy.deepcopy(snap.get("refunds", {}))
        self.emails = copy.deepcopy(snap.get("emails", {}))
        self.crm_records = copy.deepcopy(snap.get("crm_records", {}))
        self.intent_effects = copy.deepcopy(snap.get("intent_effects", {}))

    def summary(self) -> dict[str, Any]:
        """Compact human-facing summary used in reports and the terminal UI."""
        def _statuses(d: dict[str, dict[str, Any]]) -> dict[str, str]:
            return {k: v.get("status", "?") for k, v in d.items()}

        return {
            "flights": _statuses(self.flights),
            "hotels": _statuses(self.hotels),
            "cars": _statuses(self.cars),
            "payments": {k: v.get("status", "?") for k, v in self.payments.items()},
            "refunds": {k: v.get("status", "?") for k, v in self.refunds.items()},
            "emails": {k: v.get("type", "email") for k, v in self.emails.items()},
            "crm_records": {k: v.get("status", "?") for k, v in self.crm_records.items()},
        }


class BaseAdapter:
    """Common adapter machinery: failure-injection hooks, idempotent effects.

    Subclasses implement :meth:`_do_execute` for the happy path. This base class
    weaves in the failure injector so any tool can simulate errors, timeouts,
    unknown outcomes, phantom success, malformed results and crashes.
    """

    #: tool names this adapter serves
    tools: tuple[str, ...] = ()

    def __init__(self, world: MockWorldState,
                 injector: Optional[FailureInjector] = None) -> None:
        self.world = world
        self.injector = injector or FailureInjector()

    # -- to be overridden -----------------------------------------------------

    async def _do_execute(self, intent: ActionIntent) -> ToolResult:  # pragma: no cover
        raise NotImplementedError

    def _postcondition(self, intent: ActionIntent,
                       state: dict[str, Any]) -> VerificationResult:
        """Default: pass. Subclasses override with real state checks."""
        return VerificationResult(passed=True, detail="no postcondition")

    # -- adapter contract -----------------------------------------------------

    async def execute(self, intent: ActionIntent) -> ToolResult:
        start = time.perf_counter()

        # 1) Failure injection BEFORE any side effect.
        injected = self.injector.check(intent)
        if injected is not None:
            result = self._apply_injection(intent, injected, start)
            if result is not None:
                return result

        # 2) Idempotent replay: if this intent already produced an effect,
        #    return the recorded result instead of duplicating it.
        prior = self.world.intent_effects.get(str(intent.intent_id))
        if prior is not None:
            return ToolResult(
                intent_id=intent.intent_id,
                outcome=Outcome.SUCCESS,
                data=prior["data"],
                latency_ms=(time.perf_counter() - start) * 1000.0,
            )

        # 3) Happy path.
        result = await self._do_execute(intent)

        # 4) Post-side-effect crash injection (side effect applied, response lost).
        injected_after = self.injector.check(intent, phase="after")
        if injected_after is not None and injected_after.mode == "crash":
            # The effect is already durable in the world; simulate lost response.
            raise CrashSignal(
                f"crash after side effect for {intent.step_id}",
                intent_id=str(intent.intent_id),
                effect_applied=True,
            )

        result.latency_ms = (time.perf_counter() - start) * 1000.0
        return result

    def _apply_injection(
        self, intent: ActionIntent, injected: InjectedFailure, start: float
    ) -> Optional[ToolResult]:
        """Translate an injected failure into a ToolResult / exception.

        Returns ``None`` if execution should proceed to the happy path
        (e.g. ``after``-phase crashes are handled post-effect).
        """
        latency = (time.perf_counter() - start) * 1000.0
        mode = injected.mode

        if mode == "crash":
            # Crash BEFORE the side effect: nothing applied, response never sent.
            raise CrashSignal(
                f"crash before side effect for {intent.step_id}",
                intent_id=str(intent.intent_id),
                effect_applied=False,
            )

        if mode == "error":
            return ToolResult(
                intent_id=intent.intent_id,
                outcome=Outcome.FAILURE,
                error_message=injected.message or "injected tool error",
                failure_class=injected.failure_class or FailureClass.BUSINESS_RULE,
                latency_ms=latency,
            )

        if mode == "timeout":
            # Timeout BEFORE server execution: no side effect, retryable.
            # We do NOT apply the effect. Treated as UNKNOWN if the spec calls for
            # "we can't tell"; F04 says retry safely (no effect happened).
            if injected.effect_applied:
                # Timeout AFTER the server acted: effect applied, response lost.
                return None  # proceed to apply, then raise unknown below
            return ToolResult(
                intent_id=intent.intent_id,
                outcome=(
                    Outcome.UNKNOWN if injected.as_unknown else Outcome.FAILURE
                ),
                error_message="request timed out before server execution",
                failure_class=FailureClass.TIMEOUT,
                latency_ms=latency,
            )

        if mode == "unknown":
            # Simulate: server may or may not have acted. Optionally record a
            # hidden server-side effect that a status query can later discover.
            if injected.effect_applied:
                self._record_hidden_effect(intent)
            return ToolResult(
                intent_id=intent.intent_id,
                outcome=Outcome.UNKNOWN,
                error_message="no response received; outcome unknown",
                failure_class=FailureClass.TIMEOUT,
                latency_ms=latency,
            )

        if mode == "malformed":
            return ToolResult(
                intent_id=intent.intent_id,
                outcome=Outcome.FAILURE,
                error_message="malformed tool result",
                failure_class=FailureClass.MALFORMED,
                data={"__malformed__": True},
                latency_ms=latency,
            )

        if mode == "phantom_success":
            # Reports success but does NOT create the real side effect, so the
            # postcondition check will fail.
            return ToolResult(
                intent_id=intent.intent_id,
                outcome=Outcome.SUCCESS,
                data={"phantom": True},
                latency_ms=latency,
            )

        if mode == "rate_limit":
            return ToolResult(
                intent_id=intent.intent_id,
                outcome=Outcome.FAILURE,
                error_message="rate limited",
                failure_class=FailureClass.RATE_LIMIT,
                latency_ms=latency,
            )

        # Unknown injection mode -> proceed to happy path.
        return None

    def _record_hidden_effect(self, intent: ActionIntent) -> None:
        """Apply the real side effect but hide it from the immediate response.

        Used by 'unknown outcome where the server actually acted' so that a later
        ``query_status`` can discover the effect. Subclasses that have meaningful
        state override this. Default: run the normal effect synchronously.
        """
        import asyncio

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:  # pragma: no cover
            loop = None
        # Best-effort synchronous application via the subclass happy path.
        coro = self._do_execute(intent)
        if loop and loop.is_running():
            # We are already inside the running loop; schedule + wait is unsafe,
            # so apply directly by driving the coroutine to completion.
            try:
                coro.send(None)
            except StopIteration as stop:
                _ = stop.value
        else:  # pragma: no cover - not used in async tests
            asyncio.run(coro)

    async def query_status(self, intent_id: UUID) -> OutcomeQuery:
        """Look up whether a prior intent's side effect exists in the world."""
        effect = self.world.intent_effects.get(str(intent_id))
        if effect is None:
            return OutcomeQuery(
                intent_id=intent_id,
                found=False,
                outcome=Outcome.FAILURE,
                detail="no side effect found for intent",
            )
        return OutcomeQuery(
            intent_id=intent_id,
            found=True,
            outcome=Outcome.SUCCESS,
            data=effect["data"],
            detail=f"found {effect['kind']} {effect['entity_id']}",
        )

    async def verify_postcondition(
        self, intent: ActionIntent, state: dict[str, Any]
    ) -> VerificationResult:
        return self._postcondition(intent, state)
