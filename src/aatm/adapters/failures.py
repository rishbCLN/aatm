"""Deterministic failure injection for the mock world.

The injector is configured with rules keyed by ``step_id`` and/or ``tool`` name.
Every rule is deterministic and reproducible: given the same workflow + injection
spec, the same failures occur. This backs the F01-F20 failure suite.

Injection modes
---------------
- ``error``           : tool returns FAILURE (optionally a specific failure class)
- ``timeout``         : request times out (before or after server execution)
- ``unknown``         : no response; outcome UNKNOWN (server may/may not have acted)
- ``phantom_success`` : returns SUCCESS but no real side effect (postcondition fails)
- ``malformed``       : returns a malformed result
- ``rate_limit``      : transient rate-limit failure (retryable)
- ``crash``           : raise CrashSignal (before or after the side effect)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..enums import FailureClass
from ..models import ActionIntent


class CrashSignal(Exception):
    """Raised to simulate a process crash during a tool call.

    ``effect_applied`` records whether the synthetic side effect was already
    durable when the crash happened. The coordinator surfaces this to recovery,
    but crash recovery must NOT trust it blindly - it reconciles via the adapter.
    """

    def __init__(self, message: str, *, intent_id: str, effect_applied: bool) -> None:
        super().__init__(message)
        self.intent_id = intent_id
        self.effect_applied = effect_applied


@dataclass
class InjectedFailure:
    """A resolved failure to apply for a given intent."""

    mode: str
    phase: str = "before"  # "before" | "after"
    message: Optional[str] = None
    failure_class: Optional[FailureClass] = None
    effect_applied: bool = False
    as_unknown: bool = True
    # Only fire on the Nth attempt (1-indexed); None = every attempt.
    only_attempt: Optional[int] = None
    # Fire on attempts up to and including this number, then stop (transient).
    until_attempt: Optional[int] = None


@dataclass
class FailureRule:
    """A configurable rule matched against steps/tools."""

    mode: str
    step_id: Optional[str] = None
    tool: Optional[str] = None
    phase: str = "before"
    message: Optional[str] = None
    failure_class: Optional[str] = None
    effect_applied: bool = False
    as_unknown: bool = True
    only_attempt: Optional[int] = None
    until_attempt: Optional[int] = None

    def matches(self, intent: ActionIntent) -> bool:
        if self.step_id is not None and self.step_id != intent.step_id:
            return False
        if self.tool is not None and self.tool != intent.tool_name:
            return False
        return True


class FailureInjector:
    """Holds failure rules and tracks per-intent attempt counts."""

    def __init__(self, rules: Optional[list[FailureRule]] = None,
                 enabled: bool = True) -> None:
        self.rules: list[FailureRule] = rules or []
        self.enabled = enabled
        # attempt counter keyed by (intent step) so transient failures can clear.
        self._attempts: dict[str, int] = {}

    # -- configuration --------------------------------------------------------

    def add_rule(self, rule: FailureRule) -> None:
        self.rules.append(rule)

    @classmethod
    def from_spec(cls, spec: dict[str, Any]) -> "FailureInjector":
        """Build an injector from a parsed injection YAML/dict.

        Expected shape::

            enabled: true
            rules:
              - mode: error
                step: step-3
                failure_class: resource_unavailable
                message: CAR_UNAVAILABLE
        """
        enabled = bool(spec.get("enabled", True))
        rules: list[FailureRule] = []
        for raw in spec.get("rules", []) or []:
            rules.append(
                FailureRule(
                    mode=str(raw.get("mode")),
                    step_id=raw.get("step") or raw.get("step_id"),
                    tool=raw.get("tool"),
                    phase=raw.get("phase", "before"),
                    message=raw.get("message"),
                    failure_class=raw.get("failure_class"),
                    effect_applied=bool(raw.get("effect_applied", False)),
                    as_unknown=bool(raw.get("as_unknown", True)),
                    only_attempt=raw.get("only_attempt"),
                    until_attempt=raw.get("until_attempt"),
                )
            )
        return cls(rules=rules, enabled=enabled)

    # -- matching -------------------------------------------------------------

    def _attempt_key(self, intent: ActionIntent) -> str:
        return f"{intent.run_id}:{intent.step_id}:{intent.is_compensation}"

    def check(self, intent: ActionIntent, phase: str = "before") -> Optional[InjectedFailure]:
        """Return an :class:`InjectedFailure` if a rule fires for this intent.

        Attempt counting only advances on the ``before`` phase so that a single
        logical attempt does not double-count.
        """
        if not self.enabled:
            return None

        key = self._attempt_key(intent)
        if phase == "before":
            self._attempts[key] = self._attempts.get(key, 0) + 1
        attempt = self._attempts.get(key, 1)

        for rule in self.rules:
            if rule.phase != phase:
                continue
            if not rule.matches(intent):
                continue
            if rule.only_attempt is not None and attempt != rule.only_attempt:
                continue
            if rule.until_attempt is not None and attempt > rule.until_attempt:
                continue
            fc = (
                FailureClass(rule.failure_class)
                if rule.failure_class
                else None
            )
            return InjectedFailure(
                mode=rule.mode,
                phase=rule.phase,
                message=rule.message,
                failure_class=fc,
                effect_applied=rule.effect_applied,
                as_unknown=rule.as_unknown,
                only_attempt=rule.only_attempt,
                until_attempt=rule.until_attempt,
            )
        return None

    def reset(self) -> None:
        self._attempts.clear()
