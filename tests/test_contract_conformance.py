"""Contract-conformance test - proves aatm emits a Contract-v1-valid assurance.json.

Copied from ``orch_bench/templates/test_contract_conformance.py`` and wired to
the aatm engine. If aatm's CLI flags or ``assurance.json`` shape ever drift from
the shared Assurance Contract v1, THIS TEST GOES RED - which is exactly how the
six tools are kept in sync.

Requires the shared package:  pip install -e ../orch_bench/assurance_contract
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from assurance_contract import CONTRACT_VERSION, validate_proof_pack

# --------------------------------------------------------------------------- #
# Engine identity (must match the "engine" field written into assurance.json).
# --------------------------------------------------------------------------- #
ENGINE = "aatm"

# A valid, checked-in workflow to run as the reference target. (examples/ ships
# an adapter demo script, not a workflow; the runnable workflows live here.)
_REPO_ROOT = Path(__file__).resolve().parents[1]
_REFERENCE_WORKFLOW = _REPO_ROOT / "workflows" / "order_fulfillment.yaml"


def _run_engine(out_dir: Path) -> None:
    """Run aatm so it writes ``<out_dir>/assurance.json`` via the Contract v1 CLI.

    Invokes the engine through ``python -m aatm.cli.commands`` (the ``aatm``
    console entry point, ``aatm.cli.commands:main``). Using the current
    interpreter guarantees we exercise THIS checkout's aatm rather than any other
    ``aatm`` that happens to be on PATH.
    """
    assert _REFERENCE_WORKFLOW.exists(), f"missing reference workflow: {_REFERENCE_WORKFLOW}"
    cmd = [
        sys.executable, "-m", "aatm.cli.commands", "run", str(_REFERENCE_WORKFLOW),
        "--seed", "1729",
        "--out", str(out_dir),
        "--fast",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    # Exit codes: 0 ok, 1 engine error, 2 usage error, 3 crash. A clean reference
    # run returns 0; accept 0/1 as "ran" per the shared template.
    assert proc.returncode in (0, 1), (
        f"unexpected exit {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def test_emits_valid_assurance_proof_pack(tmp_path: Path):
    _run_engine(tmp_path)

    pack_path = tmp_path / "assurance.json"
    assert pack_path.exists(), "engine did not write <out>/assurance.json"

    pack = json.loads(pack_path.read_text(encoding="utf-8"))

    # Full structural validation - raises with a precise message on any drift.
    validate_proof_pack(pack, raise_on_error=True)

    # Engine-identity sanity checks.
    assert pack["engine"] == ENGINE
    assert pack["contract_version"].split(".")[0] == CONTRACT_VERSION.split(".")[0]
    # Drift guard: emitted contract_version must EXACTLY equal the shared source
    # of truth so an engine's hardcoded constant cannot silently diverge (even
    # within a major). Any drift fails this test, keeping the stack in sync.
    assert pack["contract_version"] == CONTRACT_VERSION, (
        f"{ENGINE} contract_version {pack['contract_version']!r} != "
        f"assurance_contract.CONTRACT_VERSION {CONTRACT_VERSION!r} (contract drift)"
    )
