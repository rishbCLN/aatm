# Assurance Contract v1 (FROZEN)

> **Status: FROZEN — do not redesign.** This is the shared interface that lets the
> six tools synchronize. Any agent working in any of the six repos must treat this
> as immutable and conform to it. Breaking changes require a coordinated major bump
> of the `assurance-contract` package **and** every engine + the control plane in
> the same change.
>
> Machine-enforced by the `assurance_contract` package
> (`D:\orch_bench\assurance_contract`): `pip install -e` it and call
> `validate_proof_pack(...)` in your tests. Contract version: **1.0.0**.

---

## 1. The ecosystem

| Pillar | Engine folder | Package | Emits verdict? |
|---|---|---|---|
| capability | `gauntlet` | `gauntlet` | yes |
| survivability | `monkey` | `survivalbench` | yes |
| transaction_safety | `aatm` | `aatm` | yes |
| security | `sixthlayer` | `basilisk` | yes |
| compliance | `complygraph` | (batch export) | yes (via export) |
| — (control plane) | `assura` | `keystone` | aggregates all pillars |

`keystone` runs each engine (in-process import **or** subprocess CLI), reads its
`assurance.json`, normalizes the verdict, and aggregates fail-closed into one
release certificate.

---

## 2. Verdict vocabulary

Canonical, worst-of ordering (higher == worse):

```
PASS  <  PASS_WITH_CONDITIONS  <  BLOCKED
```

- `gauntlet`, `survivalbench`, `basilisk`, `complygraph` emit **only** those three.
- `aatm` may additionally emit `FAIL` and `INCONSISTENT`; the control plane
  collapses both to `BLOCKED`.
- Fail-closed: `None`, `""`, `ERROR`, or any unrecognized status → `BLOCKED`
  (plus a `CRITICAL` hard-blocker finding).

---

## 3. CLI contract (identical across engines)

Every engine MUST expose a non-interactive run:

```
<engine> run <positional target args...> --seed <int> --out <dir> --json
```

- `--seed <int>` — deterministic seed (required).
- `--out <dir>` — output directory; the engine creates/populates it.
- `--json` — also print the machine-readable result to stdout.
- MUST write **`<dir>/assurance.json`** (§4). MAY also write native reports/HTML.
- **Exit codes:** `0` ran & not blocked · `1` ran & BLOCKED (gate) ·
  `2` usage/input error · `3` crash / inconsistent (fail-closed).

The control plane never trusts the exit code alone — it always parses
`assurance.json`. Keep positional target args positional (e.g. gauntlet's `agent`,
survivalbench's `scenario variant`, aatm's `workflow`, basilisk's `target`).

---

## 4. `assurance.json` proof pack

```jsonc
{
  "contract_version": "1.0.0",
  "engine": "gauntlet",              // gauntlet|survivalbench|aatm|basilisk|complygraph
  "engine_version": "0.1.0",
  "release_status": "PASS",          // §2 (aatm may use the superset)
  "headline": { /* engine-specific metrics, §6 */ },
  "findings": [ /* Finding[], §5 */ ],
  "manifest": {
    "content_hash": "sha256:...",    // stable hash of inputs + result
    "seed": 1729,
    "created_at": "2026-01-01T00:00:00Z",
    "target_ref": "cli:reference-strong"
  },
  "evidence_paths": { "native_json": "...", "html": "..." },  // optional
  "disclaimer": "engineering signal, not a legal certification"   // optional
}
```

Required: `contract_version`, `engine`, `engine_version`, `release_status`,
`headline`, `findings`, `manifest{content_hash, seed, created_at, target_ref}`.

---

## 5. Finding schema (shared)

```jsonc
{
  "title": "string",                      // required, non-empty
  "severity": "critical|high|medium|low|info",  // required
  "is_hard_blocker": true,                // required boolean
  "what_happened": "string",              // optional
  "expected": "string",                   // optional
  "actual": "string",                     // optional
  "remediation": "string",                // optional
  "evidence": { "any": "json" },          // optional object
  "source_ref": "engine.module"           // optional
}
```

Emit the canonical keys. The control plane tolerates aliases (`name`, `detail`,
`message`, `fix`, `ref`) but that path is lossy — don't rely on it.

---

## 6. Per-engine `headline` blocks

Fill `headline` with your engine's real metrics. Recommended minimum keys the
control plane already harvests:

- **gauntlet:** `index, ci_low, ci_high, grade, pass_hat_k, p95_latency_ms,
  mean_spend, mean_steps`
- **survivalbench:** `reliability_index, unsafe_recovery_rate, silent_failure_rate,
  false_recovery_rate, detection_rate, unsafe_rate_ci95_upper`
- **aatm:** `score_total, score_status, floor_violations, audit_chain_valid,
  consistent, dead_letters`
- **basilisk:** `security_index, ci_low, ci_high, breach_rate, critical_breaches,
  coverage, ineffective_defenses`
- **complygraph:** `control_coverage, open_findings_by_severity, framework,
  assessed_at`

---

## 7. Discovery (for in-process mode)

Be pip-installable into the control plane's environment and expose a version:

| Pillar | Package | Version attr |
|---|---|---|
| capability | `gauntlet` | `__version__` |
| survivability | `survivalbench` | `__version__` |
| transaction_safety | `aatm` | `BUILD_VERSION` |
| security | `basilisk` | `__version__` |
| compliance | `complygraph_assurance` | `__version__` |

---

## 8. How to conform (checklist per engine)

- [ ] `pip install -e ../orch_bench/assurance_contract`.
- [ ] Add `--out <dir>` to `run`/`gate`; always write `<dir>/assurance.json`.
- [ ] Emit the exact shape in §4 with your `headline` from §6.
- [ ] Map your findings to §5; map your native verdict to §2.
- [ ] Honor exit codes `0/1/2/3`.
- [ ] Copy `../orch_bench/templates/test_contract_conformance.py` into `tests/`,
      wire it to your engine, and keep it green in CI.

Full per-tool tasks live in each folder's `PRODUCTION_READINESS.md`.
