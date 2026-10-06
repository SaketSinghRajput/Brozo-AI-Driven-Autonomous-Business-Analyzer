"""Job handlers and outbox consumers for the dataset pipeline (TASK-DATA-011, TASK-PROFILE-009,
TASK-JOB-004, TASK-INTEGRATION-001/002).

Chain: upload enqueues `ingest`; ingestion enqueues `process_dataset` in its finalize transaction
(INGESTED -> PROFILED). The `DatasetProfiled` event is dispatched (app.jobs.outbox) to a second
`process_dataset` job, which runs the understanding stages (-> SEMANTICALLY_MAPPED). Every new
semantic model version emits `SemanticModelChanged`, dispatched to a `run_plan` job with payload
{dataset_id, version_id, semantic_model_version}, handled by app.planner.run_plan (plan →
execute → dashboard → READY).
Handlers are idempotent and run with a WorkspaceScope rebuilt from the job row (the job was
created in that workspace by an authorized request); the principal is the worker itself.
"""

import logging

from app.connectors.service import ConnectorService
from app.core.container import Container
from app.core.errors import DataError, StateConflictError
from app.core.scope import WorkspaceScope
from app.data.domain import DatasetFailure, DatasetState
from app.data.ingestion.errors import reject
from app.data.ingestion.service import IngestionService, IngestionSettings
from app.data.profiling.service import ProfilingService
from app.data.repository import DatasetRepository
from app.data.understanding_pipeline import UnderstandingPipeline
from app.infra.db import UnitOfWork
from app.jobs.models import Job, JobError, JobType
from app.jobs.outbox import Consumer, JobRequest, OutboxEvent
from app.jobs.worker import DeadJobHook, Handler, JobCancelled, JobContext
from app.planner.run_plan import run_plan_handler

DATA_JOBS = frozenset({JobType.INGEST, JobType.PROCESS_DATASET, JobType.RUN_PLAN})


def _scope(job: Job) -> WorkspaceScope:
    return WorkspaceScope(workspace_id=job.workspace_id, principal_id="system:worker")


def data_job_handlers(container: Container) -> dict[str, Handler]:
    ingestion = IngestionService(
        container.uow,
        container.storage,
        container.ids,
        IngestionSettings.for_upload_limit(container.config.storage.max_upload_bytes),
    )
    profiling = ProfilingService(container.uow, container.storage, container.ids)
    understanding = UnderstandingPipeline(container.uow, container.storage, container.ids)
    connectors = ConnectorService.from_container(container)

    def ingest(job: Job, ctx: JobContext) -> str | None:
        if "connector_id" in job.payload:  # TASK-DATA-013: snapshot -> upload path -> ingest
            return connectors.run_import(_scope(job), str(job.payload["connector_id"]), job.id)
        scope, version_id = _scope(job), str(job.payload["version_id"])
        try:
            version = ingestion.ingest(
                scope,
                version_id,
                filename=job.payload.get("filename"),
                sheet=job.payload.get("sheet"),
                on_batch=lambda _: ctx.raise_if_cancelled(),
            )
        except JobCancelled:
            cancelled = reject(DataError, "CANCELLED", "Ingestion was cancelled.")
            ingestion.fail(
                scope, version_id, DatasetState.INGESTING, DatasetState.INGESTION_FAILED, cancelled
            )
            raise
        return version.id

    def process_dataset(job: Job, ctx: JobContext) -> str | None:
        """Advance the version one step group: profile it, or understand it (idempotent)."""
        ctx.raise_if_cancelled()
        scope, version_id = _scope(job), str(job.payload["version_id"])
        if understanding.state(scope, version_id) is DatasetState.INGESTED:
            return profiling.profile(scope, version_id).dataset_version_id
        return understanding.run(scope, version_id, checkpoint=ctx.raise_if_cancelled).id

    return {
        JobType.INGEST: ingest,
        JobType.PROCESS_DATASET: process_dataset,
        JobType.RUN_PLAN: run_plan_handler(container),
    }


S = DatasetState
# In-progress state -> the failed state a dead job leaves it in (GAPS-5a).
DEAD_JOB_FAILED_STATE = {
    S.UPLOADED: S.INGESTION_FAILED,
    S.VALIDATING: S.VALIDATION_FAILED,
    S.INGESTING: S.INGESTION_FAILED,
    S.INGESTED: S.ANALYSIS_FAILED,
    S.PROFILED: S.ANALYSIS_FAILED,
    S.SCHEMA_ANALYZED: S.ANALYSIS_FAILED,
    S.DOMAIN_IDENTIFIED: S.ANALYSIS_FAILED,
    S.SEMANTICALLY_MAPPED: S.ANALYSIS_FAILED,
    S.ANALYZING: S.ANALYSIS_FAILED,
}
DEAD_JOB_FAILURE = DatasetFailure(
    code="JOB_FAILED", message="Processing failed repeatedly and was stopped. Upload it again."
)
log = logging.getLogger("app.data.jobs")


def fail_dead_job_version(uow: UnitOfWork) -> DeadJobHook:
    """Worker hook: a dataset job with no retry left fails its version if the version is still
    in an in-progress state (CAS on that state: a version that moved on is left alone)."""

    def on_dead(job: Job, error: JobError) -> None:
        version_id = job.payload.get("version_id")
        if job.type not in DATA_JOBS or not isinstance(version_id, str):
            return
        scope = _scope(job)
        with uow(scope) as s:
            repo = DatasetRepository(s)
            version = repo.get_version(scope, version_id)
            target = DEAD_JOB_FAILED_STATE.get(version.state) if version else None
            if version is None or target is None:
                return
            try:
                repo.transition(scope, version_id, version.state, target, failure=DEAD_JOB_FAILURE)
            except StateConflictError:  # moved on meanwhile: nothing is stuck
                return
        log.warning("version %s failed after job %s died (%s)", version_id, job.id, error.code)

    return on_dead


def _ids(event: OutboxEvent) -> dict[str, str]:
    return {"dataset_id": event.payload["dataset_id"], "version_id": event.payload["version_id"]}


def _understand(event: OutboxEvent) -> JobRequest:
    return JobRequest(JobType.PROCESS_DATASET, _ids(event))


def _replan(event: OutboxEvent) -> JobRequest:
    version = int(event.payload["semantic_model_version"])
    return JobRequest(JobType.RUN_PLAN, {**_ids(event), "semantic_model_version": version})


def data_event_consumers() -> dict[str, Consumer]:
    return {"DatasetProfiled": _understand, "SemanticModelChanged": _replan}
