"""Transactional-outbox dispatcher (TASK-JOB-004; ADR-0010, design sections 52-53, 74).

Rule: a producer that already knows the next job enqueues it in its own transaction (upload ->
`ingest`, ingest -> `process_dataset`). Every other follow-up is event driven: the dispatcher turns
an outbox event into at most one job, through the consumer registered for its event type.
The job's idempotency key is `evt:<event id>`, so dispatching an event again (a crash between
enqueue and marking, or two dispatchers) never creates a second job. Events without a consumer are
just marked dispatched. Each event is handled in its own transaction: a failing consumer is
logged and retried on the next pass without blocking the other events.
"""

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from app.core.ids import IdGenerator
from app.core.scope import SystemScope, WorkspaceScope
from app.infra.db import UnitOfWork
from app.infra.repository import Repository
from app.jobs.models import JobType
from app.jobs.repository import JobRepository

log = logging.getLogger("app.jobs.outbox")
SYSTEM = SystemScope("system:outbox")


@dataclass(frozen=True)
class OutboxEvent:
    id: str
    event_type: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class JobRequest:
    type: JobType
    payload: dict[str, Any]


Consumer = Callable[[OutboxEvent], JobRequest | None]


class OutboxRepository(Repository):
    def pending_ids(self, scope: SystemScope, limit: int) -> list[str]:
        return list(
            self._session.execute(
                text(
                    "SELECT id FROM outbox_events WHERE dispatched_at IS NULL "
                    "ORDER BY created_at, id LIMIT :limit"
                ),
                {"limit": limit},
            ).scalars()
        )

    def lock(self, scope: SystemScope, event_id: str) -> OutboxEvent | None:
        """The event, locked for this transaction; None if dispatched or locked elsewhere."""
        row = self._session.execute(
            text(
                "SELECT id, event_type, payload FROM outbox_events WHERE id = :id "
                "AND dispatched_at IS NULL FOR UPDATE SKIP LOCKED"
            ),
            {"id": event_id},
        ).one_or_none()
        return None if row is None else OutboxEvent(row.id, row.event_type, dict(row.payload))

    def mark_dispatched(self, scope: SystemScope, event_id: str) -> None:
        self._session.execute(
            text("UPDATE outbox_events SET dispatched_at = now() WHERE id = :id"),
            {"id": event_id},
        )


class OutboxDispatcher:
    def __init__(
        self,
        uow: UnitOfWork,
        ids: IdGenerator,
        consumers: Mapping[str, Consumer],
        *,
        batch_size: int = 100,
    ) -> None:
        self._uow = uow
        self._ids = ids
        self._consumers = dict(consumers)
        self._batch_size = batch_size

    def dispatch_pending(self) -> int:
        """Handle one batch of pending events; returns how many were marked dispatched."""
        with self._uow(SYSTEM) as s:
            ids = OutboxRepository(s).pending_ids(SYSTEM, self._batch_size)
        done = 0
        for event_id in ids:
            try:
                done += self._dispatch(event_id)
            except Exception:
                log.exception("outbox event %s could not be dispatched; will retry", event_id)
        return done

    def _dispatch(self, event_id: str) -> int:
        with self._uow(SYSTEM) as s:
            outbox = OutboxRepository(s)
            event = outbox.lock(SYSTEM, event_id)
            if event is None:
                return 0
            consumer = self._consumers.get(event.event_type)
            request = consumer(event) if consumer else None
            if request is not None:
                scope = WorkspaceScope(str(event.payload["workspace_id"]), SYSTEM.component)
                JobRepository(s).enqueue(
                    scope,
                    self._ids.new("job"),
                    request.type,
                    request.payload,
                    idempotency_key=f"evt:{event.id}",
                )
            outbox.mark_dispatched(SYSTEM, event.id)
        return 1

    def run_forever(self, stop: threading.Event, poll_s: float = 1.0) -> None:
        while not stop.is_set():
            try:
                idle = self.dispatch_pending() == 0
            except Exception:  # e.g. database unavailable: keep the thread alive, back off
                log.exception("outbox dispatch pass failed")
                idle = True
            if idle:
                stop.wait(poll_s)
