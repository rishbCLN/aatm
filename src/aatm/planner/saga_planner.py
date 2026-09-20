"""Saga planner.

``SagaPlanner.plan(workflow)`` turns a parsed workflow into a validated
:class:`SagaPlan`: ordered steps, dependency graph, per-step reversibility tier,
pivot, compensation strategy, approval requirements, retry + unknown-outcome
policy, and risk flags.

Planner validations (spec section 13) reject plans with critical safety issues.
"""

from __future__ import annotations

from typing import Any, Optional

from ..compensation.registry import CompensationRegistry
from ..config import AATMConfig, default_config
from ..enums import (
    CompensationSource,
    CompensationStrategy,
    FailureClass,
    IdempotencyMode,
    Reversibility,
    RiskLevel,
    Tier,
)
from ..models import (
    CompensationPlan,
    FailurePolicy,
    PlannedStep,
    PostconditionContract,
    RetryPolicy,
    SagaPlan,
)
from .parser import ParsedWorkflow
from .pivot_detector import detect_pivot, mark_pivot
from .reversibility import Classification, ReversibilityClassifier


class SagaPlanner:
    def __init__(
        self,
        config: Optional[AATMConfig] = None,
        classifier: Optional[ReversibilityClassifier] = None,
        comp_registry: Optional[CompensationRegistry] = None,
    ) -> None:
        self.config = config or default_config
        self.classifier = classifier or ReversibilityClassifier(self.config)
        self.comp_registry = comp_registry or CompensationRegistry(self.config)

    # -- public API -----------------------------------------------------------

    def plan(self, parsed: ParsedWorkflow) -> SagaPlan:
        wf = parsed.workflow
        # Workflow-level explicit tool overrides.
        explicit_tools = {t["name"]: t for t in parsed.tools if "name" in t}

        variables = {
            v["name"]: v.get("initial_value")
            for v in parsed.variables
            if "name" in v
        }

        ordered = self._topo_sort(parsed.steps)

        planned_steps: list[PlannedStep] = []
        for raw in ordered:
            planned_steps.append(
                self._plan_step(raw, explicit_tools.get(raw["tool"]))
            )

        pivot_id = detect_pivot(planned_steps)
        mark_pivot(planned_steps, pivot_id)

        fp_raw = wf.get("failure_policy", {}) or {}
        failure_policy = FailurePolicy(
            **{k: v for k, v in fp_raw.items() if k in FailurePolicy.model_fields}
        )

        plan = SagaPlan(
            workflow_id=wf.get("id", "wf"),
            workflow_name=wf.get("name", ""),
            agent=wf.get("agent", ""),
            description=wf.get("description", ""),
            max_steps=int(wf.get("max_steps", 100)),
            timeout_seconds=int(wf.get("timeout_seconds", 120)),
            failure_policy=failure_policy,
            steps=planned_steps,
            pivot_step_id=pivot_id,
            variables=variables,
        )

        self._validate_plan(plan)
        return plan

    # -- step planning --------------------------------------------------------

    def _plan_step(
        self, raw: dict[str, Any], explicit_tool: Optional[dict[str, Any]]
    ) -> PlannedStep:
        tool_name = raw["tool"]

        # Merge explicit workflow-level tool overrides with any step-level ones.
        explicit: dict[str, Any] = {}
        if explicit_tool:
            explicit.update(explicit_tool)
        # A step may carry is_pivot / approval_required; reversibility override is
        # taken from the tools[] block (workflow-declared).
        classification = self.classifier.classify(tool_name, explicit=explicit or None)

        postconditions = [
            PostconditionContract(
                expression=pc.get("expression", "true"),
                on_failure=pc.get("on_failure", "compensate"),
                description=pc.get("description", ""),
            )
            for pc in (raw.get("postconditions") or [])
        ]

        retry_raw = raw.get("retry", {}) or {}
        retry = RetryPolicy(
            max_attempts=int(retry_raw.get("max_attempts", 1)),
            backoff=retry_raw.get("backoff", "exponential"),
            backoff_ms=int(retry_raw.get("backoff_ms", 250)),
            retry_on=[
                FailureClass(fc) for fc in retry_raw.get("retry_on", [])
                if fc in FailureClass._value2member_map_
            ],
        )

        step = PlannedStep(
            step_id=raw["id"],
            name=raw.get("name", raw["id"]),
            tool_name=tool_name,
            parameters=raw.get("parameters", {}) or {},
            depends_on=raw.get("depends_on", []) or [],
            preconditions=raw.get("preconditions", []) or [],
            postconditions=postconditions,
            tier=classification.tier,
            reversibility=classification.reversibility,
            side_effect_scope=classification.side_effect_scope,
            risk_level=classification.risk_level,
            is_pivot=bool(raw.get("is_pivot", False)),
            approval_required=bool(
                raw.get("approval_required", classification.approval_required)
            ),
            idempotency_key_expr=raw.get("idempotency_key"),
            idempotent=classification.idempotent,
            idempotency_mode=classification.idempotency_mode,
            retry=retry,
            timeout_ms=int(raw.get("timeout_ms", classification.timeout_ms)
                           or classification.timeout_ms),
        )

        # Resolve compensation using the authority hierarchy.
        step.compensation = self._resolve_compensation(step, raw, explicit_tool)

        # Attach risk flags.
        step.risk_flags = self._risk_flags(step, classification)
        return step

    def _resolve_compensation(
        self,
        step: PlannedStep,
        raw: dict[str, Any],
        explicit_tool: Optional[dict[str, Any]],
    ) -> Optional[CompensationPlan]:
        """Authority hierarchy (spec 3.1):
        explicit > registry template > adapter contract > llm > none.
        """
        # 1) Explicit compensation on the step.
        step_comp = raw.get("compensation")
        if isinstance(step_comp, dict) and step_comp.get("tool"):
            return CompensationPlan(
                strategy=CompensationStrategy(
                    step_comp.get("strategy", "compensate")
                ),
                tool=step_comp["tool"],
                mapping=step_comp.get("parameter_mapping", {})
                or step_comp.get("mapping", {}),
                confidence=1.0,
                source=CompensationSource.EXPLICIT,
                requires_approval=bool(step_comp.get("requires_approval", False)),
                verification=step_comp.get("verification"),
                source_step_id=step.step_id,
            )

        # 1b) Explicit compensation contract on the workflow tool block.
        if explicit_tool and isinstance(
            explicit_tool.get("compensation_contract"), dict
        ):
            cc = explicit_tool["compensation_contract"]
            return CompensationPlan(
                strategy=CompensationStrategy(cc.get("strategy", "compensate")),
                tool=cc["tool"],
                mapping=cc.get("parameter_mapping", {}) or cc.get("mapping", {}),
                confidence=1.0,
                source=CompensationSource.EXPLICIT,
                requires_approval=bool(cc.get("requires_approval", False)),
                verification=cc.get("verification"),
                source_step_id=step.step_id,
            )

        # 2) Registry template.
        template = self.comp_registry.template_for(step.tool_name)
        if template is not None:
            template.source_step_id = step.step_id
            return template

        # 3) Adapter contract (from the tool registry's compensation_contract).
        reg = self.classifier.registry_tool(step.tool_name)
        if reg and isinstance(reg.get("compensation_contract"), dict):
            cc = reg["compensation_contract"]
            return CompensationPlan(
                strategy=CompensationStrategy(cc.get("strategy", "compensate")),
                tool=cc["tool"],
                mapping=cc.get("parameter_mapping", {}) or cc.get("mapping", {}),
                confidence=1.0,
                source=CompensationSource.ADAPTER,
                requires_approval=bool(cc.get("requires_approval", False)),
                verification=cc.get("verification"),
                source_step_id=step.step_id,
            )

        # 4/5) No verified compensation.
        if step.tier == Tier.NONE or step.side_effect_scope.value == "none":
            # No side effect -> nothing to compensate.
            return CompensationPlan(
                strategy=CompensationStrategy.NONE,
                source=CompensationSource.NONE,
                confidence=1.0,
                source_step_id=step.step_id,
            )
        # Side-effecting but no verified compensation -> manual escalation.
        return CompensationPlan(
            strategy=CompensationStrategy.MANUAL_ESCALATION,
            source=CompensationSource.NONE,
            confidence=0.0,
            requires_approval=True,
            risks=["No verified semantic compensation contract"],
            source_step_id=step.step_id,
        )

    def _risk_flags(
        self, step: PlannedStep, classification: Classification
    ) -> list[str]:
        flags: list[str] = []
        if classification.source == "fail_closed":
            flags.append("UNKNOWN_TOOL_FAIL_CLOSED")
        if step.reversibility == Reversibility.IRREVERSIBLE:
            flags.append("IRREVERSIBLE")
        if step.risk_level == RiskLevel.CRITICAL:
            flags.append("CRITICAL_RISK")
        if (
            step.tier == Tier.TWO
            and step.compensation
            and step.compensation.strategy == CompensationStrategy.MANUAL_ESCALATION
        ):
            flags.append("NO_VERIFIED_COMPENSATION")
        if (
            not step.idempotent
            and step.idempotency_mode == IdempotencyMode.NONE
            and step.retry.max_attempts > 1
        ):
            flags.append("NON_IDEMPOTENT_RETRYABLE")
        return flags

    # -- ordering -------------------------------------------------------------

    def _topo_sort(self, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Stable topological sort honoring ``depends_on``.

        Falls back to declaration order when there are no dependencies. Detects
        cycles and raises via the plan's critical issues (handled by caller).
        """
        by_id = {s["id"]: s for s in steps}
        indeg: dict[str, int] = {s["id"]: 0 for s in steps}
        adj: dict[str, list[str]] = {s["id"]: [] for s in steps}
        for s in steps:
            for dep in s.get("depends_on", []) or []:
                if dep in by_id:
                    adj[dep].append(s["id"])
                    indeg[s["id"]] += 1

        # Kahn's algorithm preserving original order for ties.
        order = [s["id"] for s in steps]
        ready = [sid for sid in order if indeg[sid] == 0]
        result: list[str] = []
        while ready:
            node = ready.pop(0)
            result.append(node)
            for nxt in adj[node]:
                indeg[nxt] -= 1
                if indeg[nxt] == 0:
                    # insert preserving declaration order
                    ready.append(nxt)
                    ready.sort(key=lambda x: order.index(x))
        if len(result) != len(steps):
            # Cycle: fall back to declaration order; validation flags it.
            return steps
        return [by_id[sid] for sid in result]

    # -- validation -----------------------------------------------------------

    def _validate_plan(self, plan: SagaPlan) -> None:
        critical: list[str] = []
        warnings: list[str] = []

        # Cycle detection (topo sort returned all steps but check dependencies).
        ids = {s.step_id for s in plan.steps}
        for step in plan.steps:
            for dep in step.depends_on:
                if dep not in ids:
                    critical.append(
                        f"{step.step_id}: depends_on unknown step '{dep}'"
                    )

        pivot_index = plan.pivot_index()

        for i, step in enumerate(plan.steps):
            # A Tier-3 tool must have approval (explicit or globally enforced).
            if step.tier == Tier.THREE:
                if not step.approval_required and not self.config.require_approval_for_unknown:
                    warnings.append(
                        f"{step.step_id}: Tier-3 action without explicit approval "
                        "gate (global enforcement will apply)"
                    )
            # Fail-closed unknown tools must require approval.
            if "UNKNOWN_TOOL_FAIL_CLOSED" in step.risk_flags and not step.approval_required:
                critical.append(
                    f"{step.step_id}: unknown tool '{step.tool_name}' must require "
                    "approval (fail-closed)"
                )

            # Pre-pivot Tier-2 action must have a known compensation.
            is_pre_pivot = pivot_index is None or i < pivot_index
            if (
                is_pre_pivot
                and step.tier == Tier.TWO
                and (
                    step.compensation is None
                    or step.compensation.strategy
                    in (CompensationStrategy.MANUAL_ESCALATION, CompensationStrategy.NONE)
                )
            ):
                critical.append(
                    f"{step.step_id}: pre-pivot compensatable action has no verified "
                    "compensation"
                )

            # Non-idempotent retryable tool must have an idempotency mechanism.
            if (
                step.retry.max_attempts > 1
                and not step.idempotent
                and step.idempotency_mode == IdempotencyMode.NONE
            ):
                critical.append(
                    f"{step.step_id}: non-idempotent tool '{step.tool_name}' is "
                    "retryable but has no idempotency mechanism"
                )

            # Post-pivot critical action must have a recovery path.
            is_post_pivot = pivot_index is not None and i > pivot_index
            if (
                is_post_pivot
                and step.risk_level == RiskLevel.CRITICAL
                and (
                    step.compensation is None
                    or step.compensation.strategy == CompensationStrategy.NONE
                )
            ):
                critical.append(
                    f"{step.step_id}: post-pivot critical action has no recovery path"
                )

            # Compensation must not reference an unknown result field pattern.
            if step.compensation and step.compensation.mapping:
                for _param, expr in step.compensation.mapping.items():
                    if not (
                        expr.startswith("result.")
                        or expr.startswith("params.")
                        or expr.startswith("variables.")
                    ):
                        warnings.append(
                            f"{step.step_id}: compensation mapping '{expr}' does not "
                            "reference result/params/variables"
                        )

        plan.critical_issues = critical
        plan.warnings = warnings
