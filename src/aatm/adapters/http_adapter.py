"""HTTP tool adapter - the bridge from mock adapters to real external services.

This implements the same :class:`~aatm.adapters.base.ToolAdapter` contract the
coordinator relies on, but talks to a real HTTP endpoint. It is dependency-free
(stdlib ``urllib``) and is NOT used by the local demo, which stays fully mock.

Key safety properties it preserves:

- **Idempotency-key propagation.** The stable ``intent_id`` is sent as the
  ``Idempotency-Key`` header so a retried request cannot double-execute on a
  server that honors the header.
- **Honest uncertainty.** A network timeout / connection drop becomes an
  ``UNKNOWN`` outcome (the coordinator then reconciles via :meth:`query_status`)
  rather than a false failure that could trigger a duplicate.
- **Failure classification.** HTTP status codes map to retryable vs
  non-retryable :class:`FailureClass` values; ``Retry-After`` is surfaced.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any, Optional
from uuid import UUID

from ..enums import FailureClass, Outcome
from ..models import ActionIntent, OutcomeQuery, ToolResult, VerificationResult


_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


def _classify_status(status: int) -> FailureClass:
    if status in (408, 504):
        return FailureClass.TIMEOUT
    if status == 429:
        return FailureClass.RATE_LIMIT
    if status in (500, 502, 503):
        return FailureClass.SERVER_ERROR
    if status in (401, 403):
        return FailureClass.AUTHORIZATION
    if status in (400, 422):
        return FailureClass.VALIDATION
    if status == 404:
        return FailureClass.RESOURCE_UNAVAILABLE
    return FailureClass.BUSINESS_RULE


class HTTPToolAdapter:
    """Calls a real HTTP service while preserving AATM's safety contract.

    ``endpoints`` maps tool name -> path template (e.g. ``"/bookings"``). The
    ``status_path`` template receives the intent id for reconciliation queries.
    """

    def __init__(
        self,
        base_url: str,
        tools: tuple[str, ...],
        *,
        endpoints: Optional[dict[str, str]] = None,
        status_path: str = "/intents/{intent_id}",
        headers: Optional[dict[str, str]] = None,
        timeout_s: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.tools = tools
        self.endpoints = endpoints or {t: f"/{t}" for t in tools}
        self.status_path = status_path
        self.headers = headers or {}
        self.timeout_s = timeout_s

    # -- contract -------------------------------------------------------------

    async def execute(self, intent: ActionIntent) -> ToolResult:
        # Run the blocking HTTP call in a thread so we stay async-friendly.
        return await asyncio.to_thread(self._execute_sync, intent)

    async def query_status(self, intent_id: UUID) -> OutcomeQuery:
        return await asyncio.to_thread(self._query_sync, intent_id)

    async def verify_postcondition(
        self, intent: ActionIntent, state: dict[str, Any]
    ) -> VerificationResult:
        # Default: trust a 2xx result. Real deployments override with a state
        # query against the service.
        result = state.get("result", {}) or {}
        ok = bool(result) and not result.get("__error__")
        return VerificationResult(passed=ok, expression="http 2xx result",
                                  detail="ok" if ok else "no successful result")

    # -- blocking helpers -----------------------------------------------------

    def _request(self, method: str, url: str, body: Optional[dict[str, Any]],
                 intent_id: str) -> tuple[int, dict[str, Any], dict[str, str]]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url=url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        # The critical safety header: dedupe on the stable intent id.
        req.add_header("Idempotency-Key", intent_id)
        for k, v in self.headers.items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            raw = resp.read().decode("utf-8") or "{}"
            payload = json.loads(raw) if raw.strip() else {}
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, payload, hdrs

    def _execute_sync(self, intent: ActionIntent) -> ToolResult:
        url = f"{self.base_url}{self.endpoints.get(intent.tool_name, '/' + intent.tool_name)}"
        iid = str(intent.intent_id)
        try:
            status, payload, _ = self._request("POST", url, intent.parameters, iid)
            return ToolResult(intent_id=intent.intent_id, outcome=Outcome.SUCCESS,
                              data=payload if isinstance(payload, dict) else
                              {"result": payload})
        except urllib.error.HTTPError as exc:
            fc = _classify_status(exc.code)
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            return ToolResult(
                intent_id=intent.intent_id,
                outcome=Outcome.FAILURE,
                error_message=f"HTTP {exc.code}: {exc.reason}",
                failure_class=fc,
                data={"status": exc.code, "retry_after": retry_after},
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # Connection/timeout: we CANNOT know if the server acted -> UNKNOWN.
            return ToolResult(
                intent_id=intent.intent_id,
                outcome=Outcome.UNKNOWN,
                error_message=f"transport error: {exc}",
                failure_class=FailureClass.TIMEOUT,
            )

    def _query_sync(self, intent_id: UUID) -> OutcomeQuery:
        url = f"{self.base_url}{self.status_path.format(intent_id=intent_id)}"
        try:
            status, payload, _ = self._request("GET", url, None, str(intent_id))
            found = bool(payload) and payload.get("status") not in (None, "not_found")
            outcome = Outcome.SUCCESS if found else Outcome.FAILURE
            return OutcomeQuery(intent_id=intent_id, found=found, outcome=outcome,
                                data=payload if isinstance(payload, dict) else {},
                                detail=payload.get("status", "") if isinstance(
                                    payload, dict) else "")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return OutcomeQuery(intent_id=intent_id, found=False,
                                    outcome=Outcome.FAILURE,
                                    detail="not found (404)")
            return OutcomeQuery(intent_id=intent_id, found=False,
                                outcome=Outcome.UNKNOWN,
                                detail=f"status query HTTP {exc.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return OutcomeQuery(intent_id=intent_id, found=False,
                                outcome=Outcome.UNKNOWN,
                                detail=f"status query transport error: {exc}")
