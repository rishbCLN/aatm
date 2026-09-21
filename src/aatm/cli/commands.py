"""AATM command-line interface.

Commands (spec section 25):

    aatm plan <workflow.yaml>
    aatm run <workflow.yaml> [--inject <injection.yaml>] [--no-color] [--report]
    aatm recover --run-id <RUN_ID>
    aatm verify-audit --run-id <RUN_ID>
    aatm report --run-id <RUN_ID>
    aatm list-runs
    aatm inspect-run --run-id <RUN_ID>

Malformed workflow input yields a structured error and a non-zero exit code
(never a raw traceback).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Optional

from ..config import AATMConfig, default_config
from ..engine import AATMEngine, EngineError
from ..planner.parser import WorkflowParseError
from ..storage.audit_log import verify_audit_chain
from .visualize import render_run


def _print_err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


def _approval_policy(auto_deny_pivot: bool):
    def cb(step) -> bool:
        if auto_deny_pivot and step.is_pivot:
            return False
        return True
    return cb


# --- commands ---------------------------------------------------------------


def cmd_plan(args, config: AATMConfig) -> int:
    engine = AATMEngine(config)
    try:
        plan = engine.plan(args.workflow)
    except WorkflowParseError as exc:
        _print_err(exc.message)
        for d in exc.errors:
            print(f"  - {d}", file=sys.stderr)
        return 2

    print(f"Workflow: {plan.workflow_name}  ({plan.workflow_id})")
    print(f"Agent:    {plan.agent or '-'}")
    print(f"Pivot:    {plan.pivot_step_id or 'none'}")
    print(f"Valid:    {plan.is_valid}")
    print("Steps:")
    for s in plan.steps:
        comp = s.compensation.tool if s.compensation and s.compensation.tool else "-"
        flags = (" [" + ",".join(s.risk_flags) + "]") if s.risk_flags else ""
        marker = "  <PIVOT>" if s.is_pivot else ("  (post)" if s.is_post_pivot else "")
        print(f"  {s.step_id:8s} {s.tool_name:22s} T{int(s.tier)} "
              f"{str(s.reversibility):16s} comp={comp}{marker}{flags}")
    if plan.warnings:
        print("\nWarnings:")
        for w in plan.warnings:
            print(f"  - {w}")
    if plan.critical_issues:
        print("\nCRITICAL ISSUES (execution blocked):")
        for c in plan.critical_issues:
            print(f"  - {c}")
        return 1
    return 0


def cmd_run(args, config: AATMConfig) -> int:
    engine = AATMEngine(config)
    approval = _approval_policy(args.deny_pivot)
    try:
        output = asyncio.run(
            engine.run(
                args.workflow,
                injection_path=args.inject,
                approval_callback=approval,
                backoff_scale=0.0 if args.fast else 0.001,
            )
        )
    except WorkflowParseError as exc:
        _print_err(exc.message)
        for d in exc.errors:
            print(f"  - {d}", file=sys.stderr)
        return 2
    except EngineError as exc:
        _print_err(str(exc))
        return 1

    use_color = None
    if args.no_color:
        use_color = False
    print(render_run(output.run, output.plan, use_color=use_color))
    print(f"\nRUN_ID: {output.run.run_id}")

    if args.report:
        experiment = {"injection": str(args.inject) if args.inject else None,
                      "crashed": output.crashed}
        rep = engine.report(output, experiment=experiment)
        print(f"Report (JSON): {rep['_paths']['json']}")
        print(f"Report (HTML): {rep['_paths']['html']}")
        # Interactive flow + breaking-analysis view.
        from ..reporting.flow_view import FlowVisualizer

        flow_path = FlowVisualizer(config).from_report_dict(
            rep, str(output.run.run_id))
        print(f"Flow (HTML):   {flow_path}")
        print(f"Assessment:    {rep['score']['status']} "
              f"({rep['score']['total']}/100)")

    if output.crashed:
        print("\n[!] Process crash was simulated. Run 'aatm recover --run-id "
              f"{output.run.run_id}' to reconcile.")
        return 3
    return 0


def cmd_recover(args, config: AATMConfig) -> int:
    engine = AATMEngine(config)
    try:
        report, _reg = asyncio.run(engine.recover(args.run_id))
    except Exception as exc:  # noqa: BLE001
        _print_err(f"recovery failed: {exc}")
        return 1
    print("CRASH RECOVERY")
    print("-" * 40)
    print(f"Run:                 {report.run_id}")
    print(f"Pending intents:     {report.pending_found}")
    print(f"Duplicates prevented:{report.duplicates_prevented}")
    for r in report.reconciliations:
        print(f"  {r.step_id:10s} {r.tool:18s} -> {r.resolution}  {r.detail}")
    return 0


def cmd_resume(args, config: AATMConfig) -> int:
    engine = AATMEngine(config)
    approval = _approval_policy(args.deny_pivot)
    try:
        report, output = asyncio.run(
            engine.resume(
                args.workflow, args.run_id,
                approval_callback=approval,
                backoff_scale=0.0 if args.fast else 0.001,
            )
        )
    except WorkflowParseError as exc:
        _print_err(exc.message)
        for d in exc.errors:
            print(f"  - {d}", file=sys.stderr)
        return 2
    except EngineError as exc:
        _print_err(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        _print_err(f"resume failed: {exc}")
        return 1

    print("RESUME-FORWARD")
    print("-" * 40)
    print(f"Run:                 {report.run_id}")
    print(f"Reconciled intents:  {report.pending_found}")
    print(f"Duplicates prevented:{report.duplicates_prevented}")
    print()
    use_color = False if args.no_color else None
    print(render_run(output.run, output.plan, use_color=use_color))
    print(f"\nFinal state: {output.run.state}")
    if args.report:
        rep = engine.report(output, experiment={"resumed": True})
        print(f"Report (JSON): {rep['_paths']['json']}")
        print(f"Assessment:    {rep['score']['status']} ({rep['score']['total']}/100)")
    return 0


def cmd_redrive(args, config: AATMConfig) -> int:
    engine = AATMEngine(config)
    try:
        report = asyncio.run(engine.redrive(args.run_id))
    except Exception as exc:  # noqa: BLE001
        _print_err(f"redrive failed: {exc}")
        return 1
    print("DEAD-LETTER REDRIVE")
    print("-" * 40)
    print(f"Run:           {report.run_id}")
    print(f"Open before:   {report.open_before}")
    print(f"Resolved:      {report.resolved}")
    print(f"Still failed:  {report.still_failed}")
    for a in report.attempts:
        mark = "OK " if a.resolution == "resolved" else "!! "
        print(f"  {mark}{a.step_id:10s} {a.tool or '-':18s} {a.detail}")
    # Non-zero exit if anything remains unresolved (useful for scripting).
    return 0 if report.still_failed == 0 else 1


def cmd_verify_audit(args, config: AATMConfig) -> int:
    path = config.audit_log_path(args.run_id)
    if not path.exists():
        _print_err(f"audit log not found for run {args.run_id}")
        return 1
    result = verify_audit_chain(path, secret_key=config.audit_hmac_key)
    status = "VALID" if result.valid else "INVALID"
    print(f"Audit chain: {status}")
    print(f"Entries:     {result.entry_count}")
    print(f"Detail:      {result.detail}")
    if not result.valid:
        print(f"Broken at seq: {result.broken_seq}")
        return 1
    return 0


def cmd_replay(args, config: AATMConfig) -> int:
    from ..replay import ReplayEngine

    path = config.audit_log_path(args.run_id)
    if not path.exists():
        _print_err(f"audit log not found for run {args.run_id}")
        return 1
    result = ReplayEngine(config).replay(args.run_id, to_seq=args.to_seq)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2, default=str))
        return 0 if result.chain_valid else 1

    chain = "VALID" if result.chain_valid else f"INVALID ({result.chain_detail})"
    target = "end" if args.to_seq is None else f"seq {args.to_seq}"
    print("DETERMINISTIC REPLAY")
    print("-" * 60)
    print(f"Run:          {result.run_id}")
    print(f"Audit chain:  {chain}  ({result.total_events} events)")
    print(f"Replaying to: {target}  ({len(result.frames)} event(s) applied)")
    print()
    print("TIMELINE")
    for f in result.frames:
        ent = f".{f.entity_id}" if f.entity_id else ""
        print(f"  {f.seq:>3}  {f.event:<22}{ent:<12}  {f.note}")

    frame = result.final_frame
    if frame is not None:
        print()
        pv = "yes" if frame.pivot_crossed else "no"
        print(f"STATE @ seq {frame.seq}:")
        print(f"  workflow: {frame.workflow_state}   pivot_crossed: {pv}   "
              f"side-effects: {frame.side_effects}")
        for sid, st in frame.steps.items():
            extra = f"  {st['detail']}" if st.get("detail") else ""
            appr = f"  approval={st['approval']}" if st.get("approval") else ""
            print(f"  {sid:<10} {st['status']:<12} {st.get('tool', ''):<22}"
                  f"{appr}{extra}")
    return 0 if result.chain_valid else 1


def cmd_report(args, config: AATMConfig) -> int:
    json_path = config.report_json_path(args.run_id)
    if not json_path.exists():
        _print_err(
            f"no report found for run {args.run_id}. Run with --report first."
        )
        return 1
    data = json.loads(json_path.read_text(encoding="utf-8"))
    print(f"Assessment:  {data['score']['status']} ({data['score']['total']}/100)")
    print(f"State:       {data['result']['state']}")
    print(f"Pivot:       {data['result']['pivot_step_id']} "
          f"(crossed={data['result']['pivot_crossed']})")
    print(f"Audit chain: {'VALID' if data['result']['audit_chain_valid'] else 'INVALID'}")
    print(f"JSON:        {json_path}")
    print(f"HTML:        {config.report_html_path(args.run_id)}")
    return 0


def cmd_list_runs(args, config: AATMConfig) -> int:
    audit_dir = config.audit_dir
    seen: set[str] = set()
    if audit_dir and audit_dir.exists():
        for p in sorted(audit_dir.glob("*.audit.jsonl")):
            run_id = p.name.replace(".audit.jsonl", "")
            seen.add(run_id)
    if not seen:
        print("No runs found.")
        return 0
    print("Runs:")
    for run_id in sorted(seen):
        report_path = config.report_json_path(run_id)
        status = "-"
        if report_path.exists():
            try:
                data = json.loads(report_path.read_text(encoding="utf-8"))
                status = data["score"]["status"]
            except Exception:  # noqa: BLE001
                status = "?"
        print(f"  {run_id}  {status}")
    return 0


def cmd_approve(args, config: AATMConfig) -> int:
    from ..storage.approvals import ApprovalStore

    store = ApprovalStore(config.approval_path(args.run_id))
    granted = args.command == "approve"
    store.decide(args.step, granted, decided_by=args.by,
                 reason=args.reason or "")
    verb = "GRANTED" if granted else "DENIED"
    print(f"Approval {verb} for run {args.run_id} step {args.step} (by {args.by}).")
    return 0


def cmd_flow(args, config: AATMConfig) -> int:
    from ..reporting.flow_view import FlowVisualizer

    viz = FlowVisualizer(config)
    try:
        path = viz.from_run_id(args.run_id)
    except FileNotFoundError as exc:
        _print_err(str(exc))
        return 1
    print(f"Flow (HTML): {path}")
    if args.open:
        import webbrowser

        webbrowser.open(path.as_uri())
    return 0


def cmd_serve(args, config: AATMConfig) -> int:  # pragma: no cover
    from ..service import serve

    print(f"AATM service on http://{args.host}:{args.port} "
          f"(Ctrl-C to stop)")
    serve(host=args.host, port=args.port, config=config)
    return 0


def cmd_approvals(args, config: AATMConfig) -> int:
    from ..storage.approvals import ApprovalStore

    store = ApprovalStore(config.approval_path(args.run_id))
    entries = store.entries()
    if not entries:
        print(f"No approval records for run {args.run_id}.")
        return 0
    print(f"Approval log for run {args.run_id}:")
    for e in entries:
        if e.get("kind") == "request":
            print(f"  REQUEST  {e['step_id']:10s} {e.get('tool','')}"
                  f"  deadline={e.get('deadline','')}")
        else:
            verb = "GRANT" if e.get("granted") else "DENY "
            print(f"  {verb}    {e['step_id']:10s} by={e.get('decided_by','')}"
                  f"  {e.get('reason','')}")
    pending = store.pending()
    if pending:
        print(f"Pending: {', '.join(p['step_id'] for p in pending)}")
    return 0


def cmd_inspect_run(args, config: AATMConfig) -> int:
    path = config.audit_log_path(args.run_id)
    if not path.exists():
        _print_err(f"no audit log for run {args.run_id}")
        return 1
    from ..storage.audit_log import AuditLog

    log = AuditLog(path, secret_key=config.audit_hmac_key)
    entries = log.entries()
    print(f"Run {args.run_id}: {len(entries)} audit events")
    for e in entries:
        payload = json.dumps(e.get("payload", {}), default=str)
        if len(payload) > 80:
            payload = payload[:77] + "..."
        print(f"  #{e['seq']:>3} {e['event']:24s} {e['entity_id']:12s} {payload}")
    chain = log.verify()
    print(f"\nChain: {'VALID' if chain.valid else 'INVALID'} - {chain.detail}")
    return 0


# --- parser -----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="aatm",
        description="Agent Action Transaction Manager - runtime safety/recovery "
        "layer for AI-agent tool calls.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("plan", help="Parse + plan a workflow.")
    sp.add_argument("workflow")
    sp.set_defaults(func=cmd_plan)

    sr = sub.add_parser("run", help="Execute a workflow.")
    sr.add_argument("workflow")
    sr.add_argument("--inject", default=None, help="failure injection YAML")
    sr.add_argument("--report", action="store_true", help="generate evidence report")
    sr.add_argument("--no-color", action="store_true")
    sr.add_argument("--deny-pivot", action="store_true",
                    help="deny approval at the pivot (demo of approval-denied path)")
    sr.add_argument("--fast", action="store_true",
                    help="zero backoff (faster demo/tests)")
    sr.set_defaults(func=cmd_run)

    rc = sub.add_parser("recover", help="Recover a crashed run via WAL replay.")
    rc.add_argument("--run-id", required=True)
    rc.set_defaults(func=cmd_recover)

    rs = sub.add_parser("resume", help="Reconcile a crashed run, then drive the "
                        "remaining steps forward to completion.")
    rs.add_argument("workflow")
    rs.add_argument("--run-id", required=True)
    rs.add_argument("--report", action="store_true", help="generate evidence report")
    rs.add_argument("--no-color", action="store_true")
    rs.add_argument("--deny-pivot", action="store_true")
    rs.add_argument("--fast", action="store_true", help="zero backoff")
    rs.set_defaults(func=cmd_resume)

    rd = sub.add_parser("redrive", help="Re-attempt a run's open dead-letter "
                        "entries (unrecoverable compensations / escalations).")
    rd.add_argument("--run-id", required=True)
    rd.set_defaults(func=cmd_redrive)

    va = sub.add_parser("verify-audit", help="Verify a run's audit hash chain.")
    va.add_argument("--run-id", required=True)
    va.set_defaults(func=cmd_verify_audit)

    rp = sub.add_parser("replay", help="Deterministically reconstruct a run's "
                        "state timeline from the audit log (read-only).")
    rp.add_argument("--run-id", required=True)
    rp.add_argument("--to-seq", type=int, default=None,
                    help="reconstruct state as of this audit seq (time-travel)")
    rp.add_argument("--json", action="store_true", help="emit the timeline as JSON")
    rp.set_defaults(func=cmd_replay)

    rp = sub.add_parser("report", help="Show a run's report summary.")
    rp.add_argument("--run-id", required=True)
    rp.set_defaults(func=cmd_report)

    lr = sub.add_parser("list-runs", help="List known runs.")
    lr.set_defaults(func=cmd_list_runs)

    ir = sub.add_parser("inspect-run", help="Dump a run's audit events.")
    ir.add_argument("--run-id", required=True)
    ir.set_defaults(func=cmd_inspect_run)

    for verb in ("approve", "deny"):
        ap = sub.add_parser(verb, help=f"{verb.capitalize()} a pending Tier-3 "
                            "approval (out-of-band).")
        ap.add_argument("--run-id", required=True)
        ap.add_argument("--step", required=True, help="step id to decide")
        ap.add_argument("--by", default="operator", help="who decided")
        ap.add_argument("--reason", default="", help="optional reason")
        ap.set_defaults(func=cmd_approve)

    al = sub.add_parser("approvals", help="Show a run's approval log.")
    al.add_argument("--run-id", required=True)
    al.set_defaults(func=cmd_approvals)

    fl = sub.add_parser("flow", help="Generate the interactive flow + breaking "
                        "analysis HTML for a run.")
    fl.add_argument("--run-id", required=True)
    fl.add_argument("--open", action="store_true", help="open in the browser")
    fl.set_defaults(func=cmd_flow)

    sv = sub.add_parser("serve", help="Run the local HTTP API (dev/service mode).")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8080)
    sv.set_defaults(func=cmd_serve)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = default_config
    try:
        return int(args.func(args, config))
    except KeyboardInterrupt:  # pragma: no cover
        _print_err("interrupted")
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
