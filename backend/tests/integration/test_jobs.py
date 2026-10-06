"""TASK-JOB-001/002: PostgreSQL queue semantics against a real database (as `analyzer_app`)."""

import threading
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text

from app.core.errors import DataError, StateConflictError
from app.core.logging import trace_id_var
from app.core.scope import SystemScope, WorkspaceScope
from app.infra.db import UnitOfWork
from app.jobs.models import Job, JobError, JobStatus, JobType
from app.jobs.repository import JobRepository
from app.jobs.worker import JobContext, Worker

from .conftest import TestDatabase

SYSTEM = SystemScope("system:test")


@pytest.fixture
def uow(database: TestDatabase, app_engine: Engine) -> UnitOfWork:
    owner = create_engine(database.owner_url)
    with owner.begin() as conn:  # the app role has no DELETE on jobs; clean up as owner
        conn.execute(text("DELETE FROM jobs"))  # claim() is global: start from an empty queue
    owner.dispose()
    return UnitOfWork(app_engine)


@pytest.fixture
def scope(uow: UnitOfWork) -> WorkspaceScope:
    ws = f"ws_{uuid.uuid4().hex[:12]}"
    with uow(SYSTEM) as s:
        s.execute(text("INSERT INTO workspaces (id, name) VALUES (:id, 'jobs')"), {"id": ws})
    return WorkspaceScope(workspace_id=ws, principal_id="usr_test")


def _enqueue(
    uow: UnitOfWork,
    scope: WorkspaceScope,
    job_type: JobType = JobType.INGEST,
    **kw: Any,
) -> Job:
    with uow(scope) as s:
        return JobRepository(s).enqueue(scope, f"job_{uuid.uuid4().hex}", job_type, {}, **kw)


def _get(uow: UnitOfWork, scope: WorkspaceScope, job_id: str) -> Job:
    with uow(scope) as s:
        job = JobRepository(s).get(scope, job_id)
    assert job is not None
    return job


def _worker(uow: UnitOfWork, handler: Callable[[Job, JobContext], str | None], **kw: Any) -> Worker:
    return Worker(uow, {JobType.INGEST: handler}, retry_base_s=0, **kw)


def test_idempotency_key_replay_returns_same_job(uow: UnitOfWork, scope: WorkspaceScope) -> None:
    first = _enqueue(uow, scope, idempotency_key="upload-1")
    again = _enqueue(uow, scope, idempotency_key="upload-1")
    assert again.id == first.id
    with pytest.raises(StateConflictError):
        _enqueue(uow, scope, JobType.EVALUATE, idempotency_key="upload-1")


def test_get_is_workspace_scoped(uow: UnitOfWork, scope: WorkspaceScope) -> None:
    job = _enqueue(uow, scope)
    other = WorkspaceScope(workspace_id="ws_ffffff", principal_id="usr_x")
    with uow(other) as s:
        assert JobRepository(s).get(other, job.id) is None


def test_concurrent_workers_never_claim_the_same_job(
    uow: UnitOfWork, scope: WorkspaceScope
) -> None:
    ids = {_enqueue(uow, scope).id for _ in range(30)}
    claimed: list[str] = []
    lock = threading.Lock()

    def drain(worker_id: str) -> None:
        while True:
            with uow(SYSTEM) as s:
                job = JobRepository(s).claim(SYSTEM, worker_id, [JobType.INGEST])
            if job is None:
                return
            with lock:
                claimed.append(job.id)

    threads = [threading.Thread(target=drain, args=(f"w{i}",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(claimed) == len(set(claimed)) == len(ids)
    assert set(claimed) == ids


def test_worker_completes_job_and_propagates_trace_id(
    uow: UnitOfWork, scope: WorkspaceScope
) -> None:
    job = _enqueue(uow, scope, trace_id="trace-abc12345")
    seen: list[str | None] = []

    def handler(j: Job, ctx: JobContext) -> str:
        seen.append(trace_id_var.get())
        return "result://1"

    assert _worker(uow, handler).run_once()
    done = _get(uow, scope, job.id)
    assert (done.status, done.result_ref, done.progress) == (JobStatus.COMPLETED, "result://1", 1)
    assert seen == ["trace-abc12345"]


def test_retryable_error_retries_until_attempts_exhausted(
    uow: UnitOfWork, scope: WorkspaceScope
) -> None:
    job = _enqueue(uow, scope, max_attempts=2)

    def flaky(j: Job, ctx: JobContext) -> None:
        raise RuntimeError("transient")

    worker = _worker(uow, flaky)
    assert worker.run_once()
    assert _get(uow, scope, job.id).status == JobStatus.RETRYING
    assert worker.run_once()  # zero backoff: claimable again
    failed = _get(uow, scope, job.id)
    assert (failed.status, failed.attempts) == (JobStatus.FAILED, 2)
    assert failed.error == JobError(code="SYSTEM_ERROR", message="handler crashed", retryable=True)


def test_non_retryable_error_fails_immediately(uow: UnitOfWork, scope: WorkspaceScope) -> None:
    job = _enqueue(uow, scope)

    def bad_file(j: Job, ctx: JobContext) -> None:
        raise DataError("file is not a CSV")

    _worker(uow, bad_file).run_once()
    failed = _get(uow, scope, job.id)
    assert (failed.status, failed.attempts) == (JobStatus.FAILED, 1)
    assert failed.error is not None
    assert failed.error.code == "DATA_ERROR"


def test_cancel_queued_job(uow: UnitOfWork, scope: WorkspaceScope) -> None:
    job = _enqueue(uow, scope)
    with uow(scope) as s:
        assert JobRepository(s).request_cancel(scope, job.id).status == JobStatus.CANCELLED
    with uow(scope) as s, pytest.raises(StateConflictError):
        JobRepository(s).request_cancel(scope, job.id)


def test_running_job_cancels_cooperatively(uow: UnitOfWork, scope: WorkspaceScope) -> None:
    job = _enqueue(uow, scope)
    started = threading.Event()
    tick = threading.Event()

    def long_task(j: Job, ctx: JobContext) -> None:
        started.set()
        for _ in range(500):
            ctx.raise_if_cancelled()
            tick.wait(0.01)
        raise AssertionError("cancel was never observed")

    worker = _worker(uow, long_task, heartbeat_s=0.05)
    runner = threading.Thread(target=worker.run_once)
    runner.start()
    assert started.wait(5)
    with uow(scope) as s:
        assert JobRepository(s).request_cancel(scope, job.id).cancel_requested
    runner.join(10)
    assert _get(uow, scope, job.id).status == JobStatus.CANCELLED


def test_crashed_worker_job_is_requeued_and_old_owner_cannot_finish(
    uow: UnitOfWork, scope: WorkspaceScope
) -> None:
    job = _enqueue(uow, scope)
    with uow(SYSTEM) as s:
        claimed = JobRepository(s).claim(SYSTEM, "crashed-worker", [JobType.INGEST])
    assert claimed is not None
    assert claimed.id == job.id
    # The worker "crashes" (no heartbeat); the reaper releases the stale lease.
    with uow(SYSTEM) as s:
        assert JobRepository(s).reap(SYSTEM, timedelta(0)) == 1
    assert _get(uow, scope, job.id).status == JobStatus.QUEUED
    with uow(SYSTEM) as s, pytest.raises(StateConflictError, match="lease lost"):
        JobRepository(s).complete(SYSTEM, job.id, "crashed-worker", None)
    # A healthy worker picks it up again and finishes it.
    assert _worker(uow, lambda j, ctx: None).run_once()
    rerun = _get(uow, scope, job.id)
    assert (rerun.status, rerun.attempts) == (JobStatus.COMPLETED, 2)


def test_stale_job_with_no_attempts_left_fails(uow: UnitOfWork, scope: WorkspaceScope) -> None:
    job = _enqueue(uow, scope, max_attempts=1)
    with uow(SYSTEM) as s:
        JobRepository(s).claim(SYSTEM, "crashed-worker", [JobType.INGEST])
    with uow(SYSTEM) as s:
        JobRepository(s).reap(SYSTEM, timedelta(0))
    failed = _get(uow, scope, job.id)
    assert failed.status == JobStatus.FAILED
    assert failed.error is not None
    assert failed.error.code == "JOB_STALE"


def test_per_type_concurrency_limit(uow: UnitOfWork, scope: WorkspaceScope) -> None:
    _enqueue(uow, scope)
    _enqueue(uow, scope)
    worker = _worker(uow, lambda j, ctx: None, concurrency={JobType.INGEST: 1})
    assert worker.claim() is not None
    assert worker.claim() is None  # the only INGEST slot is busy


def test_app_role_cannot_update_or_delete_evidence(uow: UnitOfWork, scope: WorkspaceScope) -> None:
    """TASK-DB-005: evidence is append-only at the privilege level."""
    from sqlalchemy.exc import ProgrammingError

    with uow(scope) as s:
        s.execute(
            text(
                "INSERT INTO evidence (id, workspace_id, kind, operation, payload, result_hash) "
                "VALUES ('ev_1', :ws, 'metric', 'sum', '{}', 'sha256:00')"
            ),
            {"ws": scope.workspace_id},
        )
    for stmt in ("UPDATE evidence SET kind = 'x'", "DELETE FROM evidence"):
        with pytest.raises(ProgrammingError, match="permission denied"), uow(scope) as s:
            s.execute(text(stmt))


def test_dataset_version_uniques(uow: UnitOfWork, scope: WorkspaceScope) -> None:
    """TASK-DB-003: (dataset_id, version_no) and (dataset_id, content_hash) are unique."""
    from sqlalchemy.exc import IntegrityError

    ws = scope.workspace_id
    insert = text(
        "INSERT INTO dataset_versions (id, workspace_id, dataset_id, version_no, source_sha256, "
        "content_hash) VALUES (:id, :ws, 'ds_1', :no, :src, :hash)"
    )
    with uow(scope) as s:
        s.execute(
            text(
                "INSERT INTO datasets (id, workspace_id, name, source_type) "
                "VALUES ('ds_1', :ws, 'orders', 'csv')"
            ),
            {"ws": ws},
        )
        s.execute(insert, {"id": "dsv_1", "ws": ws, "no": 1, "src": "a", "hash": "h1"})
    for dup in (
        {"id": "dsv_2", "ws": ws, "no": 1, "src": "b", "hash": "h2"},  # same version_no
        {"id": "dsv_3", "ws": ws, "no": 2, "src": "c", "hash": "h1"},  # same content_hash
        {"id": "dsv_4", "ws": ws, "no": 3, "src": "a", "hash": "h3"},  # same source_sha256
    ):
        with pytest.raises(IntegrityError), uow(scope) as s:
            s.execute(insert, dup)
