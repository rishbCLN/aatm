"""High-level engine facade.

Ties the planner, coordinator, recovery manager, and reporting together behind a
small API used by the CLI and the demo. Keeps per-run wiring (config, injector,
approval policy) in one place.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import UUID, uuid4

import yaml

from .adapters.registry import AdapterRegistry
from .adapters.failures import CrashSignal, FailureInjector
from .config import AATMConfig, default_config
from .models import SagaPlan, WorkflowRun
from .planner import SagaPlanner, WorkflowParser
from .planner.parser import WorkflowParseError
from .reporting.evidence_report import EvidenceReport
from .runtime import RecoveryManager, TransactionCoordinator


class EngineError(Exception):
    """User-facing engine error (mapped to a non-zero CLI exit)."""


class PlanResult:
    def __init__(self, plan: SagaPlan) -> None:
        self.plan = plan


class RunOutput:
    def __init__(
        self,
        run: WorkflowRun,
        plan: SagaPlan,
        registry: AdapterRegistry,
        duration_ms: float,
        crashed: bool = False,
        metrics: Optional[dict[str, Any]] = None,
    ) -> None:
        self.run = run
        self.plan = plan
        self.registry = registry
        self.duration_ms = duration_ms
        self.crashed = crashed
        self.metrics = metrics or {}


class AATMEngine:
    def __init__(self, config: Optional[AATMConfig] = None) -> None:
        self.config = config or default_config
        self.config.ensure_dirs()

    # -- planning -------------------------------------------------------------

    def plan(self, workflow_path: str | Path) -> SagaPlan:
        parser = WorkflowParser(self.config)
        parsed = parser.parse(workflow_path)  # raises WorkflowParseError
        planner = SagaPlanner(self.config)
        return planner.plan(parsed)

    # -- injection loading ----------------------------------------------------

    def load_injector(self, injection_path: Optional[str | Path]) -> FailureInjector:
        if injection_path is None:
            return FailureInjector(enabled=False)
        p = Path(injection_path)
        if not p.exists():
            raise EngineError(f"injection file not found: {p}")
        with p.open("r", encoding="utf-8") as fh:
            spec = yaml.safe_load(fh) or {}
        return FailureInjector.from_spec(spec)

    # -- running --------------------------------------------------------------

    async def run(
        self,
        workflow_path: str | Path,
        *,
        injection_path: Optional[str | Path] = None,
        run_id: Optional[UUID] = None,
        approval_callback: Optional[Callable[[Any], bool]] = None,
        backoff_scale: float = 0.001,
        timeout_scale: float = 1.0,
    ) -> RunOutput:
        plan = self.plan(workflow_path)
        if not plan.is_valid:
            raise EngineError(
                "plan validation failed:\n  - " + "\n  - ".join(plan.critical_issues)
            )
        injector = self.load_injector(injection_path)
        rid = run_id or uuid4()
        registry = AdapterRegistry(
            injector=injector,
            world_persist_path=self.config.world_state_path(str(rid)),
        )
        coord = TransactionCoordinator(
            plan, registry, config=self.config, run_id=rid,
            approval_callback=approval_callback, backoff_scale=backoff_scale,
            timeout_scale=timeout_scale,
        )
        start = time.perf_counter()
        crashed = False
        try:
            run = await coord.run_workflow()
        except CrashSignal:
            crashed = True
            run = coord.run
        finally:
            duration_ms = (time.perf_counter() - start) * 1000.0
            metrics = coord.metrics.snapshot()
            coord.close()
        return RunOutput(run, plan, registry, duration_ms, crashed=crashed,
                         metrics=metrics)

    # -- recovery -------------------------------------------------------------

    async def recover(
        self,
        run_id: UUID | str,
        *,
        registry: Optional[AdapterRegistry] = None,
    ) -> tuple[Any, AdapterRegistry]:
        # Fresh registry that loads the authoritative external state persisted by
        # the crashed process (models the external systems' own durability).
        reg = registry or AdapterRegistry(
            world_persist_path=self.config.world_state_path(str(run_id))
        )
        manager = RecoveryManager(run_id, reg, config=self.config)
        try:
            # NOTE: do NOT restore the mock world from the pre-action checkpoint
            # here. The persisted world file models the EXTERNAL systems' own
            # durable state and is authoritative for reconciliation. Restoring the
            # stale checkpoint snapshot would hide a side effect that actually
            # committed server-side (e.g. a captured payment).
            report = await manager.recover()
        finally:
            manager.close()
        return report, reg

    # -- reporting ------------------------------------------------------------

    def report(
        self,
        output: RunOutput,
        *,
        experiment: Optional[dict[str, Any]] = None,
        retries: int = 0,
        unknown_outcomes: int = 0,
        approvals: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        reporter = EvidenceReport(self.config)
        # Prefer counters captured by the coordinator's metrics when available.
        counters = (output.metrics or {}).get("counters", {})
        return reporter.generate(
            output.run,
            plan=output.plan,
            world_summary=output.registry.world.summary(),
            duration_ms=output.duration_ms,
            retries=retries or int(counters.get("retries", 0)),
            unknown_outcomes=unknown_outcomes or int(counters.get("unknown_outcomes", 0)),
            approvals=approvals,
            experiment=experiment,
            metrics=output.metrics,
        )
