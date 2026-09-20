"""Shared pytest fixtures for the AATM test suite."""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

import pytest

# Ensure ``src`` is importable when running from a checkout without install.
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aatm.config import AATMConfig  # noqa: E402


@pytest.fixture
def tmp_config(tmp_path: Path) -> AATMConfig:
    """A config rooted at a temp directory with all output dirs created."""
    cfg = AATMConfig(project_root=tmp_path)
    cfg.ensure_dirs()
    # knowledge_base/schemas point at the real project so classifier/registry work.
    real_root = Path(__file__).resolve().parent.parent
    cfg.knowledge_base_dir = real_root / "knowledge_base"
    cfg.schemas_dir = real_root / "schemas"
    return cfg


@pytest.fixture
def run_id() -> str:
    return str(uuid4())
