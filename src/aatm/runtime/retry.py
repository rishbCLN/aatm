"""Retry policy engine.

Retries are only for declared transient failures (spec section 16). Retries
preserve the same logical ``intent_id`` (idempotency). A timeout is NEVER treated
as proof that an external side effect did not happen - that is the reconciler's
job, not the retry engine's.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from ..enums import RETRYABLE_FAILURES, FailureClass
from ..models import RetryPolicy


def is_retryable(failure_class: Optional[FailureClass], policy: RetryPolicy) -> bool:
    """Decide whether a failure is retryable under a policy.

    A failure is retryable when it is in the globally transient set AND either the
    policy's ``retry_on`` list is empty (retry all transient) or explicitly lists
    the class.
    """
    if failure_class is None:
        return False
    if failure_class not in RETRYABLE_FAILURES:
        return False
    if not policy.retry_on:
        return True
    return failure_class in policy.retry_on


def backoff_delay_ms(policy: RetryPolicy, attempt: int) -> float:
    """Compute the delay before the given (1-indexed) retry attempt."""
    base = policy.backoff_ms
    if policy.backoff == "none":
        return 0.0
    if policy.backoff == "fixed":
        return float(base)
    # exponential: base * 2^(attempt-1)
    return float(base * (2 ** max(0, attempt - 1)))


async def sleep_backoff(policy: RetryPolicy, attempt: int,
                        scale: float = 0.001) -> None:
    """Sleep for the backoff delay. ``scale`` converts ms->s (test-tunable).

    Tests use a tiny scale so retries do not slow the suite; production would use
    the real 0.001 (ms -> s).
    """
    delay_ms = backoff_delay_ms(policy, attempt)
    if delay_ms > 0:
        await asyncio.sleep(delay_ms * scale)
