"""Tests for the HTTP tool adapter's status classification and query paths."""

from __future__ import annotations

import json
import urllib.error
from io import BytesIO
from uuid import uuid4

from aatm.adapters.http_adapter import HTTPToolAdapter, _classify_status
from aatm.enums import FailureClass, Outcome
from aatm.models import ActionIntent


def _intent(tool="book", params=None):
    return ActionIntent(tool_name=tool, parameters=params or {}, run_id=uuid4(),
                        workflow_id="wf", step_id="s1")


def test_status_classification_mapping():
    assert _classify_status(408) is FailureClass.TIMEOUT
    assert _classify_status(429) is FailureClass.RATE_LIMIT
    assert _classify_status(503) is FailureClass.SERVER_ERROR
    assert _classify_status(403) is FailureClass.AUTHORIZATION
    assert _classify_status(422) is FailureClass.VALIDATION
    assert _classify_status(404) is FailureClass.RESOURCE_UNAVAILABLE
    assert _classify_status(418) is FailureClass.BUSINESS_RULE


def _http_error(code, headers=None):
    return urllib.error.HTTPError(
        url="http://svc.local/book", code=code, msg="err",
        hdrs=headers or {}, fp=BytesIO(b"{}"))


async def test_http_error_maps_to_failure_class(monkeypatch):
    def _raise(req, timeout=None):
        raise _http_error(429, {"Retry-After": "5"})

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    adapter = HTTPToolAdapter("http://svc.local", ("book",))
    result = await adapter.execute(_intent())
    assert result.outcome is Outcome.FAILURE
    assert result.failure_class is FailureClass.RATE_LIMIT
    assert result.data["retry_after"] == "5"


async def test_execute_success_wraps_non_dict(monkeypatch):
    class _Resp:
        status = 200
        headers = {}
        def read(self): return json.dumps([1, 2, 3]).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _Resp())
    adapter = HTTPToolAdapter("http://svc.local", ("book",))
    result = await adapter.execute(_intent())
    assert result.outcome is Outcome.SUCCESS
    assert result.data["result"] == [1, 2, 3]


async def test_query_status_found(monkeypatch):
    class _Resp:
        status = 200
        headers = {}
        def read(self): return json.dumps({"status": "confirmed"}).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _Resp())
    adapter = HTTPToolAdapter("http://svc.local", ("book",))
    q = await adapter.query_status(uuid4())
    assert q.found is True and q.outcome is Outcome.SUCCESS


async def test_query_status_404_is_not_found(monkeypatch):
    def _raise(req, timeout=None):
        raise _http_error(404)

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    adapter = HTTPToolAdapter("http://svc.local", ("book",))
    q = await adapter.query_status(uuid4())
    assert q.found is False and q.outcome is Outcome.FAILURE


async def test_query_status_transport_error_is_unknown(monkeypatch):
    def _boom(req, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    adapter = HTTPToolAdapter("http://svc.local", ("book",))
    q = await adapter.query_status(uuid4())
    assert q.found is False and q.outcome is Outcome.UNKNOWN


async def test_verify_postcondition(monkeypatch):
    adapter = HTTPToolAdapter("http://svc.local", ("book",))
    ok = await adapter.verify_postcondition(_intent(), {"result": {"id": 1}})
    assert ok.passed is True
    bad = await adapter.verify_postcondition(_intent(), {"result": {}})
    assert bad.passed is False
