"""Lightweight, dependency-free observability: metrics, structured logs, spans.

This is intentionally small and local-first (no OpenTelemetry/Prometheus
dependency). It provides three things the runtime threads through:

- :class:`Metrics` - named counters + timers (e.g. retries, reconciliations,
  compensations, circuit-breaker trips, duplicates prevented).
- :func:`get_logger` - a structured JSON logger (one JSON object per line) so
  logs are machine-parseable without extra tooling.
- :class:`Span` - a minimal timed span that emits a structured start/stop log and
  records its duration into :class:`Metrics`. Shaped like an OTel span so it can
  be swapped for a real tracer later.

Everything is deterministic and side-effect-light; logging is off by default (a
null handler) so tests stay quiet unless a handler is attached.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


class Metrics:
    """Named counters and timers collected during a run."""

    def __init__(self) -> None:
        self.counters: dict[str, float] = {}
        self.timers_ms: dict[str, list[float]] = {}

    def incr(self, name: str, value: float = 1.0) -> None:
        self.counters[name] = self.counters.get(name, 0.0) + value

    def observe_ms(self, name: str, ms: float) -> None:
        self.timers_ms.setdefault(name, []).append(ms)

    def snapshot(self) -> dict[str, Any]:
        timers = {
            name: {
                "count": len(values),
                "total_ms": round(sum(values), 3),
                "avg_ms": round(sum(values) / len(values), 3) if values else 0.0,
                "max_ms": round(max(values), 3) if values else 0.0,
            }
            for name, values in self.timers_ms.items()
        }
        return {"counters": dict(self.counters), "timers": timers}


# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------


class _JsonFormatter(logging.Formatter):
    """Render each record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        obj: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            obj.update(extra)
        return json.dumps(obj, default=str)


def get_logger(name: str = "aatm") -> logging.Logger:
    """Return a structured logger. Emits nothing until a handler is attached."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


def enable_console_logging(level: int = logging.INFO,
                           name: str = "aatm") -> logging.Logger:
    """Attach a JSON console handler (opt-in; used by the CLI/service)."""
    logger = logging.getLogger(name)
    logger.setLevel(level)
    # Replace any existing (non-null) stream handlers to avoid duplicates.
    logger.handlers = [h for h in logger.handlers
                       if isinstance(h, logging.NullHandler)]
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def log_event(logger: logging.Logger, msg: str, level: int = logging.INFO,
              **fields: Any) -> None:
    """Log a structured event with arbitrary key/value fields."""
    logger.log(level, msg, extra={"fields": fields})


# ---------------------------------------------------------------------------
# Spans
# ---------------------------------------------------------------------------


@contextmanager
def span(
    name: str,
    *,
    metrics: Optional[Metrics] = None,
    logger: Optional[logging.Logger] = None,
    **attributes: Any,
) -> Iterator[dict[str, Any]]:
    """A minimal timed span.

    Emits a structured start/stop log (if a logger is given), records the
    duration into ``metrics`` under ``span.<name>.ms``, and yields a mutable
    attribute dict the caller can annotate (e.g. outcome, attempt).
    """
    attrs: dict[str, Any] = dict(attributes)
    start = time.perf_counter()
    if logger is not None:
        log_event(logger, f"span.start {name}", span=name, **attrs)
    try:
        yield attrs
    finally:
        duration_ms = (time.perf_counter() - start) * 1000.0
        if metrics is not None:
            metrics.observe_ms(f"span.{name}.ms", duration_ms)
        if logger is not None:
            log_event(logger, f"span.stop {name}", span=name,
                      duration_ms=round(duration_ms, 3), **attrs)
