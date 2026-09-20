"""Central configuration for the AATM engine.

All paths are local-first. No cloud credentials, no network access. Paths can be
overridden via environment variables (prefixed ``AATM_``) or by constructing an
``AATMConfig`` explicitly and passing it to the engine components.
"""

from __future__ import annotations

import os
from pathlib import Path

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
    return AATMConfig(**kwargs)  # type: ignore[arg-type]


# A shared default config instance. Components accept an explicit config too.
default_config = _env_config()
