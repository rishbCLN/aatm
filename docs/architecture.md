# AATM Architecture

This document maps the conceptual model to the actual modules under
`src/aatm/`.

## Module map

```text
aatm/
├── config.py            AATMConfig: paths for output/, run/audit/checkpoint files
├── engine.py            AATMEngine: façade tying planner → coordinator → reporting
├── enums.py             All enums (Tier, Reversibility, Outcome, WALStatus, …)
├── models.py            Pydantic models (ToolDefinition, ActionIntent, …)
│
├── planner/
│   ├── parser.py          workflow YAML + registry → validated objects
│   ├── reversibility.py    classify each tool into a Tier (fail-closed)
│   ├── pivot_detector.py   locate the pivot (explicit → first Tier-3 → none)
│   └── saga_planner.py     build the ordered TransactionPlan + compensations
│
├── compensation/
│   ├── registry.py        verified compensation templates
│   ├── generator.py       LLM-suggested compensations (advisory, off by default)
│   ├── validator.py       enforce the authority hierarchy
│   └── engine.py          resolve the compensation for a given committed step
│
├── adapters/
│   ├── base.py            ToolAdapter protocol, BaseAdapter, MockWorldState
│   ├── failures.py        FailureInjector + CrashSignal (deterministic chaos)
│   ├── registry.py        AdapterRegistry: tool_name → adapter, shared world
│   └── mock_*.py          travel / payment / email / CRM mock adapters
│
├── runtime/
│   ├── coordinator.py     the deterministic execution loop (WAL-first)
│   ├── retry.py           idempotent retry with backoff + failure classification
│   └── recovery.py        RecoveryManager: pre/post-pivot + crash recovery
│
├── storage/
│   ├── wal.py             durable write-ahead log (intent states)
│   ├── checkpoints.py     per-step world snapshots
│   ├── audit_log.py       append-only SHA-256 hash-chained audit + verify
│   ├── idempotency.py     intent_id dedupe index
│   └── db.py              lightweight local JSON/JSONL persistence
│
├── verification/
│   ├── expressions.py     safe expression evaluator for post-conditions
│   └── post_conditions.py verify observed state against declared expectations
│
├── reporting/
│   ├── scoring.py         100-point reliability score + PASS/FAIL status
│   ├── explainer.py       human-readable narrative of what happened
│   └── evidence_report.py JSON + HTML (Jinja2) evidence report
│
└── cli/
    ├── commands.py        argparse CLI (plan/run/recover/verify-audit/…)
    └── visualize.py       terminal rendering of the execution trace
```

## Execution flow

1. **Plan.** `planner` parses the workflow and tool registry, classifies each
   tool's reversibility into a `Tier`, locates the pivot, and produces a
   `TransactionPlan` with a resolved compensation per side-effecting step.
2. **Execute.** `runtime.Coordinator` walks the plan. For every step it, in
   order: checkpoints the world, writes a `PENDING` WAL record **before** the
   side effect, runs the Tier-3 approval gate if needed, calls the adapter,
   verifies the post-condition, and writes a terminal WAL state
   (`COMMITTED` / `FAILED` / `UNKNOWN`). Every transition appends a hash-chained
   audit entry.
3. **Recover.** On failure/crash, `runtime.RecoveryManager` chooses the strategy
   (see `recovery-semantics.md`).
4. **Report.** `reporting` scores the run and renders JSON + HTML evidence,
   including audit-chain verification.

## Determinism

The transaction engine is deterministic at runtime. All randomness/chaos is
funneled through `adapters/failures.FailureInjector`, driven by an injection
YAML, so every scenario is reproducible. The mock world (`MockWorldState`) is
the single source of ground truth for tests and reports.
