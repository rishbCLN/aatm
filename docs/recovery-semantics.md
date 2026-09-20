# Recovery Semantics

AATM has three distinct recovery paths. Which one runs depends on **when** the
failure happens relative to the pivot, and whether the process is still alive.

## 1. Pre-pivot failure — compensation (rollback-equivalent)

A step fails *before* the pivot is crossed. No irreversible action has occurred,
so the engine can return the world to a consistent, business-equivalent state by
compensating every completed side-effecting step in **reverse completion
order**.

```text
committed:  T1(flight)  T2(hotel)  T3(car ✗ fails)
recovery:   compensate C2(cancel hotel) → C1(cancel flight)
final:      ABORTED (CONSISTENT), 0 orphaned side effects
```

- Order is completion order reversed, tracked by a completion counter — not
  numeric step order — so parallel branches compensate correctly.
- Each compensation is itself an adapter call (never a hidden direct mutation)
  and its post-condition is verified.
- Implemented in `runtime/coordinator.py::_recover` +
  `compensation/engine.py::plan_compensations`.

## 2. Post-pivot failure — forward recovery (NOT rollback)

A step fails *after* the pivot (e.g. `charge_payment` committed, then
`confirm_bookings` times out). The Tier-3 side effect **stands** — it cannot be
undone. Recovery is a business-level reversal composed of **new forward
transactions**:

```text
committed:  T1 T2 T3 T4(charge ✓ PIVOT)  T5(confirm ✗ timeout)
recovery:   forward_fix on T4 → refund (a NEW payment transaction)
            + compensate T3, T2, T1 (cancellations)
final:      ABORTED (BUSINESS-CONSISTENT)
            EXACT ROLLBACK: NOT POSSIBLE  (reported explicitly)
```

The report marks `exact_rollback_possible = false` and the refund is labeled a
new transaction, never an "undo."

## 3. Crash recovery — WAL replay + reconciliation

The process dies mid-step. Durable state survives in the per-run WAL, checkpoint
store, and audit log. On `recover`, `runtime/recovery.py::RecoveryManager`:

1. Appends `CRASH_RECOVERY_START` to the audit chain.
2. Reads all **non-terminal** WAL intents (`PENDING`/`RUNNING`/`UNKNOWN`).
3. For each, calls `adapter.query_status(intent_id)` to ask the **authoritative
   external system** what actually happened:
   - **found + success** → mark `COMMITTED`, adopt the discovered result. The
     effect already exists, so it is **not re-issued** (`duplicates_prevented++`).
   - **not found** → mark `FAILED`; safe to treat as never executed.
4. Appends `CRASH_RECOVERY_COMPLETE` with the reconciliation summary.

> An unknown or pending intent is **never** blindly retried into a duplicate
> side effect. The status query, keyed by the stable `intent_id`, is the
> authority.

## Idempotency: the safety net under all three paths

Every intent carries a stable `intent_id` (and optional `idempotency_key`
derived from run/step variables). Adapters record effects by `intent_id`; a
replay of the same intent returns the recorded result instead of acting again.
This is what makes retries, reconciliation, and crash replay safe.

## Failure classification → retry decision

`runtime/retry.py` uses `FailureClass` to decide retryability:

- **Retryable (transient):** `timeout`, `connection`, `rate_limit`,
  `server_error` → idempotent retry with backoff.
- **Non-retryable:** `validation`, `authorization`, `business_rule`,
  `resource_unavailable`, `malformed` → stop and recover.
- **`unknown` outcome** → reconcile (status query) before any retry.

## Escalation

If a compensation itself fails, the run is marked `INCONSISTENT` and an
`ESCALATION` audit event is written — AATM does not pretend a broken
compensation succeeded. `manual_escalation` compensations always require a human.
