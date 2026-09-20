"""Central configuration for the AATM engine.

All paths are local-first. No cloud credentials, no network access. Paths can be
overridden via environment variables (prefixed ``AATM_``) or by constructing an
``AATMConfig`` explicitly and passing it to the engine components.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


def _project_root() -> Path:
    """Best-effort project root.

    Resolves to the directory that contains ``output/`` and ``knowledge_base/``
    when running from a checkout. Falls back to the current working directory.
    """
    here = Path(__file__).resolve()
    # src/aatm/config.py -> src/aatm -> src -> project root
    candidate = here.parent.parent.parent
    if (candidate / "knowledge_base").exists() or (candidate / "output").exists():
        return candidate
    return Path.cwd()


PROJECT_ROOT = _project_root()


class AATMConfig(BaseModel):
    """Runtime configuration.

    The default database backend for the demo is SQLite (WAL, checkpoints,
    idempotency). The audit trail is append-only JSONL with SHA-256 chaining.
    """

    project_root: Path = Field(default_factory=_project_root)

    # Output directories
    output_dir: Path | None = None
    audit_dir: Path | None = None
    checkpoints_dir: Path | None = None
    reports_dir: Path | None = None
    runs_dir: Path | None = None

    # Knowledge base
    knowledge_base_dir: Path | None = None
    schemas_dir: Path | None = None

    # Behavior tuning
    default_timeout_ms: int = 5000
    max_compensation_retries: int = 3
    require_approval_for_unknown: bool = True

    # Circuit breaker (per-tool). Trips OPEN after N consecutive failures; fails
    # fast during the cooldown window; allows a trial call when HALF_OPEN.
    circuit_failure_threshold: int = 5
    circuit_cooldown_s: float = 30.0
    circuit_half_open_trials: int = 1

    # Human-in-the-loop approval: how long a Tier-3 approval request stays valid
    # before the fail-safe (DENY) applies. Decisions may arrive out-of-band.
    approval_timeout_s: float = 3600.0

    # Audit hardening. When set, the audit log is HMAC-signed (tamper-proof, not
    # just tamper-evident). Sourced from AATM_AUDIT_HMAC_KEY by default.
    audit_hmac_key: Optional[str] = None

    # PII/secret redaction for audit payloads and reports (on by default so
    # sensitive values never touch disk).
    redact_pii: bool = True

    # Feature flags
    enable_llm: bool = False  # Deterministic by default; LLM is opt-in/advisory.

    model_config = {"arbitrary_types_allowed": True}

    def model_post_init(self, __context: object) -> None:  # noqa: D401
        root = self.project_root
        self.output_dir = self.output_dir or (root / "output")
        self.audit_dir = self.audit_dir or (self.output_dir / "audit")
        self.checkpoints_dir = self.checkpoints_dir or (self.output_dir / "checkpoints")
        self.reports_dir = self.reports_dir or (self.output_dir / "reports")
        self.runs_dir = self.runs_dir or (self.output_dir / "runs")
        self.knowledge_base_dir = self.knowledge_base_dir or (root / "knowledge_base")
        self.schemas_dir = self.schemas_dir or (root / "schemas")

    def ensure_dirs(self) -> None:
        """Create all output directories if they do not exist."""
        for d in (
            self.output_dir,
            self.audit_dir,
            self.checkpoints_dir,
            self.reports_dir,
            self.runs_dir,
        ):
            if d is not None:
                d.mkdir(parents=True, exist_ok=True)

    # --- Per-run path helpers -------------------------------------------------

    def wal_db_path(self, run_id: str) -> Path:
        assert self.runs_dir is not None
        return self.runs_dir / f"{run_id}.wal.db"

    def world_state_path(self, run_id: str) -> Path:
        """Durable store modeling the EXTERNAL systems' own persistence.

        Lets a fresh process (crash recovery) query authoritative external state.
        """
        assert self.runs_dir is not None
        return self.runs_dir / f"{run_id}.world.json"

    def checkpoint_db_path(self, run_id: str) -> Path:
        assert self.checkpoints_dir is not None
        return self.checkpoints_dir / f"{run_id}.checkpoints.db"

    def idempotency_db_path(self, run_id: str) -> Path:
        assert self.runs_dir is not None
        return self.runs_dir / f"{run_id}.idempotency.db"

    def audit_log_path(self, run_id: str) -> Path:
        assert self.audit_dir is not None
        return self.audit_dir / f"{run_id}.audit.jsonl"

    def approval_path(self, run_id: str) -> Path:
        """Durable human-in-the-loop approval request/decision log."""
        assert self.runs_dir is not None
        return self.runs_dir / f"{run_id}.approvals.jsonl"

    def audit_anchor_path(self, run_id: str) -> Path:
        """External anchor file recording audit-chain heads over time."""
        assert self.audit_dir is not None
        return self.audit_dir / f"{run_id}.anchor.jsonl"

    def dead_letter_path(self, run_id: str) -> Path:
        """Durable queue of compensations needing manual intervention."""
        assert self.runs_dir is not None
        return self.runs_dir / f"{run_id}.deadletter.jsonl"

    def run_state_path(self, run_id: str) -> Path:
        assert self.runs_dir is not None
        return self.runs_dir / f"{run_id}.run.json"

    def report_json_path(self, run_id: str) -> Path:
        assert self.reports_dir is not None
        return self.reports_dir / f"{run_id}.report.json"

    def report_html_path(self, run_id: str) -> Path:
        assert self.reports_dir is not None
        return self.reports_dir / f"{run_id}.report.html"


def _env_config() -> AATMConfig:
    """Build a config, applying ``AATM_*`` environment overrides where present."""
    kwargs: dict[str, object] = {}
    root_override = os.environ.get("AATM_PROJECT_ROOT")
    if root_override:
        kwargs["project_root"] = Path(root_override)
    if os.environ.get("AATM_ENABLE_LLM", "").lower() in {"1", "true", "yes"}:
        kwargs["enable_llm"] = True
    hmac_key = os.environ.get("AATM_AUDIT_HMAC_KEY")
    if hmac_key:
        kwargs["audit_hmac_key"] = hmac_key
    return AATMConfig(**kwargs)  # type: ignore[arg-type]


# A shared default config instance. Components accept an explicit config too.
default_config = _env_config()
