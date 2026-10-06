"""PostgreSQL job queue (TASK-JOB-001, ADR-0010).

Claiming uses `FOR UPDATE SKIP LOCKED`, so concurrent workers never claim the same row. Every
worker-side write is a compare-and-set on `(status = RUNNING, locked_by = worker)`: a worker
whose lease was reaped cannot complete or fail a job it no longer owns (`StateConflictError`).

RETRYING is the visible "waiting for backoff" state; it is claimable again once `run_after`
passes (design §53 writes this as RETRYING → QUEUED; one fewer write, same semantics).
"""

import json
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from sqlalchemy import Row, text

from app.core.errors import NotFoundError, StateConflictError
from app.core.scope import SystemScope, WorkspaceScope
from app.infra.repository import Repository
from app.jobs.models import Job, JobError, JobStatus, JobType

# Module constants only: the f-string SQL below interpolates these, never caller input.
_RUNNING_OWNED = "id = :id AND status = 'RUNNING' AND locked_by = :worker"


def _job(row: Row[Any]) -> Job:
    return Job.model_validate(dict(row._mapping))


class JobRepository(Repository):
    def enqueue(
        self,
        scope: WorkspaceScope,
        job_id: str,
        job_type: JobType,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        max_attempts: int = 3,
        trace_id: str | None = None,
    ) -> Job:
        """Insert a job; replaying the same idempotency key returns the original job."""
        row = self._session.execute(
            text(
                "INSERT INTO jobs (id, workspace_id, type, payload, max_attempts, "
                "idempotency_key, trace_id) VALUES (:id, :ws, :type, CAST(:payload AS jsonb), "
                ":max_attempts, :key, :trace) "
                "ON CONFLICT (workspace_id, idempotency_key) DO NOTHING RETURNING *"
            ),
            {
                "id": job_id,
                "ws": scope.workspace_id,
                "type": job_type.value,
                "payload": json.dumps(payload),
                "max_attempts": max_attempts,
                "key": idempotency_key,
                "trace": trace_id,
            },
        ).one_or_none()
        if row is not None:
            return _job(row)
        existing = self._session.execute(
            text("SELECT * FROM jobs WHERE workspace_id = :ws AND idempotency_key = :key"),
            {"ws": scope.workspace_id, "key": idempotency_key},
        ).one()
        job = _job(existing)
        if job.type != job_type:
            raise StateConflictError("idempotency key already used for a different job type")
        return job

    def get(self, scope: WorkspaceScope, job_id: str) -> Job | None:
        row = self._session.execute(
            text("SELECT * FROM jobs WHERE id = :id AND workspace_id = :ws"),
            {"id": job_id, "ws": scope.workspace_id},
        ).one_or_none()
        return None if row is None else _job(row)

    def request_cancel(self, scope: WorkspaceScope, job_id: str) -> Job:
        """Waiting jobs are cancelled at once; running jobs get a cooperative cancel flag."""
        row = self._session.execute(
            text(
                "UPDATE jobs SET cancel_requested = true, updated_at = now(), "
                "status = CASE WHEN status IN ('QUEUED', 'RETRYING') THEN 'CANCELLED' "
                "ELSE status END "
                "WHERE id = :id AND workspace_id = :ws AND status IN "
                "('QUEUED', 'RETRYING', 'RUNNING') RETURNING *"
            ),
            {"id": job_id, "ws": scope.workspace_id},
        ).one_or_none()
        if row is not None:
            return _job(row)
        job = self.get(scope, job_id)
        if job is None:
            raise NotFoundError("job not found")
        raise StateConflictError(f"job is already {job.status}")

    def claim(self, scope: SystemScope, worker_id: str, types: Sequence[str]) -> Job | None:
        if not types:
            return None
        row = self._session.execute(
            text(
                "UPDATE jobs SET status = 'RUNNING', attempts = attempts + 1, "
                "locked_by = :worker, locked_at = now(), heartbeat_at = now(), "
                "updated_at = now() "
                "WHERE id = (SELECT id FROM jobs WHERE status IN ('QUEUED', 'RETRYING') "
                "AND run_after <= now() AND type = ANY(:types) "
                "ORDER BY run_after, id LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING *"
            ),
            {"worker": worker_id, "types": list(types)},
        ).one_or_none()
        return None if row is None else _job(row)

    def heartbeat(
        self, scope: SystemScope, job_id: str, worker_id: str, progress: float | None = None
    ) -> bool:
        """Extend the lease; returns True when cancellation was requested."""
        cancel = self._session.execute(
            text(
                "UPDATE jobs SET heartbeat_at = clock_timestamp(), updated_at = now(), "  # noqa: S608
                f"progress = COALESCE(:progress, progress) WHERE {_RUNNING_OWNED} "
                "RETURNING cancel_requested"
            ),
            {"id": job_id, "worker": worker_id, "progress": progress},
        ).scalar_one_or_none()
        if cancel is None:
            raise StateConflictError("job lease lost")
        return bool(cancel)

    def complete(
        self, scope: SystemScope, job_id: str, worker_id: str, result_ref: str | None
    ) -> None:
        self._finish(
            "status = 'COMPLETED', progress = 1, result_ref = :result",
            {"id": job_id, "worker": worker_id, "result": result_ref},
        )

    def mark_cancelled(self, scope: SystemScope, job_id: str, worker_id: str) -> None:
        self._finish("status = 'CANCELLED'", {"id": job_id, "worker": worker_id})

    def fail(
        self,
        scope: SystemScope,
        job_id: str,
        worker_id: str,
        error: JobError,
        retry_delay: timedelta,
    ) -> JobStatus:
        """Retryable errors with attempts left → RETRYING after `retry_delay`; else FAILED."""
        retry = "(:retryable AND attempts < max_attempts)"
        status = self._finish(
            f"status = CASE WHEN {retry} THEN 'RETRYING' ELSE 'FAILED' END, "
            f"run_after = CASE WHEN {retry} THEN now() + :delay ELSE run_after END, "
            "error = CAST(:error AS jsonb)",
            {
                "id": job_id,
                "worker": worker_id,
                "retryable": error.retryable,
                "delay": retry_delay,
                "error": error.model_dump_json(),
            },
        )
        return JobStatus(status)

    def reap(self, scope: SystemScope, stale_after: timedelta) -> int:
        """Release RUNNING jobs whose worker stopped heart-beating (crashed or hung)."""
        stale_error = JobError(
            code="JOB_STALE", message="worker stopped responding", retryable=False
        )
        result = self._session.execute(
            text(
                "UPDATE jobs SET locked_by = NULL, heartbeat_at = NULL, updated_at = now(), "
                "status = CASE WHEN cancel_requested THEN 'CANCELLED' "
                "WHEN attempts < max_attempts THEN 'QUEUED' ELSE 'FAILED' END, "
                "error = CASE WHEN NOT cancel_requested AND attempts >= max_attempts "
                "THEN CAST(:error AS jsonb) ELSE error END "
                "WHERE status = 'RUNNING' AND heartbeat_at < now() - :stale"
            ),
            {"stale": stale_after, "error": stale_error.model_dump_json()},
        )
        return int(result.rowcount)  # type: ignore[attr-defined]

    def _finish(self, assignments: str, params: dict[str, Any]) -> str:
        status = self._session.execute(
            text(
                f"UPDATE jobs SET {assignments}, locked_by = NULL, heartbeat_at = NULL, "  # noqa: S608
                f"updated_at = now() WHERE {_RUNNING_OWNED} RETURNING status"
            ),
            params,
        ).scalar_one_or_none()
        if status is None:
            raise StateConflictError("job lease lost")
        return str(status)
