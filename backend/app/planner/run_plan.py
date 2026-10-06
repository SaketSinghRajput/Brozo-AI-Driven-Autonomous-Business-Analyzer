"""``run_plan`` job handler (TASK-ANALYSIS-007, TASK-INTEGRATION-004/005; design §9, §18, §28).

Payload {dataset_id, version_id, semantic_model_version}, enqueued by the outbox for every
``SemanticModelChanged``. Steps:
1. Skip (no-op) a stale job: its semantic model version is no longer the version's current one
   (a newer override queued its own job), or the version is not SEMANTICALLY_MAPPED/ANALYZING.
2. SEMANTICALLY_MAPPED → ANALYZING (CAS). A retried job finds ANALYZING and resumes.
3. PlanningService.default_plan → persist plan + run (run id derived from the job id, so a
   retry resumes the same run and its persisted step states).
4. PlanExecutor: every step through the ToolExecutor (evidence per step).
5. DashboardService builds and validates the spec; it is stored on the run.
6. ANALYZING → READY. A run in which every step failed, a cancellation or a permanent error
   moves the version to ANALYSIS_FAILED with the reason; transient errors propagate (job retry).
"""

import asyncio
import hashlib
import logging

from app.core.container import Container
from app.core.errors import ApplicationError, DataError
from app.core.scope import WorkspaceScope
from app.dashboard.service import DashboardService
from app.data.domain import DatasetFailure, DatasetState
from app.data.repository import DatasetRepository
from app.data.understanding_pipeline import UNCERTAIN
from app.data.understanding_store import UnderstandingStore
from app.jobs.models import Job
from app.jobs.worker import Handler, JobCancelled, JobContext
from app.planner.executor import PlanExecutor
from app.planner.runs import AnalysisRunRepository, RunState, StepRecord
from app.planner.service import PlanningService
from app.tools.domain import ToolContext
from app.tools.permissions import PROFILE_TOOL_PERMISSIONS, TurnProfile
from app.validator.service import ValidationService

log = logging.getLogger("app.planner.run_plan")
S = DatasetState
PRINCIPAL = "system:worker"


def _cancelled(ctx: JobContext) -> bool:
    try:
        ctx.raise_if_cancelled()
    except JobCancelled:
        return True
    return False


def run_plan_handler(container: Container) -> Handler:
    uow = container.uow
    planning = PlanningService(uow, container.tools)
    executor = PlanExecutor(container.tool_executor, container.tools)
    dashboards = DashboardService(uow, ValidationService(uow, container.ids), container.clock)

    def fail(scope: WorkspaceScope, version_id: str, code: str, message: str) -> None:
        failure = DatasetFailure(code=code, message=message)
        with uow(scope) as s:
            DatasetRepository(s).transition(
                scope, version_id, S.ANALYZING, S.ANALYSIS_FAILED, failure=failure
            )

    def run(job: Job, ctx: JobContext, scope: WorkspaceScope, generic_only: bool) -> str:
        dataset_id, version_id = str(job.payload["dataset_id"]), str(job.payload["version_id"])
        smv = int(job.payload["semantic_model_version"])
        planned = planning.default_plan(scope, dataset_id, version_id, smv)
        facts = planning.facts(scope, dataset_id, version_id, smv)
        plan, report = planned.validated.plan, planned.validated.report
        run_id = f"ar_{hashlib.sha256(job.id.encode()).hexdigest()[:26]}"
        with uow(scope) as s:
            runs = AnalysisRunRepository(s)
            runs.save_plan(scope, plan)
            started = runs.start(scope, run_id, plan, job.id)
        tool_ctx = ToolContext(
            workspace_id=scope.workspace_id,
            principal_id=PRINCIPAL,
            trace_id=job.trace_id or job.id,
            permissions=PROFILE_TOOL_PERMISSIONS[TurnProfile.DASHBOARD_SYSTEM],
            dataset_version_id=version_id,
            semantic_model_version=smv,
        )

        def on_step(record: StepRecord) -> None:
            with uow(scope) as s:
                AnalysisRunRepository(s).put_step(scope, run_id, record)

        state, records = asyncio.run(
            executor.execute(started, plan, tool_ctx, on_step, lambda: _cancelled(ctx))
        )
        if state in (RunState.CANCELLED, RunState.FAILED):
            with uow(scope) as s:
                AnalysisRunRepository(s).finish(scope, run_id, state)
            if state is RunState.CANCELLED:
                raise JobCancelled
            raise DataError("Every analysis step failed for this dataset version.")
        spec = asyncio.run(
            dashboards.build(scope, run_id, plan, report, records, facts, generic_only)
        )
        with uow(scope) as s:
            AnalysisRunRepository(s).finish(scope, run_id, state, spec.model_dump(mode="json"))
            DatasetRepository(s).transition(scope, version_id, S.ANALYZING, S.READY)
        return run_id

    def handle(job: Job, ctx: JobContext) -> str | None:
        scope = WorkspaceScope(workspace_id=job.workspace_id, principal_id=PRINCIPAL)
        version_id = str(job.payload["version_id"])
        wanted = int(job.payload["semantic_model_version"])
        with uow(scope) as s:
            repo, store = DatasetRepository(s), UnderstandingStore(s)
            version = repo.get_version(scope, version_id)
            model = store.semantic_model(scope, version_id)
            domain = store.domain(scope, version_id)
            if version is None or model is None or model.version != wanted:
                log.info("run_plan %s is stale (semantic model %s); skipped", job.id, wanted)
                return None
            if version.state is S.SEMANTICALLY_MAPPED:
                repo.transition(scope, version_id, S.SEMANTICALLY_MAPPED, S.ANALYZING)
            elif version.state is not S.ANALYZING:
                return None  # READY for this model already, or failed: nothing to do
        generic_only = domain is None or domain.classification.status in UNCERTAIN
        try:
            return run(job, ctx, scope, generic_only)
        except JobCancelled:
            fail(scope, version_id, "CANCELLED", "Analysis was cancelled.")
            raise
        except ApplicationError as exc:
            if exc.retryable:
                raise
            fail(scope, version_id, exc.code, exc.message)
            raise

    return handle
