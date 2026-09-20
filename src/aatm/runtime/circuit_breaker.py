"""Per-tool circuit breaker.

A circuit breaker stops hammering a tool that is consistently failing. After a
tool accumulates ``failure_threshold`` consecutive failures (errors, timeouts, or
unresolved-unknowns) the breaker trips OPEN and further calls fail fast instead of
retrying into the same wall. After ``cooldown_s`` the breaker moves to HALF_OPEN
and permits a limited number of trial calls; a success closes it, a failure
re-opens it.

The breaker is deterministic and clock-injectable so it is fully testable without
real time passing.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Callable


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class _ToolState:
    __slots__ = ("state", "consecutive_failures", "opened_at", "half_open_calls")

    def __init__(self) -> None:
        self.state = BreakerState.CLOSED
        self.consecutive_failures = 0
        self.opened_at = 0.0
        self.half_open_calls = 0


class CircuitBreaker:
    """Tracks per-tool health and decides whether a call may proceed."""

    def __init__(
        self,
        failure_threshold: int = 5,
        cooldown_s: float = 30.0,
        half_open_trials: int = 1,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self.half_open_trials = max(1, half_open_trials)
        self._clock = clock
        self._tools: dict[str, _ToolState] = {}
        # Observability: count how many times each tool's breaker tripped open.
        self.trips: dict[str, int] = {}

    def _get(self, tool: str) -> _ToolState:
        st = self._tools.get(tool)
        if st is None:
            st = _ToolState()
            self._tools[tool] = st
        return st

    def state(self, tool: str) -> BreakerState:
        return self._get(tool).state

    def allow(self, tool: str) -> bool:
        """Return True if a call to ``tool`` may proceed right now."""
        st = self._get(tool)
        if st.state is BreakerState.CLOSED:
            return True
        if st.state is BreakerState.OPEN:
            if (self._clock() - st.opened_at) >= self.cooldown_s:
                # Cooldown elapsed -> allow a trial.
                st.state = BreakerState.HALF_OPEN
                st.half_open_calls = 1
                return True
            return False
        # HALF_OPEN: allow a bounded number of trial calls.
        if st.half_open_calls < self.half_open_trials:
            st.half_open_calls += 1
            return True
        return False

    def record_success(self, tool: str) -> None:
        st = self._get(tool)
        st.state = BreakerState.CLOSED
        st.consecutive_failures = 0
        st.half_open_calls = 0

    def record_failure(self, tool: str) -> bool:
        """Record a failure. Returns True if the breaker just tripped OPEN."""
        st = self._get(tool)
        st.consecutive_failures += 1
        if st.state is BreakerState.HALF_OPEN:
            # A trial failed -> re-open immediately.
            st.state = BreakerState.OPEN
            st.opened_at = self._clock()
            st.half_open_calls = 0
            self.trips[tool] = self.trips.get(tool, 0) + 1
            return True
        if (
            st.state is BreakerState.CLOSED
            and st.consecutive_failures >= self.failure_threshold
        ):
            st.state = BreakerState.OPEN
            st.opened_at = self._clock()
            self.trips[tool] = self.trips.get(tool, 0) + 1
            return True
        return False
