"""Tests for the local HTTP service API (service mode)."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from aatm.service import make_server


@pytest.fixture
def server(tmp_config):
    httpd = make_server("127.0.0.1", 0, config=tmp_config)  # port 0 = ephemeral
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.15)
    base = f"http://127.0.0.1:{port}"
    yield base
    httpd.shutdown()


def _get(base, path, timeout=10):
    with urllib.request.urlopen(base + path, timeout=timeout) as r:
        return r.status, json.loads(r.read())


def _post(base, path, body, timeout=30):
    data = json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read())


def test_health(server):
    status, body = _get(server, "/health")
    assert status == 200 and body["status"] == "ok"


def test_plan_endpoint(server):
    status, body = _post(server, "/plan",
                         {"workflow": "workflows/travel_booking.yaml"})
    assert status == 200
    assert body["valid"] is True
    assert body["pivot_step_id"] == "step-4"
    assert len(body["steps"]) == 7


def test_run_endpoint_and_report_fetch(server):
    status, body = _post(server, "/run",
                        {"workflow": "workflows/travel_booking.yaml",
                         "fast": True, "report": True})
    assert status == 200
    assert body["state"].lower().endswith("completed")
    assert body["crashed"] is False
    run_id = body["run_id"]
    # The report should now be fetchable.
    status2, report = _get(server, f"/runs/{run_id}/report")
    assert status2 == 200
    assert report["result"]["run_id"] == run_id


def test_run_with_injection_reports_break(server):
    status, body = _post(server, "/run",
                        {"workflow": "workflows/travel_booking.yaml",
                         "inject": "injections/step3_failure.yaml",
                         "fast": True})
    assert status == 200
    assert body["state"].lower().endswith("aborted")
    assert body["pivot_crossed"] is False


def test_approve_endpoint_records_decision(server, tmp_config):
    from aatm.storage.approvals import ApprovalStore

    status, body = _post(server, "/approve",
                        {"run_id": "svc-run", "step": "step-4",
                         "granted": True, "by": "svc-tester"})
    assert status == 200
    store = ApprovalStore(tmp_config.approval_path("svc-run"))
    latest = store.latest_decision("step-4")
    assert latest["granted"] is True and latest["decided_by"] == "svc-tester"


def test_bad_request_missing_workflow(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(server, "/plan", {})
    assert exc.value.code == 400


def test_unknown_route_404(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server, "/nope")
    assert exc.value.code == 404


def test_auth_required_when_token_set(tmp_config, monkeypatch):
    monkeypatch.setenv("AATM_API_TOKEN", "s3cr3t")
    httpd = make_server("127.0.0.1", 0, config=tmp_config)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.15)
    base = f"http://127.0.0.1:{port}"
    try:
        # No auth header -> 401.
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(base, "/health")
        assert exc.value.code == 401
        # With the right token -> 200.
        req = urllib.request.Request(base + "/health")
        req.add_header("Authorization", "Bearer s3cr3t")
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 200
    finally:
        httpd.shutdown()
