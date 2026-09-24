# AATM — Production Readiness Runbook

**Package:** `aatm` (Agent Action Transaction Manager)
**Role in the ecosystem:** Transaction-safety engine (the `transaction_safety`
pillar). A runtime safety/recovery layer that wraps agent tool calls: durable
write-ahead intent log, reversibility classification + pivot detection,
compensating actions in reverse order, crash survival, idempotency, a
tamper-evident audit chain, and an evidence/reliability report.

> Maturity today: **High.** ~143 tests across unit/integration/property/e2e,
> coverage + hypothesis + mypy + ruff all exercised, real HTTP adapter, structured
> observability, `aatm` CLI. Gaps are **operational**: no CI/Docker, no `py.typed`,
> no checked-in lint/type config, an experimental Postgres backend, and a CLI that
> doesn't match the control plane's contract.

See `D:\assura\PRODUCTION_READINESS.md` §2 for the canonical **Assurance Contract v1**.

---

## 0. Definition of Done

- [ ] `aatm run <workflow> --seed N --out DIR --json [--inject FILE]` writes a schema-valid `DIR/assurance.json`.
- [ ] CI runs ruff + mypy + pytest (+coverage) with checked-in config.
- [ ] Docker image builds and runs a demo workflow.
- [ ] `py.typed` shipped so consumers see the package as typed.
- [ ] The Postgres backend is either promoted (tested) or clearly quarantined behind a flag + docs.
- [ ] `assurance_contract` validation test passes on the emitted proof pack.

---

## 1. Contract conformance (synchronization) — HIGH priority

The control plane's transaction-safety runner calls flags that don't exist.
Reconcile to **Assurance Contract v1**.

**Current state**
- `workflow` is positional (`aatm/src/aatm/cli/commands.py:401`) — good, keep it.
- The injection flag is **`--inject`** (`commands.py:402`), but the control plane
  passes `--injection`.
- There is **no `--report-dir`/`--out`**; the report is written only with
  `--report`, to a path chosen by the engine/config (`commands.py:80-127`).
- The report dict (`reporting/evidence_report.py:142-155`) already has the right
  shape: `result`, `score{total,status,floor_violations}`, `audit_chain{valid,...}`,
  `dead_letters`, `meta{build_version,disclaimer}`, and `_paths{json,html}`.
- Exit code `3` on simulated crash already matches the contract (`commands.py:125`).

**Tasks**
1. Add `--out <dir>` (accept `--report-dir` as an alias for back-compat) to `run`
   and `resume`. When set, always write reports into that dir even without
   `--report`.
2. Write `<out>/assurance.json` in the Contract v1 shape. Map the existing report:
   ```jsonc
   {
     "contract_version": "1.0.0",
     "engine": "aatm",
     "engine_version": "<aatm.BUILD_VERSION>",
     "release_status": "<score.status>",   // PASS|PASS_WITH_CONDITIONS|FAIL|BLOCKED|INCONSISTENT
     "headline": {
       "score_total": score.total, "score_status": score.status,
       "floor_violations": score.floor_violations,
       "audit_chain_valid": audit_chain.valid,
       "consistent": result.consistent, "dead_letters": dead_letters
     },
     "findings": [ /* derive from floor_violations / dead-letters / audit breaks */ ],
     "manifest": { "content_hash": "...", "seed": ..., "created_at": "...",
                   "target_ref": "<workflow>" },
     "evidence_paths": { "native_json": "...report.json", "html": "...", "flow": "..." }
   }
   ```
   (Keystone maps `FAIL`/`INCONSISTENT` → `BLOCKED`; still emit the native status.)
3. Standardize the injection flag: keep `--inject`, add `--injection` as an alias,
   and use `--inject` in all docs. (The control plane is being updated to `--inject`.)
4. Ensure `result` in the report exposes a `consistent` boolean (the control plane
   reads `report["result"]["consistent"]`); add it if the model doesn't already
   serialize it.
5. Add a `--seed` option threaded into the engine's deterministic paths so repeated
   runs are reproducible for the control plane.
6. Add a unit test validating `assurance.json` against `assurance_contract`.

---

## 2. CI/CD & config

1. Add `.github/workflows/aatm.yml`: `ruff check` → `mypy` → `pytest` with coverage
   on Python 3.11/3.12, plus a gate step that runs a bundled workflow and asserts a
   valid audit chain (e.g., `aatm run examples/... --out out && aatm verify-audit
   --run-id ...`).
2. Check in the lint/type configuration. The `.ruff_cache`/`.mypy_cache` prove both
   run locally, but there's no committed `ruff`/`mypy` config — add it to
   `pyproject.toml` (or `ruff.toml`/`mypy.ini`) so CI and contributors are consistent.

---

## 3. Docker

Add a `Dockerfile` (non-root, pinned base) and a `docker-compose.yml`. For the
default SQLite backend a single service suffices; if the Postgres backend is
promoted (§5), add a `postgres` service and wire `AATM_DB_URL`.

---

## 4. Packaging

1. Ship a `py.typed` marker — the package is extensively typed but consumers won't
   see it as typed without the marker.
2. Confirm the `aatm` console entry point (`aatm.cli.commands:main`) and
   `BUILD_VERSION` are exported (control-plane discovery reads `BUILD_VERSION`).
3. Pin dependencies for reproducible installs.

---

## 5. Postgres backend

`storage/backend.py` offers SQLite (default) + an **experimental** Postgres backend
via `AATM_DB_URL`. Either:
- Promote it: add integration tests against a real Postgres (in CI via a service
  container), document connection/migration, and remove the "experimental" caveat; or
- Quarantine it: gate behind an explicit opt-in and clearly document it as
  unsupported, so nobody ships it to production unaware.

---

## 6. Security & secrets

1. The real HTTP adapter (`adapters/http_adapter.py`) already preserves the safety
   contract (idempotency-key propagation, `UNKNOWN` on transport error, status→
   failure-class mapping). Keep this; add per-adapter timeouts if any are missing.
2. Redaction (`redaction.py`) masks PII/secrets while preserving UUIDs/hashes for
   the audit chain — verify it runs before **every** report/log write path.
3. Any credentials for real adapters must come from env, never persisted in the WAL,
   audit log, or evidence report.

---

## 7. Suggested execution order

1. §1 contract conformance (`--out` + `assurance.json`, `--inject` alias, `--seed`).
2. §2 CI + checked-in lint/type config.
3. §4 `py.typed` + pinning; §3 Docker.
4. §5 resolve the Postgres backend status.
5. Add the `assurance_contract` validation test and tag a release.
