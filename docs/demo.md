# Demo Walkthrough

Run everything with:

```bash
bash demo.sh        # or: make demo
```

The script auto-selects a Python interpreter that has AATM's dependencies
installed (honoring `$PYTHON`), runs entirely against local mock adapters, and
writes HTML + JSON evidence reports to `output/reports/`. No network access or
credentials are required.

The travel workflow (`workflows/travel_booking.yaml`) has 7 steps; the pivot is
`step-4 charge_payment` (the first Tier-3, irreversible action):

```text
step-1 book_flight       T2 compensatable   → cancel_flight
step-2 book_hotel        T2 compensatable   → cancel_hotel
step-3 reserve_car       T2 compensatable   → cancel_car
step-4 charge_payment    T3 irreversible    → refund_payment   ◀ PIVOT
step-5 confirm_bookings  T1 fully_reversible
step-6 send_email        T3 irreversible    → send_correction_email
step-7 update_crm        T2 compensatable   → delete_database_record
```

## 0. Plan

```bash
python main.py plan workflows/travel_booking.yaml
```

Shows the classified tier per step, the resolved compensation, and the detected
pivot. Proves: parsing, reversibility classification, pivot detection.

## A. Happy path

```bash
python main.py run workflows/travel_booking.yaml --report
```

All 7 steps commit. Final state `COMPLETED (CONSISTENT)`, score 100/100, audit
chain valid. Proves: WAL-first execution, post-condition verification, audit
integrity on the success path.

## B. Failure BEFORE the pivot

```bash
python main.py run workflows/travel_booking.yaml \
    --inject injections/step3_failure.yaml --report
```

`reserve_car` fails at step 3 (car unavailable). Because the pivot has not been
crossed, the engine compensates in reverse completion order:
`cancel hotel (step-2) → cancel flight (step-1)`. No payment is made, no side
effects orphaned. Final state `ABORTED (CONSISTENT)`.

**What it proves:** rollback-equivalent compensation and correct reverse-order
execution.

## C. Failure AFTER the pivot

```bash
python main.py run workflows/travel_booking.yaml \
    --inject injections/post_pivot_timeout.yaml --report
```

Payment commits at step 4, then `confirm_bookings` times out at step 5. The
charge cannot be undone, so recovery is **forward**: a **refund** (a NEW
transaction) plus cancellations. The report shows
`exact_rollback_possible = false` and explicitly labels the refund as a new
transaction. Final state `ABORTED (BUSINESS-CONSISTENT)`.

**What it proves:** the engine never claims exact rollback after a Tier-3
effect; compensation ≠ rollback.

## D. Crash DURING payment, then recover

```bash
python main.py run workflows/travel_booking.yaml \
    --inject injections/crash_mid_payment.yaml         # exits non-zero (crash)
python main.py recover --run-id <RUN_ID>               # reconcile
```

The process crashes *after* the charge is dispatched but before the response is
recorded. On restart, `recover` replays the WAL, finds the pending
`charge_payment` intent, and queries the adapter by `intent_id`. It discovers the
payment already exists and marks it `committed` — **no duplicate charge**
(`duplicates_prevented ≥ 1`).

**What it proves:** crash survival, WAL replay, idempotent reconciliation.

## E. Approval denied at the pivot

```bash
python main.py run workflows/travel_booking.yaml --deny-pivot
```

The Tier-3 approval gate at step 4 is denied. No charge occurs; the reversible
work already done (flight, hotel, car) is compensated. Final state
`ABORTED (CONSISTENT)`.

**What it proves:** the human-in-the-loop gate for irreversible actions and
fail-safe abort.

## F. Tamper-evident audit

```bash
python main.py verify-audit --run-id <RUN_ID>
```

Verifies the append-only SHA-256 hash chain for a run. Editing any past entry
breaks the chain and `verify-audit` reports `INVALID` (see
`tests/integration/test_cli.py::test_cli_audit_tamper_detected`).

## Reports

Each `--report` run writes:

- `output/reports/<run_id>.report.json` — machine-readable evidence.
- `output/reports/<run_id>.report.html` — human-readable reliability report with
  the execution timeline, world-state diff, compensation ledger, score, and
  audit-verification result.

List all runs with `python main.py list-runs`.
