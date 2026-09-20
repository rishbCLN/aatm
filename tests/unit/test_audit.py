"""Unit tests for the tamper-evident audit log."""

from __future__ import annotations

import json
from uuid import uuid4

from aatm.enums import AuditEvent
from aatm.storage.audit_log import AuditLog, verify_audit_chain


def _populate(path, run_id, n=5):
    log = AuditLog(path)
    log.append(AuditEvent.WORKFLOW_START, run_id=run_id, entity_id=run_id,
               payload={"workflow": "wf-1"})
    for i in range(n):
        log.append(
            AuditEvent.ACTION_COMPLETE,
            run_id=run_id,
            entity_id=f"step-{i}",
            payload={"i": i, "tool": "book_flight"},
        )
    log.append(AuditEvent.WORKFLOW_COMPLETE, run_id=run_id, entity_id=run_id)
    return log


def test_valid_chain_verifies(tmp_path):
    run_id = str(uuid4())
    path = tmp_path / "audit.jsonl"
    log = _populate(path, run_id)
    result = log.verify()
    assert result.valid is True
    assert result.entry_count == 7  # 1 start + 5 actions + 1 complete


def test_chain_resumes_after_reopen(tmp_path):
    run_id = str(uuid4())
    path = tmp_path / "audit.jsonl"
    _populate(path, run_id, n=2)

    # Reopen and append more; chain must remain valid and seq continues.
    log2 = AuditLog(path)
    entry = log2.append(AuditEvent.ESCALATION, run_id=run_id, entity_id="x")
    assert entry["seq"] == 5  # 1 + 2 + 1(complete) + 1 new
    assert verify_audit_chain(path).valid is True


def test_tampered_payload_detected(tmp_path):
    run_id = str(uuid4())
    path = tmp_path / "audit.jsonl"
    _populate(path, run_id, n=3)

    # Tamper: edit a payload value on line 3 but keep everything else.
    lines = path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[2])
    entry["payload"]["tool"] = "charge_payment"  # malicious swap
    lines[2] = json.dumps(entry)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = verify_audit_chain(path)
    assert result.valid is False
    assert "tampering" in result.detail.lower()


def test_tampered_header_detected(tmp_path):
    run_id = str(uuid4())
    path = tmp_path / "audit.jsonl"
    _populate(path, run_id, n=3)

    lines = path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[1])
    entry["event"] = "WORKFLOW_ABORTED"  # tamper event but leave hash
    lines[1] = json.dumps(entry)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = verify_audit_chain(path)
    assert result.valid is False


def test_deleted_line_detected(tmp_path):
    run_id = str(uuid4())
    path = tmp_path / "audit.jsonl"
    _populate(path, run_id, n=4)

    lines = path.read_text(encoding="utf-8").splitlines()
    del lines[2]  # remove a middle entry -> sequence gap
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = verify_audit_chain(path)
    assert result.valid is False
    assert result.broken_seq is not None


def test_events_by_type(tmp_path):
    run_id = str(uuid4())
    path = tmp_path / "audit.jsonl"
    log = _populate(path, run_id, n=3)
    actions = log.events_by_type(AuditEvent.ACTION_COMPLETE)
    assert len(actions) == 3
