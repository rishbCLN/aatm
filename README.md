# AATM — Agent Action Transaction Manager

> **Engineering prototype.** AATM is a reliability/evidence tool, not a
> certification mechanism. It makes no zero-risk guarantees, provides no
> regulatory or legal certification, and cannot truly roll back an irreversible
> real-world action. The demo world is entirely synthetic: no real payments,
> emails, bookings, or cloud resources are ever touched.

## 1. Problem

When an autonomous agent executes a multi-step sequence of tool calls with real
side effects (book a flight, charge a card, send an email) and reality breaks
partway through — a timeout, a crash, an ambiguous response — the agent can
duplicate a charge, orphan a booking, or silently leave the world in a
corrupted, half-finished state. LLMs are non-deterministic and cannot be trusted
to reason correctly about what they already did or how to safely undo it.

## 2. What AATM is

AATM is a deterministic runtime safety and recovery layer that wraps an agent's
tool calls. It registers durable intent **before** each side effect, classifies
each tool's reversibility, finds the irreversible "pivot," enforces idempotency,
verifies post-conditions, reconciles unknown outcomes, executes compensations in
the correct order, survives process crashes via a write-ahead log, and emits a
tamper-evident audit chain plus a human-readable reliability report. The agent
still decides *what* to do; AATM governs *how safely* it executes.

## 3. Architecture

```text
                          ┌───────────────────────────────────────────┐
      workflow.yaml  ─────▶│  Planner                                   │
      tool_registry ─────▶│  parse → classify tiers → detect pivot →    │
      compensation ─────▶│  build saga plan (+ compensations)          │
                          └───────────────────┬───────────────────────┘
                                              │ TransactionPlan
                                              ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │  Coordinator (deterministic execution loop)                            │
   │                                                                        │
   │   for each step:                                                       │
   │     1. checkpoint world           ─────▶ CheckpointStore               │
   │     2. WAL: write PENDING intent  ─────▶ WAL  (durable, WAL-first)     │
   │     3. approval gate if Tier-3                                         │
   │     4. adapter.execute(intent)    ─────▶ Adapter ──▶ MockWorldState    │
   │     5. verify post-condition                                          │
   │     6. WAL: COMMITTED / FAILED / UNKNOWN                               │
   │     7. retry (idempotent) / reconcile / recover                        │
   │                                                                        │
   │   every transition ──────────────────▶ Audit log (hash-chained)        │
   └───────────────┬───────────────────────────────────┬────────────────────┘
                   │ failure / crash                    │ success
                   ▼                                     ▼
        ┌────────────────────┐               ┌────────────────────────┐
        │ RecoveryManager    │               │ Reporting              │
        │  • pre-pivot:      │               │  • scoring (100 pts)   │
        │    compensate in   │               │  • evidence JSON       │
        │    reverse order   │               │  • HTML report         │
        │  • post-pivot:     │               │  • audit verification  │
        │    forward recovery│               └────────────────────────┘
        │  • crash: WAL replay + status query │
        └────────────────────┘
```

## 4. Transaction semantics

A run is modeled as a saga `T1 → T2 → … → Tn`. Each side-effecting step has a
recovery strategy `Ci`. If step `Tj` fails **before** the pivot, previously
committed steps are compensated in reverse **completion order**
(`C(j-1) … C1`) — not numeric order, so parallel branches stay correct.

Guarantees the engine enforces:

- **WAL-first:** a durable `PENDING` intent is written before any side effect,
  so a crash can never leave an untracked action.
- **Idempotency:** every invocation carries a stable `intent_id`. Retries reuse
  it; adapters dedupe on it, so a retried charge cannot double-bill.
- **Explicit uncertainty:** an ambiguous result becomes `UNKNOWN` and triggers
  reconciliation (`query_status`) rather than a blind retry.
- **Fail-closed:** unknown/unclassified tools are treated as Tier-3, critical,
  approval-required.

## 5. Why compensation is not rollback

A rollback restores prior state exactly. Most real side effects cannot be
undone that way — you can only issue a **new, forward** action that offsets the
old one:

| Side effect          | "Compensation" is really…            |
|----------------------|--------------------------------------|
| Captured payment     | a **refund** (a new transaction)     |
| Confirmed booking    | a cancellation                        |
| Sent email           | a corrective follow-up email          |
| Local file edit      | *this one* is a true restore          |

AATM surfaces this distinction in both code (`CompensationStrategy` =
`restore` vs `compensate` vs `forward_fix`) and the report. It never claims an
exact restoration once a Tier-3 effect has occurred.

## 6. The pivot

The pivot is the first point after which pre-pivot state can no longer be
restored exactly. Resolution rules:

1. An explicit `is_pivot: true` on a step wins.
2. Otherwise the **first Tier-3 (irreversible)** action is the pivot.
3. If no Tier-3 action exists, there is no pivot.

Crossing the pivot switches recovery from rollback-equivalent compensation to
**forward recovery** (e.g., refund + cancellations). In the travel demo the
pivot is `step-4 charge_payment`.

## 7. Demo

Run the full narrated sequence (happy path, pre-pivot failure, post-pivot
failure, crash + recovery, approval denied, audit verification):

```bash
bash demo.sh          # or: make demo
```

Each scenario prints a live execution trace and writes an HTML + JSON evidence
report under `output/reports/`. See [docs/demo.md](docs/demo.md) for a
walkthrough of what each case proves.

## 8. Setup

Requires Python ≥ 3.11.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate   |  POSIX: source .venv/bin/activate
pip install -r requirements.txt      # runtime deps
pip install -e ".[dev]"              # editable install + test deps (optional)
```

No cloud credentials or external APIs are needed — everything runs against local
mock adapters.

## 9. CLI commands

Invoke via `python main.py <cmd>` (or the `aatm` entry point after
`pip install -e .`):

```bash
python main.py plan   workflows/travel_booking.yaml
python main.py run    workflows/travel_booking.yaml --report
python main.py run    workflows/travel_booking.yaml --inject injections/step3_failure.yaml
python main.py run    workflows/travel_booking.yaml --deny-pivot   # deny Tier-3 approval
python main.py recover      --run-id <RUN_ID>       # WAL replay + reconcile a crashed run
python main.py verify-audit --run-id <RUN_ID>       # check the tamper-evident hash chain
python main.py report       --run-id <RUN_ID>       # print a run's assessment summary
python main.py list-runs                            # list known runs
python main.py inspect-run  --run-id <RUN_ID>       # dump a run's audit events
```

Useful flags: `--fast` (zero retry backoff, for demos/tests), `--no-color`.

## 10. Tests

```bash
make test                       # full suite (unit + integration + property + e2e)
python -m pytest -q             # same, directly
python -m pytest tests/unit         -v
python -m pytest tests/integration  -v
python -m pytest tests/property     -v   # hypothesis-based invariant checks
python -m pytest tests/e2e          -v   # full demo scenarios A/B/C
```

## 11. Limitations

- Adapters and the "world" are in-memory mocks; there is no real external system.
- Storage is local JSON/JSONL files (no distributed durability or HA).
- No true rollback of irreversible actions — only forward business reversal.
- Dependency handling is linear + declared branches; no rich DAG scheduler.
- LLM compensation generation is **advisory only** and off by default; only
  explicit/registry/adapter compensations execute automatically.
- Approval is a simple gate (CLI flag / callback), not a full workflow UI.

## 12. Security notes

- **WAL-first + idempotency** are the core safety invariants: no side effect is
  ever issued without a prior durable intent, and retries/recovery dedupe on
  `intent_id` to prevent double execution.
- **Fail-closed:** unclassified tools default to irreversible/critical/approval.
- **Tamper-evident audit:** the audit log is an append-only SHA-256 hash chain;
  any edit to a past entry breaks verification (`verify-audit`).
- Unknown outcomes are never blindly retried; they are reconciled against
  authoritative external state first.
- No secrets or real PII are used anywhere in the demo or fixtures.

## 13. Relationship to the Survivability Engine

AATM and the **Survivability Engine** are separate, independently runnable
modules that may later sit under one AI-assurance platform:

- **Survivability Engine (pre-deployment):** deliberately break an agent, measure
  whether it survives, identify failure modes, and score resilience.
- **AATM (runtime):** protect the agent *while it executes* — record state,
  reconcile uncertainty, and compensate/recover when something fails.

This repository keeps the runtime transaction layer standalone.

## 14. Roadmap

Documented, not yet implemented:

- **V2:** PostgreSQL backend, Redis checkpointing, pluggable remote adapters,
  richer dependency graphs, approval web UI, multi-tenant isolation, signed run
  attestations, OpenTelemetry ingestion, LangGraph / OpenAI Agents SDK / generic
  MCP tool adapters.
- **V3:** automatic compensation-contract synthesis from observed behavior,
  formal recovery-policy checking, distributed transaction coordination, a
  runtime policy engine, and integration with the Survivability Engine.

## License

Apache-2.0. See [LICENSE](LICENSE).
