"""Evidence / reliability report generation (JSON + HTML).

Builds a machine-readable :class:`RunResult`, computes the engineering score,
renders an HTML report via Jinja2, and writes both artifacts. All content is
derived from the real execution trace (WAL, audit log, world state) - never
hard-coded sample text.

Wording rules (spec section 20/36): the report records observed engineering
controls and recovery behavior. It never claims regulatory certification, legal
compliance, zero risk, or true rollback of irreversible actions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .. import BUILD_VERSION
from ..config import AATMConfig, default_config
from ..enums import WorkflowState
from ..models import RunResult, WorkflowRun, utcnow
from ..storage.audit_log import AuditLog
from .explainer import RecoveryExplainer
from .scoring import Scorer

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

DISCLAIMER = (
    "This report records the engineering controls and observed recovery behavior "
    "for the tested workflow. It is evidence for engineering review; it is not "
    "legal or regulatory certification."
)


class EvidenceReport:
    def __init__(self, config: Optional[AATMConfig] = None) -> None:
        self.config = config or default_config
        self.config.ensure_dirs()
        self.scorer = Scorer()
        self.explainer = RecoveryExplainer(enable_llm=self.config.enable_llm)
        self._env = Environment(
            loader=FileSystemLoader(str(_TEMPLATES_DIR)),
            autoescape=select_autoescape(["html", "xml"]),
        )

    # -- result assembly ------------------------------------------------------

    def build_result(
        self,
        run: WorkflowRun,
        *,
        plan=None,
        world_summary: Optional[dict[str, Any]] = None,
        duration_ms: float = 0.0,
        retries: int = 0,
        unknown_outcomes: int = 0,
        approvals: Optional[list[dict[str, Any]]] = None,
    ) -> RunResult:
        audit = AuditLog(self.config.audit_log_path(str(run.run_id)),
                         secret_key=self.config.audit_hmac_key)
        chain = audit.verify()

        residual_risks: list[str] = []
        for c in run.compensations:
            residual_risks.extend(c.residual_risk)

        failed_assertions: list[str] = []
        for s in run.step_executions:
            if s.postcondition is not None and not s.postcondition.passed:
                failed_assertions.append(
                    f"{s.step_id}: {s.postcondition.detail}"
                )

        consistent = run.state in (WorkflowState.COMPLETED, WorkflowState.ABORTED)
        exact_rollback = not run.pivot_crossed

        result = RunResult(
            run_id=str(run.run_id),
            workflow_id=run.workflow_id,
            workflow_name=run.workflow_name,
            agent=run.agent,
            build_version=BUILD_VERSION,
            state=run.state,
            pivot_step_id=run.pivot_step_id,
            pivot_crossed=run.pivot_crossed,
            started_at=run.created_at.isoformat(),
            finished_at=run.updated_at.isoformat(),
            duration_ms=duration_ms,
            steps=run.step_executions,
            compensations=run.compensations,
            retries=retries,
            unknown_outcomes=unknown_outcomes,
            approvals=approvals or [],
            final_world_state=world_summary or {},
            audit_chain_valid=chain.valid,
            audit_event_count=chain.entry_count,
            residual_risks=sorted(set(residual_risks)),
            failed_assertions=failed_assertions,
            consistent=consistent,
            exact_rollback_possible=exact_rollback,
            detail=run.detail,
        )
        return result

    # -- generation -----------------------------------------------------------

    def generate(
        self,
        run: WorkflowRun,
        *,
        plan=None,
        world_summary: Optional[dict[str, Any]] = None,
        duration_ms: float = 0.0,
        retries: int = 0,
        unknown_outcomes: int = 0,
        approvals: Optional[list[dict[str, Any]]] = None,
        experiment: Optional[dict[str, Any]] = None,
        metrics: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        result = self.build_result(
            run, plan=plan, world_summary=world_summary, duration_ms=duration_ms,
            retries=retries, unknown_outcomes=unknown_outcomes, approvals=approvals,
        )
        audit = AuditLog(self.config.audit_log_path(str(run.run_id)),
                         secret_key=self.config.audit_hmac_key)
        breakdown = self.scorer.score(result, audit)
        explanation = self.explainer.explain(run, pivot_step_id=run.pivot_step_id)
        evaluation = self.explainer.evaluate(run)

        tool_inventory = self._tool_inventory(plan)

        # Durable dead-letter queue (unrecoverable compensations, if any).
        from ..storage.dead_letter import DeadLetterQueue

        dlq = DeadLetterQueue(self.config.dead_letter_path(str(run.run_id)))
        dead_letters = dlq.entries()

        report = {
            "meta": {
                "build_version": BUILD_VERSION,
                "generated_at": utcnow().isoformat(),
                "disclaimer": DISCLAIMER,
            },
            "result": result.model_dump(mode="json"),
            "score": breakdown.to_dict(),
            "explanation": explanation,
            "evaluation": evaluation,
            "tool_inventory": tool_inventory,
            "experiment": experiment or {},
            "audit_chain": audit.verify().to_dict(),
            "metrics": metrics or {},
            "dead_letters": dead_letters,
        }

        # Redact PII/secrets before the report is written or rendered. Audit
        # hashes/UUIDs are preserved (see redaction heuristics), so the audit
        # chain summary stays intact.
        if self.config.redact_pii:
            from ..redaction import Redactor

            report = Redactor(enabled=True).redact(report)

        # Write JSON.
        json_path = self.config.report_json_path(str(run.run_id))
        json_path.write_text(json.dumps(report, indent=2, default=str),
                             encoding="utf-8")

        # Write HTML.
        html = self._render_html(report)
        html_path = self.config.report_html_path(str(run.run_id))
        html_path.write_text(html, encoding="utf-8")

        report["_paths"] = {"json": str(json_path), "html": str(html_path)}
        return report

    def _tool_inventory(self, plan) -> list[dict[str, Any]]:
        if plan is None:
            return []
        inventory = []
        for s in plan.steps:
            inventory.append({
                "step_id": s.step_id,
                "name": s.name,
                "tool": s.tool_name,
                "tier": int(s.tier),
                "reversibility": str(s.reversibility),
                "side_effect_scope": str(s.side_effect_scope),
                "risk_level": str(s.risk_level),
                "depends_on": list(s.depends_on),
                "is_pivot": s.is_pivot,
                "is_post_pivot": s.is_post_pivot,
                "approval_required": s.approval_required,
                "compensation": {
                    "tool": s.compensation.tool if s.compensation else None,
                    "strategy": str(s.compensation.strategy) if s.compensation
                    else None,
                    "source": str(s.compensation.source) if s.compensation else None,
                } if s.compensation else None,
                "risk_flags": s.risk_flags,
            })
        return inventory

    def _render_html(self, report: dict[str, Any]) -> str:
        template = self._env.get_template("reliability_report.html")
        return template.render(report=report)
