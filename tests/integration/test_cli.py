"""Integration tests for CLI commands + audit tamper detection via CLI path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aatm.cli.commands import main as cli_main
from aatm.config import AATMConfig
from aatm.storage.audit_log import verify_audit_chain


@pytest.fixture
def cli_config(tmp_path, monkeypatch):
    """Point the CLI's module-level default_config at a temp project."""
    cfg = AATMConfig(project_root=tmp_path)
    cfg.ensure_dirs()
    real_root = Path(__file__).resolve().parent.parent.parent
    cfg.knowledge_base_dir = real_root / "knowledge_base"
    cfg.schemas_dir = real_root / "schemas"
    monkeypatch.setattr("aatm.cli.commands.default_config", cfg, raising=True)
    return cfg


def _wf(name):
    return str(Path(__file__).resolve().parent.parent.parent / "workflows" / name)


def test_cli_plan_ok(cli_config, capsys):
    rc = cli_main(["plan", _wf("travel_booking.yaml")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Pivot:    step-4" in out


def test_cli_run_and_report_and_verify(cli_config, capsys):
    rc = cli_main(["run", _wf("travel_booking.yaml"), "--fast", "--report",
                   "--no-color"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "FINAL STATE: COMPLETED" in out
    # Extract run id.
    run_id = None
    for line in out.splitlines():
        if line.startswith("RUN_ID:"):
            run_id = line.split(":", 1)[1].strip()
    assert run_id

    # verify-audit
    rc2 = cli_main(["verify-audit", "--run-id", run_id])
    out2 = capsys.readouterr().out
    assert rc2 == 0
    assert "VALID" in out2

    # report summary
    rc3 = cli_main(["report", "--run-id", run_id])
    out3 = capsys.readouterr().out
    assert rc3 == 0
    assert "Assessment:" in out3

    # list-runs
    rc4 = cli_main(["list-runs"])
    out4 = capsys.readouterr().out
    assert rc4 == 0
    assert run_id in out4


def test_cli_malformed_workflow_structured_error(cli_config, tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("workflow:\n  name: no id here\nsteps: []\n", encoding="utf-8")
    rc = cli_main(["plan", str(bad)])
    err = capsys.readouterr().err
    assert rc == 2  # structured parse error, not a traceback
    assert "validation failed" in err or "error:" in err


def test_cli_audit_tamper_detected(cli_config, capsys):
    rc = cli_main(["run", _wf("travel_booking.yaml"), "--fast", "--no-color"])
    out = capsys.readouterr().out
    assert rc == 0
    run_id = next(line.split(":", 1)[1].strip() for line in out.splitlines()
                  if line.startswith("RUN_ID:"))

    audit_path = cli_config.audit_log_path(run_id)
    # Tamper: flip a payload field on a middle line.
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[3])
    entry["payload"]["tampered"] = True
    lines[3] = json.dumps(entry)
    audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    rc2 = cli_main(["verify-audit", "--run-id", run_id])
    out2 = capsys.readouterr().out
    assert rc2 == 1  # detected
    assert "INVALID" in out2
    # Direct check too.
    assert verify_audit_chain(audit_path).valid is False


def test_cli_crash_then_recover(cli_config, capsys):
    inj = str(Path(__file__).resolve().parent.parent.parent / "injections" /
              "crash_mid_payment.yaml")
    rc = cli_main(["run", _wf("travel_booking.yaml"), "--inject", inj, "--fast",
                   "--no-color"])
    out = capsys.readouterr().out
    assert rc == 3  # crash exit code
    run_id = next(line.split(":", 1)[1].strip() for line in out.splitlines()
                  if line.startswith("RUN_ID:"))

    rc2 = cli_main(["recover", "--run-id", run_id])
    out2 = capsys.readouterr().out
    assert rc2 == 0
    assert "committed" in out2  # payment reconciled, no duplicate
