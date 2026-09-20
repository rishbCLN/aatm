# AGENT ACTION TRANSACTION MANAGER (AATM)
## End-to-End Autonomous Build Instructions for Claude Opus 4.8 Max

> **Execution mode:** Autonomous engineering build. Do not merely describe the implementation. Build the repository, implement the code, run the tests, fix failures, generate the demo, and leave a runnable project.
>
> **Primary objective:** Create a working, demo-ready **Agent Action Transaction Manager (AATM)**: a runtime safety/recovery layer around AI-agent tool calls that tracks intent durably, classifies reversibility, creates and executes compensating actions, survives process crashes, reconciles unknown outcomes, enforces idempotency, and produces a tamper-evident execution record plus a reliability/evidence report.
>
> **Reference specification:** The user-provided AATM specification is the conceptual source of truth for the saga model, reversibility tiers, pivot transaction, idempotency, WAL, checkpointing, compensation, audit trail, sample workflows, failure injection and demo structure. Use it as the conceptual baseline. Do not blindly copy unsafe assumptions; the implementation rules below are authoritative wherever they improve safety or testability.

---

# 0. NON-NEGOTIABLE EXECUTION RULES

Claude must follow these rules throughout the build.

1. **Build, do not just discuss.** Every requested component must exist as code or as a concrete repository artifact.
2. **Run code after meaningful implementation.** Do not accumulate untested code for the entire build.
3. **Test continuously.** Fix failing tests before moving to dependent phases.
4. **Use deterministic mechanisms wherever possible.** LLMs may assist with inference, but the transaction engine must remain deterministic at runtime.
5. **Never execute arbitrary LLM-invented side effects against real external services.** All provided demos use local/mock adapters.
6. **No real payments, real emails, real bookings, real production cloud resources, or real customer data.** The demo environment must be completely synthetic.
7. **No fake success.** A test only passes if the final state and audit trail prove the expected outcome.
8. **Never trust the agent's own statement that an action is reversible.** The transaction layer decides using registered tool metadata, explicit compensation contracts, and observable state.
9. **Never represent compensation as physical rollback.** Compensation is a semantic forward action. Examples: refund, cancellation, correction message.
10. **Do not claim regulatory certification.** The report is an engineering/reliability evidence artifact, not legal advice, certification, or a regulator approval.
11. **Do not hide uncertainty.** Unknown outcomes must become explicit `UNKNOWN` state and trigger reconciliation policies.
12. **Do not silently skip dangerous edge cases.** Implement or explicitly mark unsupported behavior and fail safely.
13. **Do not optimize for raw feature count.** Correctness, observability, reproducibility and a visceral demo are more important.
14. **Use a clean local-first architecture.** Demo must run without cloud credentials or external APIs.
15. **At the end, run the complete quality gate suite and the full demo from a clean checkout-equivalent state.**

---

# 1. PRODUCT DEFINITION

## 1.1 What AATM is

AATM is a runtime transaction/recovery layer for AI agents that execute tool calls with real or simulated side effects.

Its central question is:

> **When an autonomous agent partially executes a multi-step action sequence and reality breaks, can the system recover to a consistent state without duplicating, orphaning, or silently corrupting side effects?**

The core primitives are:

- intent registration
- write-ahead logging
- checkpoints
- deterministic tool classification
- reversibility tiers
- pivot detection
- idempotency
- compensation planning
- compensation execution
- post-condition verification
- unknown-outcome reconciliation
- crash recovery
- append-only tamper-evident audit logging
- experiment/failure injection
- evidence/report generation

## 1.2 What AATM is not

Do NOT turn the project into any of the following:

- a generic workflow builder
- a task manager
- a chatbot
- an LLM observability dashboard
- a generic distributed transaction database
- a full Temporal/LangGraph replacement
- a production payment gateway
- a legal compliance certification system
- an autonomous system that is allowed to invent arbitrary destructive compensations in production

## 1.3 Relationship to the separate Survivability Engine

Treat the projects as separate modules/repositories for now.

### Survivability Engine
Pre-deployment:

> deliberately break the agent -> measure whether it survives -> identify failure modes -> score resilience

### AATM
Runtime:

> protect the agent while it executes -> record state -> reconcile uncertainty -> compensate/recover when something fails

They may later sit under one broader AI assurance platform, but this build must keep the runtime transaction layer independently runnable and testable.

---

# 2. THE CONCEPTUAL MODEL

## 2.1 Saga model

For an execution sequence:

`T1 -> T2 -> T3 -> ... -> Tn`

each completed side-effecting action must have a recovery strategy:

`C1, C2, C3, ...`

If `Tj` fails before the irreversible boundary, completed compensations execute in reverse **completion order**:

`C(j-1), ..., C1`

Do not assume numerical step order when parallel branches exist.

## 2.2 Compensation is not rollback

Examples:

- database/file snapshot restore: state restoration
- created booking -> cancellation
- captured payment -> refund
- sent email -> corrective email (not an undo)
- external webhook -> compensating/void event if the receiver supports it

The engine must expose this distinction in code and in the UI/report.

## 2.3 Reversibility tiers

Implement exactly these three conceptual tiers for the initial version:

| Tier | Meaning | Example | Default recovery |
|---|---|---|---|
| 1 | Fully reversible/local | file edit with checkpoint, local state mutation | restore checkpoint/snapshot |
| 2 | Compensatable | booking, internal DB write, internal resource reservation | execute semantic inverse/compensation |
| 3 | Irreversible externally | payment capture, email, external webhook | approval gate + forward recovery/business-level reversal |

Tier is not merely the same thing as side-effect class. Store both `side_effect_scope` and `reversibility`.

## 2.4 Pivot

The pivot is the first point after which the entire pre-pivot state cannot be restored exactly.

Rules:

1. Explicit `is_pivot: true` wins.
2. Otherwise, first Tier-3 action is the default pivot.
3. If no Tier-3 action exists, there is no pivot.
4. Crossing a pivot changes the recovery strategy from rollback-equivalent compensation to forward recovery.
5. The engine must not claim an exact restoration after a Tier-3 side effect has occurred.

## 2.5 Idempotency

Every action invocation gets a stable `intent_id`.

Properties:

- retries of the same logical invocation use the same `intent_id`
- adapters must support deduplication where possible
- if a tool is naturally non-idempotent, AATM must wrap it with durable intent tracking and reconcile before retrying
- an unknown outcome must never blindly issue the same external side effect again

---

# 3. SAFETY CORRECTION TO THE REFERENCE SPEC: COMPENSATION GENERATION

The reference specification proposes LLM-powered auto-generation of compensations from tool schemas. Preserve this as an **optional intelligence capability**, but do NOT make it the unconditional runtime authority.

## 3.1 Compensation authority hierarchy

Use this exact precedence:

1. Explicit compensation declared by the workflow/tool owner
2. Verified compensation template from the tool registry
3. Registered adapter contract
4. LLM-suggested compensation
5. No compensation

Only levels 1–3 may execute automatically for production-grade paths.

Level 4 is advisory unless a human explicitly approves it in the demo/control interface.

## 3.2 Why

A JSON schema cannot prove semantic reversibility. A model may infer a plausible-looking inverse that is wrong, incomplete, unsafe, or impossible.

Therefore the generator is an assistant to engineering contracts, not the transaction authority.

## 3.3 Compensation object

Use:

```json
{
  "strategy": "restore|compensate|forward_fix|manual_escalation|none",
  "tool": "cancel_booking",
  "parameters": {},
  "mapping": {},
  "confidence": 0.0,
  "source": "explicit|registry|adapter|llm",
  "requires_approval": false,
  "verification": {
    "type": "state_query",
    "expression": "booking.status == 'cancelled'"
  },
  "risks": []
}
```

---

# 4. SUCCESS CRITERIA

The system is only considered complete when it can reliably demonstrate all of the following:

1. Parse a workflow definition.
2. Build a transaction/saga plan.
3. Correctly classify tool reversibility.
4. Correctly locate the pivot.
5. Persist intent BEFORE the side effect.
6. Persist checkpoint state before a step executes.
7. Execute a tool through an adapter interface.
8. Verify post-conditions.
9. Handle tool error.
10. Handle timeout.
11. Handle unknown outcome.
12. Retry idempotently.
13. Execute compensations in correct reverse completion order.
14. Stop exact-rollback semantics at the pivot.
15. Perform forward recovery after the pivot.
16. Survive a process crash.
17. Reconcile pending WAL entries on restart.
18. Detect phantom success.
19. Detect compensation failure and escalate.
20. Produce a tamper-evident audit chain.
21. Produce a machine-readable result.
22. Produce a human-readable HTML reliability/evidence report.
23. Run the complete demo without external credentials.
24. Pass all unit, integration, property-based and end-to-end tests.

---

# 5. REPOSITORY STRUCTURE

Create exactly this baseline structure, extending it only when necessary:

```text
agent-action-transaction-manager/
├── README.md
├── LICENSE
├── pyproject.toml
├── requirements.txt
├── .gitignore
├── Makefile
├── demo.sh
├── main.py
├── src/
│   └── aatm/
│       ├── __init__.py
│       ├── models.py
│       ├── enums.py
│       ├── config.py
│       ├── planner/
│       │   ├── __init__.py
│       │   ├── saga_planner.py
│       │   ├── pivot_detector.py
│       │   └── reversibility.py
│       ├── runtime/
│       │   ├── __init__.py
│       │   ├── coordinator.py
│       │   ├── executor.py
│       │   ├── retry.py
│       │   └── recovery.py
│       ├── storage/
│       │   ├── __init__.py
│       │   ├── wal.py
│       │   ├── checkpoints.py
│       │   ├── idempotency.py
│       │   └── audit_log.py
│       ├── compensation/
│       │   ├── __init__.py
│       │   ├── engine.py
│       │   ├── registry.py
│       │   ├── generator.py
│       │   └── validator.py
│       ├── adapters/
│       │   ├── __init__.py
│       │   ├── base.py
│       │   ├── mock_travel.py
│       │   ├── mock_payment.py
│       │   ├── mock_email.py
│       │   └── mock_crm.py
│       ├── verification/
│       │   ├── __init__.py
│       │   └── post_conditions.py
│       ├── reporting/
│       │   ├── __init__.py
│       │   ├── evidence_report.py
│       │   ├── scoring.py
│       │   └── templates/
│       │       ├── reliability_report.html
│       │       └── execution_trace.html
│       └── cli/
│           ├── __init__.py
│           └── commands.py
├── schemas/
│   ├── workflow.schema.json
│   ├── tool.schema.json
│   └── result.schema.json
├── knowledge_base/
│   ├── tool_registry.json
│   ├── compensation_templates.json
│   └── reversibility_rules.json
├── workflows/
│   ├── travel_booking.yaml
│   ├── cloud_provisioning.yaml
│   ├── financial_workflow.yaml
│   └── mixed_reversibility.yaml
├── injections/
│   ├── step3_failure.yaml
│   ├── post_pivot_timeout.yaml
│   └── crash_mid_payment.yaml
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── property/
│   ├── e2e/
│   └── fixtures/
├── examples/
│   └── custom_tool.py
├── output/
│   ├── audit/
│   ├── checkpoints/
│   ├── reports/
│   └── runs/
└── docs/
    ├── architecture.md
    ├── recovery-semantics.md
    ├── adapter-contract.md
    └── demo.md
```

---

# 6. CORE DATA MODELS

Use Pydantic models or equivalent strongly validated Python dataclasses.

## 6.1 ToolDefinition

Required fields:

```python
name: str
description: str
side_effect_scope: Literal["none", "local", "internal", "external"]
reversibility: Literal["fully_reversible", "compensatable", "irreversible"]
idempotent: bool
idempotency_mode: Literal["native", "wrapper", "none"]
parameters_schema: dict
returns_schema: dict
compensation_contract: Optional[dict]
postcondition_contract: Optional[dict]
timeout_ms: int
risk_level: Literal["low", "medium", "high", "critical"]
```

## 6.2 ActionIntent

```python
intent_id: UUID
run_id: UUID
workflow_id: str
step_id: str
tool_name: str
parameters_hash: str
parameters: dict
status: IntentStatus
created_at: datetime
started_at: Optional[datetime]
completed_at: Optional[datetime]
result: Optional[dict]
error: Optional[dict]
outcome: Literal["success", "failure", "unknown"]
```

## 6.3 StepExecution

Must record:

- step ID
- attempt number
- intent ID
- tier
- pivot flag
- precondition result
- action result
- postcondition result
- compensation strategy
- final state

## 6.4 CompensationExecution

Record:

- source step
- compensation intent ID
- source of compensation (`explicit`, `registry`, `adapter`, `llm`)
- approval state
- execution outcome
- verification outcome
- residual risk

## 6.5 WorkflowRun

States:

```text
PLANNED
RUNNING
WAITING_APPROVAL
RECOVERING
COMPLETED
ABORTED
FAILED
INCONSISTENT
UNKNOWN
TIMED_OUT
```

`INCONSISTENT` must be a terminal/manual-intervention state unless a deterministic recovery procedure exists.

---

# 7. WORKFLOW INPUT CONTRACT

Implement YAML input with the following fields.

```yaml
workflow:
  id: wf-travel-001
  name: Corporate Travel Booking
  description: Book a business trip
  agent: travel-booking-agent
  max_steps: 7
  timeout_seconds: 120
  failure_policy:
    on_step_failure: compensate_and_abort
    on_compensation_failure: escalate_human
    on_unknown_outcome: query_and_reconcile
    max_compensation_retries: 3

steps:
  - id: step-1
    name: Reserve Flight
    tool: book_flight
    parameters: {}
    depends_on: []
    preconditions: []
    postconditions:
      - expression: result.booking_status == 'confirmed'
        on_failure: compensate
    compensation: null
    is_pivot: false
    approval_required: false
    idempotency_key: user_id + flight_date
    retry:
      max_attempts: 2
      backoff: exponential
      backoff_ms: 250
      retry_on: [timeout, rate_limit]

variables:
  - name: user_id
    type: string
    initial_value: emp-4521

tools:
  - name: book_flight
    description: Reserve a flight seat
    parameters_schema: {}
    returns_schema: {}
    side_effect_scope: external
    reversibility: compensatable
    idempotent: false
    idempotency_mode: wrapper
    timeout_ms: 5000
    compensation_contract:
      tool: cancel_flight
      parameter_mapping:
        booking_id: result.booking_id

failure_injection:
  enabled: true
```

Validate the file against a JSON Schema before execution.

Malformed workflow input must return a structured error and non-zero CLI exit status; it must never cause a Python traceback by default.

---

# 8. TOOL ADAPTER CONTRACT

Every tool used by AATM must go through an adapter.

Implement:

```python
class ToolAdapter(Protocol):
    async def execute(self, intent: ActionIntent) -> ToolResult: ...
    async def query_status(self, intent_id: UUID) -> OutcomeQuery: ...
    async def verify_postcondition(self, intent: ActionIntent, state: dict) -> VerificationResult: ...
```

Compensation is also an adapter call, never a direct function call hidden inside the coordinator.

## Adapter requirements

Every adapter must expose:

- deterministic mock state
- execution
- status query by intent ID
- postcondition verification
- failure injection hooks
- idempotency behavior
- side-effect log

This allows the entire system to be tested without real APIs.

---

# 9. MOCK TOOL ENVIRONMENT

Build a complete local simulation for the travel workflow.

Required tools:

### `book_flight`
Creates a booking record.

### `cancel_flight`
Moves booking to `cancelled`.

### `book_hotel`
Creates reservation.

### `cancel_hotel`
Cancels reservation.

### `reserve_car`
Creates car reservation.

### `cancel_car`
Cancels reservation.

### `charge_payment`
Creates a captured payment transaction.

### `query_payment`
Returns transaction state by intent ID.

### `refund_payment`
Creates a refund transaction against the payment.

### `confirm_bookings`
Checks all reservations and payment state.

### `send_email`
Creates a synthetic email record.

### `send_correction_email`
Creates synthetic corrective email.

### `create_database_record`
Creates a CRM record.

### `delete_database_record`
Deletes a CRM record.

All mock tools must maintain a queryable global `MockWorldState`.

The final world state is the primary ground truth used by tests.

---

# 10. WRITE-AHEAD LOG

Implement a durable WAL backed by SQLite.

## 10.1 Invariant

**No side effect may execute before its intent is durable.**

Required ordering:

```text
WAL INTENT WRITTEN
       ↓
CHECKPOINT WRITTEN
       ↓
TOOL EXECUTION
       ↓
POST-CONDITION VERIFICATION
       ↓
WAL COMMIT/FAIL/UNKNOWN
```

## 10.2 WAL states

```text
PENDING
RUNNING
COMMITTED
FAILED
UNKNOWN
COMPENSATING
COMPENSATED
ESCALATED
```

## 10.3 WAL record

At minimum:

```json
{
  "seq": 17,
  "intent_id": "...",
  "run_id": "...",
  "step_id": "step-3",
  "tool": "reserve_car",
  "parameters_hash": "...",
  "tier": 2,
  "pivot": false,
  "status": "PENDING",
  "created_at": "..."
}
```

Never store plaintext secrets such as API keys in the demo WAL.

---

# 11. CHECKPOINTING

Before each side-effecting tool call, persist:

- completed step IDs
- workflow variables
- current saga state
- pivot state
- known tool outcomes
- world-state snapshot for mock tools
- current WAL sequence
- active compensation plan

Use SQLite for the demo checkpoint store.

Checkpoint writes must be atomic.

Provide:

```python
create_checkpoint(run_id, step_id) -> checkpoint_id
load_checkpoint(checkpoint_id) -> Snapshot
restore_checkpoint(checkpoint_id) -> None
```

For Tier 3 actions, checkpointing does not make the action reversible. It only captures pre-action state for recovery logic and evidence.

---

# 12. REVERSIBILITY CLASSIFIER

Implement deterministic rules first.

Priority:

1. explicit workflow declaration
2. tool registry
3. adapter metadata
4. fail-closed unknown classification

Unknown tools MUST default to:

```text
reversibility = unknown
risk = critical
approval_required = true
```

Do not have the LLM silently decide that an unknown side effect is safe.

Provide an optional LLM explanation field, but not LLM authority.

---

# 13. SAGA PLANNER

Implement `SagaPlanner.plan(workflow)`.

It must produce:

- ordered steps
- dependency graph
- reversibility tier per step
- pivot
- compensation strategy per step
- approval requirement
- retry policy
- unknown-outcome policy
- risk flags

## Planner validations

Reject the plan if:

- a Tier-3 tool has neither explicit approval nor globally enforced approval
- a pre-pivot Tier-2 action has no known compensation
- a non-idempotent retryable tool has no idempotency mechanism
- a post-pivot critical action has no recovery path
- a compensation contract references an unknown result field
- a compensation executes before the original action could possibly have completed

Warnings may be emitted for lower-risk issues; critical issues must block execution.

---

# 14. TRANSACTION COORDINATOR

This is the highest-priority runtime component.

Responsibilities:

1. load plan
2. assign run ID
3. establish execution state
4. enforce preconditions
5. write WAL intent
6. checkpoint
7. request approval if required
8. execute adapter
9. verify postcondition
10. commit WAL
11. continue or recover
12. generate final evidence

Pseudo-flow:

```text
for next eligible step:
    validate preconditions
    create intent_id
    persist WAL=PENDING
    create checkpoint
    if approval required:
        pause
        require explicit approval
    execute via adapter
    classify result: SUCCESS / FAILURE / UNKNOWN
    if SUCCESS:
        verify postcondition
        if verification fails:
            treat as FAILURE
        else:
            mark COMMITTED
            continue
    if FAILURE:
        invoke recovery
    if UNKNOWN:
        invoke reconciliation
```

---

# 15. UNKNOWN OUTCOME RECONCILIATION

This is mandatory.

Scenario:

```text
request sent
↓
external system performs action
↓
network dies
↓
AATM receives no response
```

AATM must NOT immediately retry.

Instead:

1. Keep original `intent_id`.
2. Query adapter status.
3. If succeeded -> commit original action.
4. If failed -> compensate/retry according to policy.
5. If status remains unknown -> enter `UNKNOWN` and apply configured policy.
6. Critical external actions -> escalate rather than guess.

This behavior must be explicitly tested.

---

# 16. RETRY POLICY

Implement retry only for declared transient failures.

Default retryable classes:

- timeout
- temporary connection failure
- rate limit
- 5xx-equivalent mock error

Default non-retryable classes:

- validation failure
- authorization failure
- business rule violation
- resource unavailable
- malformed request

Retries must preserve the same logical intent ID.

Never treat a timeout as proof that an external side effect did not happen.

---

# 17. COMPENSATION ENGINE

## 17.1 Planning

For every completed step before failure:

- determine compensation strategy
- resolve compensation source
- bind original result data
- validate parameter mappings
- validate compensation risk
- enqueue in reverse **completion order**

## 17.2 Execution

For each compensation:

1. create compensation intent ID
2. write WAL
3. execute via adapter
4. verify compensation postcondition
5. mark compensated
6. append audit event

## 17.3 Compensation failure

If compensation fails:

- retry according to declared compensation policy
- reconcile unknown outcomes
- if still unresolved, enter `INCONSISTENT`
- stop automatic compensation chaining
- generate explicit human intervention record

Do NOT recursively “compensate the compensation” unless a future version implements a separately verified recovery graph. Version 1 must escalate.

---

# 18. PIVOT / POST-PIVOT RECOVERY

Pre-pivot:

```text
failure -> compensate completed reversible/compensatable work -> abort or retry
```

Post-pivot:

```text
failure -> reconcile -> retry -> forward-fix/business reversal -> escalate
```

Example:

```text
book flight       Tier 2
book hotel        Tier 2
reserve car       Tier 2
charge payment    Tier 3  <-- PIVOT
confirm bookings  Tier 2 but now POST-PIVOT
send email        Tier 3 and POST-PIVOT
```

If `confirm_bookings` fails after payment:

1. reconcile confirmation call
2. retry safely
3. if impossible, initiate business-level reversal
4. refund payment through a NEW transaction
5. cancel reservations if required
6. verify final business state
7. report that exact rollback was impossible after the pivot

The UI/report must make this distinction visually obvious.

---

# 19. AUDIT TRAIL

Implement append-only JSONL with SHA-256 chaining.

Each entry must include:

```json
{
  "seq": 42,
  "timestamp": "...",
  "run_id": "...",
  "event": "ACTION_COMPLETE",
  "entity_id": "...",
  "payload_hash": "...",
  "prev_hash": "...",
  "hash": "..."
}
```

## Required events

- WORKFLOW_START
- PLAN_CREATED
- CHECKPOINT_CREATED
- ACTION_INTENT_CREATED
- ACTION_START
- ACTION_COMPLETE
- ACTION_FAILED
- ACTION_UNKNOWN
- POST_CONDITION_PASS
- POST_CONDITION_FAIL
- RETRY
- RECONCILIATION_START
- RECONCILIATION_RESULT
- COMPENSATION_PLANNED
- COMPENSATION_START
- COMPENSATION_COMPLETE
- COMPENSATION_FAILED
- PIVOT_REACHED
- APPROVAL_REQUESTED
- APPROVAL_GRANTED
- APPROVAL_DENIED
- ESCALATION
- WORKFLOW_COMPLETE
- WORKFLOW_ABORTED
- WORKFLOW_INCONSISTENT
- CRASH_RECOVERY_START
- CRASH_RECOVERY_COMPLETE

Implement `verify_audit_chain()` and test it by deliberately editing a log line and verifying detection.

---

# 20. RELIABILITY/EVIDENCE REPORT

Generate both:

1. JSON report
2. HTML report

The report must include:

- workflow metadata
- agent identifier
- run ID
- assessment/build version
- tool inventory
- reversibility classification
- pivot identification
- approval gates
- retries
- unknown outcomes
- compensation executions
- final world state
- audit chain integrity
- residual risk register
- failed assertions
- recovery timings
- experiment configuration
- exact failure scenarios executed

### Important wording rule

Never write:

> “This system is regulator-approved.”

Never write:

> “This proves legal compliance.”

Use wording like:

> “This report records the engineering controls and observed recovery behavior for the tested workflow.”

> “This is evidence for engineering review; it is not legal or regulatory certification.”

---

# 21. SCORING MODEL

Use a transparent engineering score, not a mystical AI score.

Total = 100.

### Transaction integrity — 30
- WAL correctness: 10
- state consistency: 10
- compensation correctness: 10

### Failure recovery — 25
- tool failures: 5
- timeout/unknown outcome handling: 5
- crash recovery: 5
- post-pivot forward recovery: 5
- compensation failure handling: 5

### Safety controls — 20
- pivot detection: 5
- approval gates: 5
- idempotency: 5
- fail-closed unknown tools: 5

### Verification — 15
- postconditions: 5
- phantom success detection: 5
- final-state verification: 5

### Audit/evidence — 10
- hash chain: 5
- report completeness: 5

## Mandatory score floors

The score MUST be overridden to `FAIL` if any of these occur:

- side effect executed before durable WAL intent
- duplicate non-idempotent side effect caused by retry
- compensation executed against the wrong object
- system falsely claims a Tier-3 action was rolled back
- crash leaves a pending side effect unreconciled without explicit UNKNOWN state
- audit-chain tampering goes undetected

Do not label a workflow “safe for production” purely from the numeric score. Use status:

```text
PASS
PASS_WITH_CONDITIONS
FAIL
BLOCKED
INCONSISTENT
```

---

# 22. REQUIRED FAILURE INJECTION SUITE

Implement these failure injectors:

| ID | Failure | Inject at | Expected behavior |
|---|---|---|---|
| F01 | immediate tool error | step 1 | abort, no compensation |
| F02 | tool error | step 2 | compensate step 1 |
| F03 | tool error | step 3 | compensate steps 2 then 1 |
| F04 | timeout before server execution | pre-pivot | retry safely |
| F05 | unknown outcome | pre-pivot | query by intent ID, then decide |
| F06 | phantom success | any step | postcondition fails, treat as failure |
| F07 | failure after pivot | step 5 | retry/forward-fix, not pretend rollback |
| F08 | compensation failure | step 3 recovery | retry/reconcile, then INCONSISTENT if unresolved |
| F09 | crash before tool call | any step | WAL remains PENDING; recovery should not duplicate |
| F10 | crash after tool side effect but before response | external action | reconcile, do not double execute |
| F11 | crash after compensation intent | recovery path | reconcile compensation before retry |
| F12 | malformed tool result | any step | fail safely |
| F13 | rate limit | transient tool | exponential backoff |
| F14 | dependency outage | multiple steps | retry/reconcile according to policy |
| F15 | schema mismatch | compensation | mark compensation stale; block execution |
| F16 | duplicate intent submission | same logical action | deduplicate |
| F17 | workflow timeout | long run | stop new work and recover completed pre-pivot work |
| F18 | parallel branch failure | branch A | compensate completed branch actions in reverse completion order |
| F19 | approval denied | Tier 3 | no side effect, compensate previous reversible work if configured |
| F20 | unknown tool | plan time | fail closed / approval required |

---

# 23. REQUIRED TESTS

Use `pytest`, `pytest-asyncio`, and `hypothesis`.

## 23.1 Unit tests

### WAL
- write entry
- update state
- replay pending entries
- atomicity
- duplicate intent handling

### idempotency
- same intent repeated
- duplicate execution blocked
- timeout then reconciliation

### classifier
- known Tier 1
- known Tier 2
- known Tier 3
- explicit override
- unknown tool -> fail closed

### pivot detector
- explicit pivot
- inferred first Tier 3
- no Tier 3 -> no pivot

### compensation planner
- registry template
- explicit override
- invalid mapping
- LLM suggestion marked advisory

### postconditions
- successful effect
- phantom success
- malformed result

### audit
- valid chain
- tampered chain
- missing event detection

## 23.2 Integration tests

1. Travel happy path.
2. Travel failure at step 2.
3. Travel failure at step 3.
4. Travel failure after payment pivot.
5. Crash at payment.
6. Unknown payment outcome.
7. Compensation failure.
8. approval denied.
9. duplicate run.
10. workflow timeout.

## 23.3 Property-based invariants

Generate small random saga graphs and verify:

### Invariant A — no pre-intent side effect
For every side-effect event, an earlier durable intent exists.

### Invariant B — no duplicate non-idempotent effects
Two executions with same intent ID cannot produce two effects.

### Invariant C — LIFO compensation
For a linear pre-pivot saga, completed actions are compensated in reverse completion order.

### Invariant D — pivot honesty
Once a Tier-3 action commits, the report cannot claim exact rollback to the old state.

### Invariant E — unknown is not failed
Unknown outcome must remain distinguishable from explicit failure.

### Invariant F — compensation failure is visible
A failed compensation can never silently produce `CONSISTENT`.

### Invariant G — audit integrity
Every committed run has a verifiable hash chain.

## 23.4 E2E tests

Run the full demo using only local mock adapters.

---

# 24. REQUIRED DEMO WORKFLOW

Use the seven-step travel workflow from the supplied reference, but make the entire implementation local/mock.

```text
1. Reserve Flight
2. Reserve Hotel
3. Reserve Car
4. Charge Corporate Card       <-- PIVOT
5. Confirm All Bookings
6. Send Confirmation Email
7. Update CRM
```

## Demo Case A — Failure before pivot

Inject car-unavailable at step 3.

Expected:

```text
Flight: booked -> cancelled
Hotel: reserved -> cancelled
Car: failed -> never created
Payment: never attempted
Email: never sent
CRM: never updated
Final state: CONSISTENT
```

Compensation order must be:

```text
cancel_hotel
cancel_flight
```

## Demo Case B — Failure after pivot

Inject timeout at step 5 after payment capture.

Expected:

```text
Payment: captured
Confirmation: unknown/failed
Retry: 3 attempts according to policy
If exhausted:
    refund payment
    cancel car
    cancel hotel
    cancel flight
Final state: business-consistent, NOT exact rollback
```

The report MUST explicitly state:

> “Payment capture crossed the pivot. Recovery used a refund, which is a new transaction rather than an undo of the original capture.”

## Demo Case C — Crash during payment

Kill the process immediately after `charge_payment` is dispatched but before the response is received.

Restart AATM.

Expected:

```text
WAL replay
→ detect PENDING payment intent
→ query payment system by intent_id
→ if NOT FOUND:
      mark payment FAILED
      compensate steps 1-3
→ if FOUND:
      mark payment COMMITTED
      continue post-pivot recovery
→ never issue blind duplicate charge
```

---

# 25. CLI REQUIREMENTS

Provide:

```bash
python -m aatm.cli plan workflows/travel_booking.yaml
python -m aatm.cli run workflows/travel_booking.yaml
python -m aatm.cli run workflows/travel_booking.yaml --inject injections/step3_failure.yaml
python -m aatm.cli recover --run-id <RUN_ID>
python -m aatm.cli verify-audit --run-id <RUN_ID>
python -m aatm.cli report --run-id <RUN_ID>
python -m aatm.cli list-runs
python -m aatm.cli inspect-run --run-id <RUN_ID>
```

`demo.sh` must run the canonical presentation sequence automatically.

---

# 26. DEMO VISUALIZATION

A terminal UI is sufficient; a lightweight Streamlit UI is optional.

The terminal visualization must show:

```text
AATM RUNTIME
────────────────────────────────────────────
RUN: wf-travel-001
PIVOT: STEP 4 — charge_payment

STEP 1  Reserve Flight       ✅
STEP 2  Reserve Hotel        ✅
STEP 3  Reserve Car           ❌ CAR_UNAVAILABLE

RECOVERY
  ↳ cancel_hotel             ✅
  ↳ cancel_flight            ✅

FINAL STATE: CONSISTENT
PAYMENT MOVED: NO
ORPHANED BOOKINGS: 0
```

For post-pivot recovery:

```text
STEP 4  Charge Payment       ✅  ₹45,200
                ↑ PIVOT
STEP 5  Confirm Bookings     ⚠ TIMEOUT

RECOVERY MODE: FORWARD RECOVERY
  ↳ retry x3
  ↳ refund_payment           ✅
  ↳ cancel_car               ✅
  ↳ cancel_hotel             ✅
  ↳ cancel_flight            ✅

FINAL STATE: BUSINESS-CONSISTENT
EXACT ROLLBACK: NOT POSSIBLE AFTER PIVOT
```

The visual distinction between **pre-pivot compensation** and **post-pivot forward recovery** is mandatory.

---

# 27. LLM COMPONENTS

The system may use an LLM for three optional capabilities:

1. compensation suggestion
2. natural-language explanation of recovery
3. report narrative generation

The LLM must NEVER be the sole authority for:

- whether an external action occurred
- whether a payment is captured
- whether a booking exists
- whether compensation succeeded
- whether an action is safe to repeat
- whether an exact rollback occurred

Those facts must come from deterministic state/adapters/WAL/postcondition verification.

## Compensation generator behavior

Input:

- tool schema
- tool description
- current parameters
- returned result schema
- side-effect metadata
- workflow goal

Output only a structured proposal.

Proposal is advisory unless its source is explicitly marked approved.

For unknown/irreversible tools with no verified compensation, output:

```json
{
  "strategy": "manual_escalation",
  "confidence": 0.0,
  "source": "llm",
  "requires_approval": true,
  "risks": ["No verified semantic compensation contract"]
}
```

---

# 28. OPTIONAL LLM EVALUATOR

Implement a non-authoritative evaluator that reads the execution trace and produces:

- what went wrong
- whether the recovery policy matched the failure class
- residual risks
- suggested engineering improvements

The evaluator's result must be labeled `ANALYSIS`, never `GROUND_TRUTH`.

---

# 29. SECURITY REQUIREMENTS

Even for a demo:

- never log secrets
- hash or redact credential-like strings
- never use real customer records
- never call arbitrary URLs in demos
- mock external services
- sanitize workflow inputs
- validate tool names against registered adapters
- reject dynamic shell commands from workflow YAML
- prevent path traversal in checkpoint paths
- use parameterized SQLite queries
- avoid arbitrary Python execution from YAML

---

# 30. PERFORMANCE TARGETS FOR V1

These are engineering targets for the local demo, not production SLAs.

- plan 100-step workflow: < 1 second excluding LLM calls
- WAL write + checkpoint: < 50 ms typical local run
- compensation plan lookup: < 10 ms for registry paths
- recovery of 10-step mock workflow: < 1 second excluding intentional delays
- audit verification of 10,000 events: < 1 second locally

Do not sacrifice correctness to hit these targets.

---

# 31. BUILD ORDER

Execute in this exact order.

## Phase 1 — Foundation

Build:

- models
- enums
- config
- SQLite storage
- WAL
- checkpoint store
- idempotency store

Immediately write and run unit tests.

## Phase 2 — Mock world

Build:

- MockWorldState
- tool adapters
- deterministic failure injector

Run simple end-to-end state mutation tests.

## Phase 3 — Planner

Build:

- workflow parser
- JSON schema validation
- reversibility classifier
- pivot detector
- saga planner

Use `travel_booking.yaml` as the reference plan.

## Phase 4 — Runtime coordinator

Build:

- execution loop
- retry logic
- postcondition verifier
- approval gate
- recovery manager

Run happy path.

## Phase 5 — Compensation

Build:

- compensation registry
- deterministic compensation execution
- compensation verification
- compensation failure state handling

Run failure at step 3.

## Phase 6 — Crash recovery

Build:

- WAL replay
- status reconciliation
- restart recovery

Run crash-during-payment test repeatedly.

## Phase 7 — Audit

Build:

- hash-chained JSONL
- verification command

Run tamper test.

## Phase 8 — Optional LLM assistant

Build:

- compensation generator
- validator
- explanation generator

Do not let this phase destabilize deterministic runtime behavior.

## Phase 9 — Reporting

Build:

- scoring
- evidence report
- HTML templates
- machine-readable JSON report

## Phase 10 — Demo polish

Build:

- terminal visualization
- demo script
- colored state transitions if terminal supports it
- clear failure/recovery narrative

## Phase 11 — Final quality gate

Run all tests.

Fix all failures.

Run demo from a clean process.

Verify generated report.

---

# 32. REQUIRED ACCEPTANCE TESTS / DEFINITION OF DONE

The build is NOT done unless every item is true:

- [ ] repository installs from scratch
- [ ] `pytest` passes
- [ ] property tests pass
- [ ] travel happy path passes
- [ ] step-2 failure compensation passes
- [ ] step-3 failure compensation passes
- [ ] post-pivot failure passes
- [ ] unknown outcome reconciliation passes
- [ ] crash recovery passes
- [ ] no duplicate payment is produced under crash/retry scenario
- [ ] phantom success is detected
- [ ] compensation failure enters `INCONSISTENT`
- [ ] pivot detection is correct
- [ ] unknown tools fail closed
- [ ] audit hash chain verifies
- [ ] deliberate audit tampering is detected
- [ ] HTML report renders without broken placeholders
- [ ] JSON report validates against schema
- [ ] CLI commands operate correctly
- [ ] malformed YAML produces clear error
- [ ] `demo.sh` succeeds end-to-end
- [ ] no network access is required for demo
- [ ] no credentials are required
- [ ] README has setup + architecture + demo instructions
- [ ] source code has type hints for public APIs
- [ ] errors are structured and user-readable
- [ ] no hidden TODOs in critical paths

---

# 33. README CONTENT REQUIREMENTS

README must contain, in order:

1. one-paragraph problem statement
2. one-paragraph product explanation
3. architecture diagram (ASCII is acceptable)
4. transaction semantics
5. why compensation is not rollback
6. pivot explanation
7. demo steps
8. setup instructions
9. CLI commands
10. test commands
11. limitations
12. security notes
13. relationship to the separate Survivability Engine
14. roadmap

Include a clear warning that the project is an engineering prototype and not a regulator certification mechanism.

---

# 34. ROADMAP PLACEHOLDERS

Do not implement these unless the core is complete; document them instead:

### V2
- PostgreSQL backend
- Redis checkpointing
- pluggable remote adapters
- richer dependency graphs
- approval web UI
- multi-tenant isolation
- signed run attestations
- OpenTelemetry trace ingestion
- LangGraph adapter
- OpenAI Agents SDK adapter
- generic MCP tool adapter

### V3
- automatic compensation contract synthesis from observed tool behavior
- formal recovery-policy checking
- distributed transaction coordination
- runtime policy engine
- integration with the separate Survivability Engine

---

# 35. EXACT DESIGN PHILOSOPHY

The product should feel like:

> **“A circuit breaker + transaction journal + saga coordinator for autonomous agents.”**

The agent remains the decision-maker for business intent.

AATM becomes the **execution safety mechanism** around that intent.

The agent may decide:

> “Book the trip.”

AATM decides:

> “Here is the transaction boundary, here is what has happened, here is what can be compensated, here is the pivot, here is the exact recovery path, and here is whether we actually reached a consistent state.”

This separation is essential.

---

# 36. DO NOT MAKE THESE CLAIMS

Do not write marketing or report language claiming:

- “zero risk”
- “guaranteed safe”
- “regulator certified”
- “legally compliant”
- “impossible to fail”
- “true rollback of irreversible actions”
- “fully autonomous compensation of arbitrary APIs”

Use engineering language:

- observed behavior
- tested failure modes
- recovery coverage
- residual risk
- verified compensation
- unknown outcome
- inconsistent state
- manual escalation

---

# 37. FINAL AUTONOMOUS EXECUTION INSTRUCTION TO OPUS

After reading this document:

1. Inspect the working directory.
2. Determine whether a repository already exists.
3. Preserve useful existing work; do not overwrite working code blindly.
4. Create the repository structure in Section 5.
5. Implement Phase 1 through Phase 11 in order.
6. After every phase, execute the relevant tests.
7. When tests fail, diagnose the root cause, patch the implementation, rerun the tests, and only proceed when the phase is stable.
8. Do not stop at a skeleton.
9. Do not leave mock functions where deterministic functionality is required.
10. Do not reduce the demo to screenshots or fabricated logs; the final demo must execute the real engine against mock adapters.
11. Do not use real-world payment/email/booking services.
12. Generate real audit records from the execution.
13. Generate the final report from those records, never from hard-coded sample text.
14. Verify all required failure scenarios.
15. Run the final test suite from a clean process.
16. Run `demo.sh` and inspect its output.
17. Fix anything that makes the demo confusing, misleading, brittle, or non-reproducible.
18. Ensure every acceptance checkbox in Section 32 is true.
19. Update README with the actual commands used.
20. At the end, print a concise build summary containing:
    - files created/modified
    - test count
    - tests passed
    - demo command
    - report path
    - known limitations

**Do not ask the user to make routine implementation decisions. Make the engineering decisions from this specification, prefer safe deterministic defaults, and continue until the acceptance criteria are satisfied.**

---

# 38. REFERENCE TO THE SUPPLIED AATM SPEC

The supplied source specification established the central architecture used here:

- AATM as a runtime layer around agent tool calls
- saga-style compensation
- three reversibility tiers
- pivot transaction semantics
- idempotency via intent IDs
- write-ahead logging
- checkpointing
- audit trail
- travel-booking demo
- failure injection
- crash recovery
- evidence/reliability reporting

Those concepts are retained here, but the implementation above intentionally makes the authority model stricter: deterministic contracts outrank LLM suggestions, and real external side effects are not used in the overnight demo.

---

# END OF BUILD SPEC
