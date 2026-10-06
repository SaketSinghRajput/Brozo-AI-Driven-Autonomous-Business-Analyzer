"""PlanningService: capability report and validated default plan of a stored dataset version
(TASK-ANALYSIS-002/005/006, TASK-INTEGRATION-003; design 17).

Loads the version's persisted Understanding, current (or requested) SemanticModel and profile in
the caller's workspace scope, then runs the deterministic planner stack. Nothing is persisted:
the report and plan are pure functions of versioned inputs (dataset version, semantic model
version, rules version, analysis config version), so they are recomputed on demand. The
``run_plan`` job (PlanExecutor stream, TASK-ANALYSIS-007) calls ``default_plan`` and executes
the returned ``ValidatedPlan``.
"""

from dataclasses import dataclass

from app.core.errors import NotFoundError
from app.core.scope import WorkspaceScope
from app.data.profiling.domain import ProfileResult
from app.data.profiling.profiler import ENGINE_VERSION
from app.data.repository import DatasetRepository
from app.data.understanding_store import UnderstandingStore
from app.infra.db import UnitOfWork
from app.planner.capabilities import CapabilityEvaluator
from app.planner.default_planner import DefaultPlanner
from app.planner.domain import CapabilityReport, Unavailability
from app.planner.facts import DataFacts, build_facts
from app.planner.runs import AnalysisRun, AnalysisRunRepository, StepState
from app.planner.validator import PlanValidator, ValidatedPlan
from app.tools.registry import ToolRegistry


def attempted(run: AnalysisRun | None, available: list[str]) -> list[Unavailability]:
    """U-4: available analyses the run's tools refused with a typed reason, once each.
    ponytail: FAILED steps are not listed (their card says "could not be completed")."""
    found: dict[str, Unavailability] = {}
    for record in run.steps.values() if run else []:
        gap = record.unavailable
        if record.state is StepState.UNAVAILABLE and gap and gap.analysis in available:
            found.setdefault(gap.analysis, gap)
    return list(found.values())


@dataclass(frozen=True)
class PlannedDashboard:
    validated: ValidatedPlan
    deferred: dict[str, str]


class PlanningService:
    def __init__(
        self,
        uow: UnitOfWork,
        registry: ToolRegistry,
        evaluator: CapabilityEvaluator | None = None,
    ) -> None:
        self._uow = uow
        self._evaluator = evaluator or CapabilityEvaluator()
        self._planner = DefaultPlanner(registry)
        self._validator = PlanValidator(registry)

    def facts(
        self,
        scope: WorkspaceScope,
        dataset_id: str,
        version_id: str,
        semantic_model_version: int | None = None,
    ) -> DataFacts:
        with self._uow(scope) as s:
            repo, store = DatasetRepository(s), UnderstandingStore(s)
            version = repo.get_version(scope, version_id)
            if version is None or version.dataset_id != dataset_id:
                raise NotFoundError("dataset version not found")
            model = store.semantic_model(scope, version_id, semantic_model_version)
            understanding = store.latest(scope, version_id)
            profile = repo.get_profile(scope, version_id, ENGINE_VERSION)
        if model is None or understanding is None or profile is None:
            raise NotFoundError("the version has no semantic model (version) yet")
        return build_facts(understanding, model, ProfileResult.model_validate(profile))

    def capabilities(
        self,
        scope: WorkspaceScope,
        dataset_id: str,
        version_id: str,
        semantic_model_version: int | None = None,
    ) -> CapabilityReport:
        facts = self.facts(scope, dataset_id, version_id, semantic_model_version)
        report = self._evaluator.evaluate(facts)
        with self._uow(scope) as s:
            run = AnalysisRunRepository(s).latest_finished(
                scope, version_id, report.semantic_model_version
            )
        return report.model_copy(update={"attempted": attempted(run, report.available)})

    def default_plan(
        self,
        scope: WorkspaceScope,
        dataset_id: str,
        version_id: str,
        semantic_model_version: int | None = None,
    ) -> PlannedDashboard:
        facts = self.facts(scope, dataset_id, version_id, semantic_model_version)
        report = self._evaluator.evaluate(facts)
        planned = self._planner.plan(facts, report)
        return PlannedDashboard(self._validator.validate(planned.plan, report), planned.deferred)
