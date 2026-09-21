"""Interactive flow visualization + automatic breaking analysis.

Turns a run's evidence report into a self-contained HTML page that shows the
tool-call pipeline as a diagram and pinpoints *where and why* the workflow broke.

Two things the tabular report doesn't give you at a glance:

1. **Flow diagram** - every step as a node in execution order, colored by outcome
   (success / failure / unknown-reconciled / compensated / not-reached), with the
   irreversible **pivot** marked, dependency edges drawn, and compensation shown
   as reverse edges. Click a node for its full detail.

2. **Breaking analysis** - the generator locates the break point (first failed /
   crashed / unresolved step), says whether it happened before or after the pivot,
   classifies the root cause, describes what recovery did, and suggests a fix.

The output is a single HTML file with inline CSS + vanilla JS (no external
dependencies, works offline) so it can be opened or shared directly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..config import AATMConfig, default_config

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

# Terminal states that mean the run did not finish cleanly.
_UNCLEAN_STATES = {"aborted", "inconsistent", "timed_out", "running", "compensating",
                   "waiting_approval"}


def _outcome_of(step_exec: Optional[dict[str, Any]]) -> str:
    if step_exec is None:
        return "not_reached"
    outcome = (step_exec.get("outcome") or "").lower()
    if outcome in ("success", "failure", "unknown"):
        return outcome
    return "pending"


def _friendly_cause(detail: str) -> tuple[str, str]:
    """Map a step detail string to (root_cause, suggested_fix)."""
    d = (detail or "").lower()
    if "circuit breaker" in d:
        return ("Repeated failures tripped the per-tool circuit breaker; the "
                "downstream tool is likely unhealthy.",
                "Investigate the downstream service. Tune circuit thresholds or "
                "add a fallback before retrying.")
    if "rate" in d and "limit" in d:
        return ("The downstream tool rate-limited the call.",
                "Lower concurrency or increase backoff; honor Retry-After.")
    if "timeout" in d or "timed out" in d:
        return ("The call timed out. The outcome was unknown, so the reconciler "
                "queried the tool rather than blindly retrying.",
                "Increase the step timeout if the tool is legitimately slow, or "
                "ensure the tool exposes a status/query endpoint.")
    if "unknown" in d:
        return ("The tool returned an ambiguous/unknown outcome and was "
                "reconciled by querying actual state.",
                "Confirm the tool honors idempotency keys and reports status "
                "truthfully so reconciliation is reliable.")
    if "validation" in d or "invalid" in d:
        return ("A validation error - a non-retryable, deterministic failure.",
                "Fix the step inputs/preconditions; retries will not help.")
    if "business" in d or "rule" in d or "declined" in d or "insufficient" in d:
        return ("A business-rule rejection (e.g. declined / insufficient funds).",
                "This is expected domain behavior; handle it in the plan rather "
                "than retrying.")
    if "authorization" in d or "auth" in d or "forbidden" in d:
        return ("An authorization failure.",
                "Check credentials/scopes for the tool; not retryable.")
    if "unavailable" in d or "not found" in d or "resource" in d or "404" in d:
        return ("A required resource was unavailable (e.g. sold out / not found).",
                "This is a domain outcome, not a transient fault; handle it in the "
                "plan (alternative resource or graceful abort) rather than retrying.")
    if "crash" in d or "wal" in d:
        return ("The process crashed mid-flight; the run was rebuilt from the "
                "write-ahead log and reconciled.",
                "None required - this demonstrates crash recovery. Ensure "
                "durable storage in production.")
    return (detail or "Step did not complete successfully.",
            "Review the step detail and the tool's response.")


def build_flow_model(report: dict[str, Any]) -> dict[str, Any]:
    """Compute the graph + breaking analysis from an evidence report dict."""
    result = report.get("result", {})
    inventory = {t["step_id"]: t for t in report.get("tool_inventory", [])}
    execs = {s["step_id"]: s for s in result.get("steps", [])}
    comps = result.get("compensations", [])
    comp_ok = {c["source_step_id"] for c in comps
               if (c.get("outcome") or "").lower() == "success"}
    pivot_id = result.get("pivot_step_id")
    pivot_crossed = bool(result.get("pivot_crossed"))
    state = (result.get("state") or "").lower()

    # --- ordered node list (execution order = tool_inventory order) ----------
    order = [t["step_id"] for t in report.get("tool_inventory", [])]
    # Fall back to step execution order if inventory is empty.
    if not order:
        order = [s["step_id"] for s in result.get("steps", [])]

    nodes: list[dict[str, Any]] = []
    for sid in order:
        inv = inventory.get(sid, {})
        se = execs.get(sid)
        status = _outcome_of(se)
        if sid in comp_ok and status != "failure":
            # Successfully executed then compensated/rolled back.
            status = "compensated"
        pc = (se or {}).get("postcondition")
        nodes.append({
            "step_id": sid,
            "name": inv.get("name") or sid,
            "tool": inv.get("tool", ""),
            "tier": inv.get("tier", 0),
            "reversibility": inv.get("reversibility", "unknown"),
            "risk_level": inv.get("risk_level", ""),
            "is_pivot": bool(inv.get("is_pivot")),
            "is_post_pivot": bool(inv.get("is_post_pivot")),
            "approval_required": bool(inv.get("approval_required")),
            "depends_on": inv.get("depends_on", []),
            "status": status,
            "attempt": (se or {}).get("attempt", 0),
            "reconciled": bool((se or {}).get("reconciled")),
            "approval_state": (se or {}).get("approval_state", ""),
            "postcondition_passed": (pc or {}).get("passed") if pc else None,
            "detail": (se or {}).get("detail", ""),
            "compensation": inv.get("compensation"),
        })

    # --- edges ----------------------------------------------------------------
    ids = set(order)
    edges: list[dict[str, str]] = []
    for i, sid in enumerate(order):
        deps = inventory.get(sid, {}).get("depends_on") or []
        real_deps = [d for d in deps if d in ids]
        if real_deps:
            for d in real_deps:
                edges.append({"from": d, "to": sid, "kind": "flow"})
        elif i > 0:
            # No explicit deps: chain in execution order so the pipeline reads L->R.
            edges.append({"from": order[i - 1], "to": sid, "kind": "flow"})
    # Compensation edges (reverse), in the order compensations ran.
    for c in comps:
        edges.append({"from": c["source_step_id"], "to": c["source_step_id"],
                      "kind": "compensation"})

    # --- breaking analysis ----------------------------------------------------
    analysis = _analyze_break(nodes, execs, order, pivot_id, pivot_crossed, state,
                              comps, comp_ok, result, report)

    return {
        "nodes": nodes,
        "edges": edges,
        "pivot_step_id": pivot_id,
        "pivot_crossed": pivot_crossed,
        "analysis": analysis,
        "result": result,
        "meta": report.get("meta", {}),
        "explanation": report.get("explanation", ""),
        "evaluation": report.get("evaluation", {}),
    }


def _analyze_break(nodes, execs, order, pivot_id, pivot_crossed, state, comps,
                   comp_ok, result, report) -> dict[str, Any]:
    # Locate the break: first failure, else the first unresolved step on an
    # unclean run, else none.
    break_step: Optional[str] = None
    for sid in order:
        if _outcome_of(execs.get(sid)) == "failure":
            break_step = sid
            break
    if break_step is None and state in _UNCLEAN_STATES:
        for sid in order:
            se = execs.get(sid)
            oc = _outcome_of(se)
            if oc in ("unknown", "pending") and not (se or {}).get("reconciled"):
                break_step = sid
                break
        if break_step is None:
            # Last step that actually ran.
            ran = [sid for sid in order if execs.get(sid) is not None]
            break_step = ran[-1] if ran else None

    broke = break_step is not None and state != "completed"

    if not broke:
        return {
            "broke": False,
            "headline": "No break - the workflow completed consistently.",
            "break_step": None,
            "phase": None,
            "root_cause": "",
            "suggested_fix": "",
            "recovery_mode": "none",
            "recovery_ok": True,
            "uncompensated": [],
            "residual_risks": result.get("residual_risks", []),
        }

    node: dict[str, Any] = next(
        (n for n in nodes if n["step_id"] == break_step), {})
    detail = node.get("detail", "")
    root_cause, suggested_fix = _friendly_cause(detail)

    # Before or after the pivot?
    if pivot_id is None:
        phase = "no-pivot"
    else:
        pivot_idx = order.index(pivot_id) if pivot_id in order else -1
        break_idx = order.index(break_step) if break_step in order else -1
        phase = "post-pivot" if (pivot_crossed or (break_idx > pivot_idx >= 0)) \
            else "pre-pivot"

    if pivot_crossed and comps:
        recovery_mode = "forward recovery (post-pivot; e.g. refund = new action)"
    elif comps:
        recovery_mode = "compensation (pre-pivot rollback, reverse order)"
    else:
        recovery_mode = "none"
    recovery_ok = all((c.get("outcome") or "").lower() == "success" for c in comps)

    # Effects that ran successfully but were not rolled back on an unclean run.
    uncompensated = [
        n["step_id"] for n in nodes
        if n["status"] in ("success",) and n["step_id"] not in comp_ok
        and n["tier"] >= 2
    ] if state != "completed" else []

    headline = (
        f"Broke at {break_step} ({node.get('tool', '')}) "
        f"{phase.replace('-', ' ')}."
    )

    return {
        "broke": True,
        "headline": headline,
        "break_step": break_step,
        "break_tool": node.get("tool", ""),
        "break_tier": node.get("tier", 0),
        "phase": phase,
        "detail": detail,
        "root_cause": root_cause,
        "suggested_fix": suggested_fix,
        "recovery_mode": recovery_mode,
        "recovery_ok": recovery_ok,
        "uncompensated": uncompensated,
        "residual_risks": result.get("residual_risks", []),
        "final_state": result.get("state", ""),
    }


def render_flow_html(
    report: dict[str, Any],
    replay_frames: Optional[list[dict[str, Any]]] = None,
) -> str:
    model = build_flow_model(report)
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template("flow_view.html")
    # Embed the model as JSON for the client-side interactivity.
    model_json = json.dumps(model, default=str)
    # The deterministic-replay timeline powers the optional time-travel scrubber.
    replay_json = json.dumps(replay_frames or [], default=str)
    return template.render(model=model, model_json=model_json,
                           replay_json=replay_json)


class FlowVisualizer:
    """Generates the interactive flow HTML from a report (dict or saved JSON)."""

    def __init__(self, config: Optional[AATMConfig] = None) -> None:
        self.config = config or default_config

    def from_report_dict(self, report: dict[str, Any], run_id: str) -> Path:
        html = render_flow_html(report, replay_frames=self._replay_frames(run_id))
        out = self.flow_path(run_id)
        out.write_text(html, encoding="utf-8")
        return out

    def _replay_frames(self, run_id: str) -> list[dict[str, Any]]:
        """Best-effort deterministic-replay timeline for the time-travel scrubber.

        Never fatal: if the audit log is missing/unreadable the flow view simply
        renders without the scrubber.
        """
        try:
            from ..replay import ReplayEngine

            return ReplayEngine(self.config).replay(run_id).to_dict()["frames"]
        except Exception:  # noqa: BLE001 - scrubber is a progressive enhancement
            return []

    def from_run_id(self, run_id: str) -> Path:
        json_path = self.config.report_json_path(run_id)
        if not json_path.exists():
            raise FileNotFoundError(
                f"no report found for run {run_id}; run with --report first"
            )
        report = json.loads(json_path.read_text(encoding="utf-8"))
        return self.from_report_dict(report, run_id)

    def flow_path(self, run_id: str) -> Path:
        assert self.config.reports_dir is not None
        return self.config.reports_dir / f"{run_id}.flow.html"
