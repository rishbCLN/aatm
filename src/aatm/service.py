"""AATM service mode - a small local HTTP API over the engine.

Exposes plan/run/recover/approve so multiple agents can share one coordinator
instead of each shelling out to the CLI. Built on the stdlib ``http.server`` (no
web framework dependency).

Security posture (deliberate and explicit):

- Binds to ``127.0.0.1`` by default. Do NOT expose this to a network without
  putting real authentication + TLS in front of it.
- Supports a shared bearer token via ``AATM_API_TOKEN``. If unset, the server
  runs UNAUTHENTICATED and logs a clear warning - acceptable only for local dev.

This is a reference/dev server, not a hardened production gateway.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from .config import AATMConfig, default_config
from .engine import AATMEngine, EngineError
from .observability import enable_console_logging, log_event
from .storage.approvals import ApprovalStore


def _token() -> Optional[str]:
    return os.environ.get("AATM_API_TOKEN")


class _Handler(BaseHTTPRequestHandler):
    # Injected by make_server.
    config: AATMConfig = default_config
    logger: logging.Logger = logging.getLogger("aatm.service")

    # -- helpers --------------------------------------------------------------

    def _send(self, status: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self) -> bool:
        token = _token()
        if not token:
            return True  # unauthenticated dev mode (warned at startup)
        provided = self.headers.get("Authorization", "")
        return provided == f"Bearer {token}"

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw.strip() else {}

    def log_message(self, fmt: str, *args: Any) -> None:  # silence default logging
        return

    # -- routing --------------------------------------------------------------

    def do_GET(self) -> None:
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        if self.path == "/health":
            return self._send(200, {"status": "ok"})
        if self.path.startswith("/runs/") and self.path.endswith("/report"):
            run_id = self.path.split("/")[2]
            json_path = self.config.report_json_path(run_id)
            if not json_path.exists():
                return self._send(404, {"error": "no report for run"})
            return self._send(200, json.loads(json_path.read_text(encoding="utf-8")))
        return self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        try:
            body = self._read_json()
        except json.JSONDecodeError:
            return self._send(400, {"error": "invalid JSON body"})

        try:
            if self.path == "/plan":
                return self._handle_plan(body)
            if self.path == "/run":
                return self._handle_run(body)
            if self.path == "/recover":
                return self._handle_recover(body)
            if self.path == "/approve":
                return self._handle_approve(body)
        except EngineError as exc:
            return self._send(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            return self._send(500, {"error": f"internal error: {exc}"})
        return self._send(404, {"error": "not found"})

    # -- handlers -------------------------------------------------------------

    def _handle_plan(self, body: dict[str, Any]) -> None:
        wf = body.get("workflow")
        if not wf:
            return self._send(400, {"error": "missing 'workflow'"})
        engine = AATMEngine(self.config)
        plan = engine.plan(wf)
        self._send(200, {
            "workflow_id": plan.workflow_id,
            "pivot_step_id": plan.pivot_step_id,
            "valid": plan.is_valid,
            "critical_issues": plan.critical_issues,
            "steps": [{"step_id": s.step_id, "tool": s.tool_name,
                       "tier": int(s.tier)} for s in plan.steps],
        })

    def _handle_run(self, body: dict[str, Any]) -> None:
        wf = body.get("workflow")
        if not wf:
            return self._send(400, {"error": "missing 'workflow'"})
        engine = AATMEngine(self.config)
        output = asyncio.run(engine.run(
            wf, injection_path=body.get("inject"),
            backoff_scale=0.0 if body.get("fast") else 0.001,
        ))
        report = engine.report(output) if body.get("report") else None
        resp = {
            "run_id": str(output.run.run_id),
            "state": str(output.run.state),
            "pivot_crossed": output.run.pivot_crossed,
            "crashed": output.crashed,
            "metrics": output.metrics,
        }
        if report is not None:
            resp["score"] = report["score"]
            resp["report_paths"] = report["_paths"]
        log_event(self.logger, "service.run", run_id=resp["run_id"],
                  state=resp["state"])
        self._send(200, resp)

    def _handle_recover(self, body: dict[str, Any]) -> None:
        run_id = body.get("run_id")
        if not run_id:
            return self._send(400, {"error": "missing 'run_id'"})
        engine = AATMEngine(self.config)
        report, _reg = asyncio.run(engine.recover(run_id))
        self._send(200, report.to_dict())

    def _handle_approve(self, body: dict[str, Any]) -> None:
        run_id = body.get("run_id")
        step = body.get("step")
        if not run_id or not step:
            return self._send(400, {"error": "missing 'run_id' or 'step'"})
        store = ApprovalStore(self.config.approval_path(run_id))
        rec = store.decide(step, bool(body.get("granted", False)),
                           decided_by=body.get("by", "api"),
                           reason=body.get("reason", ""))
        self._send(200, {"recorded": rec})


def make_server(host: str = "127.0.0.1", port: int = 8080,
                config: Optional[AATMConfig] = None) -> ThreadingHTTPServer:
    cfg = config or default_config
    logger = enable_console_logging(name="aatm.service")

    handler = type("_BoundHandler", (_Handler,), {"config": cfg, "logger": logger})
    httpd = ThreadingHTTPServer((host, port), handler)
    if not _token():
        log_event(logger, "service.WARNING.unauthenticated", level=logging.WARNING,
                  detail="AATM_API_TOKEN not set; API is UNAUTHENTICATED. "
                  "Bind to localhost only and set a token before exposing it.")
    log_event(logger, "service.listening", host=host, port=port,
              authenticated=bool(_token()))
    return httpd


def serve(host: str = "127.0.0.1", port: int = 8080,
          config: Optional[AATMConfig] = None) -> None:  # pragma: no cover
    httpd = make_server(host, port, config)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
