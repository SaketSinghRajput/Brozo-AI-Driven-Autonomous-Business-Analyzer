"""Shared dataset-pipeline test environment: API client + worker + outbox dispatcher on PostgreSQL.

The API, the worker and the dispatcher all run as the least-privilege `analyzer_app` role, so
missing grants or RLS policies fail the tests. `env` is exposed to every integration test
through conftest.
"""

import dataclasses
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from app.api.deps import current_principal
from app.core.config import AppConfig, Environment
from app.core.container import Container
from app.core.scope import SystemScope, WorkspaceScope
from app.data.jobs import data_event_consumers, data_job_handlers, fail_dead_job_version
from app.infra.db import UnitOfWork
from app.jobs.outbox import OutboxDispatcher
from app.jobs.worker import Worker
from app.main import create_app
from app.tools.permissions import Role
from tests.principals import principal_for

FIXTURES = Path(__file__).resolve().parents[3] / "evals" / "fixtures"


@dataclasses.dataclass
class Env:
    client: TestClient
    container: Container
    scope: WorkspaceScope
    worker: Worker
    dispatcher: OutboxDispatcher
    role: list[Role]  # mutable: tests switch the session role

    def upload(self, path: Path, key: str | None = None, **form: str) -> Any:
        headers = {"Idempotency-Key": key} if key else {}
        files = {"file": (path.name, path.read_bytes())}
        return self.client.post("/api/v1/datasets", files=files, data=form, headers=headers)

    def drain(self) -> int:
        """Run queued jobs only (no event dispatch)."""
        runs = 0
        while self.worker.run_once():
            runs += 1
        return runs

    def settle(self) -> int:
        """Run jobs and dispatch outbox events until neither has work; returns jobs run."""
        runs = 0
        while True:
            ran = self.drain()
            runs += ran
            if self.dispatcher.dispatch_pending() == 0 and ran == 0:
                return runs

    def get(self, path: str) -> Any:
        return self.client.get(f"/api/v1/datasets{path}")

    def version(self, ids: dict[str, str]) -> dict[str, Any]:
        body: dict[str, Any] = self.get(f"/{ids['dataset_id']}/versions/{ids['version_id']}").json()
        return body

    def job(self, job_id: str) -> dict[str, Any]:
        body: dict[str, Any] = self.client.get(f"/api/v1/jobs/{job_id}").json()
        return body


def new_workspace(uow: UnitOfWork) -> WorkspaceScope:
    ws = f"ws_{uuid.uuid4().hex[:12]}"
    with uow(SystemScope("system:test")) as s:
        s.execute(text("INSERT INTO workspaces (id, name) VALUES (:id, 'data')"), {"id": ws})
    return WorkspaceScope(workspace_id=ws, principal_id="usr_test")


@pytest.fixture
def env(database: Any, app_engine: Engine, tmp_path: Path) -> Iterator[Env]:
    with make_env(database, app_engine, tmp_path) as built:
        yield built


@contextmanager
def make_env(database: Any, app_engine: Engine, tmp_path: Path, **config: Any) -> Iterator[Env]:
    """`config`: extra AppConfig sections, e.g. connectors={...}; security={...} is merged."""
    security = {"session_secret": "t" * 32, **config.pop("security", {})}
    owner = create_engine(database.owner_url)
    with owner.begin() as conn:  # claim() and dispatch are global: start from empty queues
        conn.execute(text("DELETE FROM jobs"))
        conn.execute(text("UPDATE outbox_events SET dispatched_at = now()"))
    owner.dispose()
    app_config = AppConfig(
        _env_file=None,
        env=Environment.TEST,
        database={"url": database.owner_url},
        storage={"datastore_path": tmp_path / "store"},
        security=security,
        **config,
    )
    app = create_app(app_config)
    container = dataclasses.replace(
        app.state.container, engine=app_engine, uow=UnitOfWork(app_engine)
    )
    app.state.container = container
    scope = new_workspace(container.uow)
    role = [Role.ANALYST]
    app.dependency_overrides[current_principal] = lambda: principal_for(scope, role[0])
    worker = Worker(
        container.uow,
        data_job_handlers(container),
        retry_base_s=0,
        on_dead=fail_dead_job_version(container.uow),
    )
    dispatcher = OutboxDispatcher(container.uow, container.ids, data_event_consumers())
    with TestClient(app, raise_server_exceptions=False) as client:
        yield Env(client, container, scope, worker, dispatcher, role)
