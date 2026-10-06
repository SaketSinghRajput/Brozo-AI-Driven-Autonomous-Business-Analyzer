"""Job worker (TASK-JOB-002, design §52-53).

Handler contract — every handler MUST be idempotent: a job can run more than once (a retry
after a retryable error, or a re-run after the reaper releases a job whose worker crashed).
Handlers write through natural keys / CAS transitions so a repeat run is a no-op, call
`ctx.raise_if_cancelled()` between steps, and may report `ctx.progress` (0..1). Raise an
`ApplicationError` to control retry (`retryable`); any other exception is retried until
`max_attempts` is exhausted.

Run: `python -m app.jobs.worker`.
"""

import logging
import socket
import threading
import uuid
from collections import Counter
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import timedelta

from app.core.errors import ApplicationError, StateConflictError
from app.core.logging import trace_id_var
from app.core.observability import JOBS, span
from app.core.scope import SystemScope
from app.infra.db import UnitOfWork
from app.jobs.models import Job, JobError, JobStatus
from app.jobs.repository import JobRepository

log = logging.getLogger("app.jobs")
SYSTEM = SystemScope("system:worker")


class JobCancelled(Exception):  # control-flow signal, not an error
    pass


@dataclass
class JobContext:
    job: Job
    progress: float | None = None
    _cancel: threading.Event = field(default_factory=threading.Event)

    def raise_if_cancelled(self) -> None:
        if self._cancel.is_set():
            raise JobCancelled


Handler = Callable[[Job, JobContext], str | None]  # returns result_ref
# Called once a job is terminally FAILED (no retry left), so its owner can fail what the job was
# advancing (e.g. a dataset version left mid-pipeline). Must be idempotent.
DeadJobHook = Callable[[Job, JobError], None]


class Worker:
    def __init__(
        self,
        uow: UnitOfWork,
        handlers: Mapping[str, Handler],
        *,
        concurrency: Mapping[str, int] | None = None,
        worker_id: str | None = None,
        heartbeat_s: float = 10.0,
        stale_after: timedelta = timedelta(seconds=60),
        retry_base_s: float = 5.0,
        on_dead: DeadJobHook | None = None,
    ) -> None:
        self._uow = uow
        self._on_dead = on_dead
        self._handlers = dict(handlers)
        self._limits = {t: (concurrency or {}).get(t, 1) for t in self._handlers}
        self.worker_id = worker_id or f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
        self._heartbeat_s = heartbeat_s
        self._stale_after = stale_after
        self._retry_base_s = retry_base_s
        self._active: Counter[str] = Counter()
        self._lock = threading.Lock()

    def reap(self) -> int:
        with self._uow(SYSTEM) as s:
            return JobRepository(s).reap(SYSTEM, self._stale_after)

    def claim(self) -> Job | None:
        with self._lock:
            free = [t for t, limit in self._limits.items() if self._active[t] < limit]
        with self._uow(SYSTEM) as s:
            job = JobRepository(s).claim(SYSTEM, self.worker_id, free)
        if job is not None:
            with self._lock:
                self._active[job.type] += 1
        return job

    def run_once(self) -> bool:
        """Claim and run one job in the calling thread. Returns False when nothing was claimable."""
        job = self.claim()
        if job is None:
            return False
        self._run(job)
        return True

    def run_forever(self, stop: threading.Event, poll_s: float = 1.0) -> None:
        slots = max(1, sum(self._limits.values()))
        with ThreadPoolExecutor(max_workers=slots, thread_name_prefix="job") as pool:
            while not stop.is_set():
                self.reap()
                job = self.claim()
                if job is None:
                    stop.wait(poll_s)
                    continue
                pool.submit(self._run, job)

    def _run(self, job: Job) -> None:
        token = trace_id_var.set(job.trace_id)
        ctx = JobContext(job)
        done = threading.Event()
        beat = threading.Thread(target=self._heartbeat, args=(ctx, done), daemon=True)
        beat.start()
        try:
            # Root span of the job's trace: same trace id as the request that enqueued it.
            with span("job.run", job_type=job.type.value, attempt=job.attempts):
                outcome = self._execute(job, ctx)
            JOBS.labels(job.type.value, outcome).inc()
        except StateConflictError:
            log.warning("job %s lease lost; result discarded", job.id)
        finally:
            done.set()
            beat.join()
            with self._lock:
                self._active[job.type] -= 1
            trace_id_var.reset(token)

    def _execute(self, job: Job, ctx: JobContext) -> str:
        """Run the handler and record the outcome; returns a metrics label."""
        try:
            result_ref = self._handlers[job.type](job, ctx)
        except JobCancelled:
            with self._uow(SYSTEM) as s:
                JobRepository(s).mark_cancelled(SYSTEM, job.id, self.worker_id)
            return "cancelled"
        except ApplicationError as exc:
            self._fail(job, JobError(code=exc.code, message=exc.message, retryable=exc.retryable))
            return "failed"
        except Exception:
            log.exception("job %s handler crashed", job.id)
            self._fail(
                job, JobError(code="SYSTEM_ERROR", message="handler crashed", retryable=True)
            )
            return "crashed"
        with self._uow(SYSTEM) as s:
            JobRepository(s).complete(SYSTEM, job.id, self.worker_id, result_ref)
        return "completed"

    def _fail(self, job: Job, error: JobError) -> None:
        delay = timedelta(seconds=self._retry_base_s * 2 ** (job.attempts - 1))
        with self._uow(SYSTEM) as s:
            status = JobRepository(s).fail(SYSTEM, job.id, self.worker_id, error, delay)
        if status is JobStatus.FAILED and self._on_dead is not None:
            try:
                self._on_dead(job, error)
            except Exception:  # the job row is final either way; the hook's failure is logged
                log.exception("dead-job hook failed for job %s", job.id)

    def _heartbeat(self, ctx: JobContext, done: threading.Event) -> None:
        while not done.wait(self._heartbeat_s):
            try:
                with self._uow(SYSTEM) as s:
                    cancel = JobRepository(s).heartbeat(
                        SYSTEM, ctx.job.id, self.worker_id, ctx.progress
                    )
            except StateConflictError:
                cancel = True  # lease lost: stop the handler at its next checkpoint
            if cancel:
                ctx._cancel.set()


# Handler registry: job type → handler. Feature modules register here as they land (P1).
HANDLERS: dict[str, Handler] = {}


def main() -> None:  # pragma: no cover - process entry point
    import signal

    from app.core.config import load_config
    from app.core.container import build_container
    from app.core.logging import configure_logging
    from app.core.observability import configure_tracing
    from app.data.jobs import data_event_consumers, data_job_handlers, fail_dead_job_version
    from app.jobs.outbox import OutboxDispatcher

    config = load_config()
    configure_logging(config.observability.log_level)
    configure_tracing(config.observability)
    if config.observability.worker_metrics_port:
        from prometheus_client import start_http_server

        start_http_server(config.observability.worker_metrics_port)  # internal network only
    container = build_container(config)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    from app.jobs.models import JobType
    from app.security.audit import make_verify_handler  # imports this module: keep it local

    # Handlers that need adapters are built here, at the composition root.
    handlers = {
        **HANDLERS,
        JobType.AUDIT_VERIFY.value: make_verify_handler(container.uow, container.auth.audit_keys),
        **data_job_handlers(container),
    }
    wcfg = config.worker  # design section 65 limits (TASK-MLOPS-008)
    worker = Worker(
        container.uow,
        handlers,
        concurrency=wcfg.concurrency,
        heartbeat_s=wcfg.heartbeat_s,
        stale_after=timedelta(seconds=wcfg.stale_after_s),
        retry_base_s=wcfg.retry_base_s,
        on_dead=fail_dead_job_version(container.uow),
    )
    dispatcher = OutboxDispatcher(container.uow, container.ids, data_event_consumers())
    outbox = threading.Thread(target=dispatcher.run_forever, args=(stop,), name="outbox")
    outbox.start()
    log.info("worker %s started; handlers=%s", worker.worker_id, sorted(handlers))
    worker.run_forever(stop, poll_s=wcfg.poll_s)
    outbox.join()


if __name__ == "__main__":
    main()
