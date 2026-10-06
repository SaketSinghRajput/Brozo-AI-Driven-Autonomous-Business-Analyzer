"""TASK-JOB-003: GET /jobs/{id} and POST /jobs/{id}/cancel against a real database, called
with real sessions (TASK-SEC-004/005): RBAC, CSRF and cross-workspace denial."""

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.scope import WorkspaceScope
from app.jobs.models import JobType
from app.jobs.repository import JobRepository
from app.tools.permissions import Role

from .auth_helpers import add_user, add_workspace, container_of, csrf_headers, login, make_app
from .conftest import TestDatabase

BASE = "https://testserver"


@pytest.fixture
def api(database: TestDatabase, tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(
        make_app(database, tmp_path), base_url=BASE, raise_server_exceptions=False
    ) as client:
        yield client


@pytest.fixture
def workspace(api: TestClient) -> str:
    return add_workspace(container_of(api))


def _enqueue(api: TestClient, workspace_id: str) -> str:
    scope = WorkspaceScope(workspace_id=workspace_id, principal_id="usr_test")
    with container_of(api).uow(scope) as s:
        job = JobRepository(s).enqueue(scope, f"job_{uuid.uuid4().hex}", JobType.INGEST, {})
    return job.id


def _session(api: TestClient, workspace_id: str, role: Role) -> str:
    return login(api, add_user(container_of(api), workspace_id, role))


def test_get_job(api: TestClient, workspace: str) -> None:
    _session(api, workspace, Role.VIEWER)
    job_id = _enqueue(api, workspace)
    r = api.get(f"/api/v1/jobs/{job_id}")
    assert r.status_code == 200
    assert r.json()["status"] == "QUEUED"
    assert r.json()["workspace_id"] == workspace


def test_without_session_is_401(api: TestClient, workspace: str) -> None:
    job_id = _enqueue(api, workspace)
    assert api.get(f"/api/v1/jobs/{job_id}").status_code == 401
    assert api.post(f"/api/v1/jobs/{job_id}/cancel").status_code == 401


def test_unknown_or_foreign_job_is_404(api: TestClient, workspace: str) -> None:
    _session(api, workspace, Role.ADMIN)
    r = api.get("/api/v1/jobs/job_does_not_exist")
    assert r.status_code == 404
    assert r.json()["error_code"] == "NOT_FOUND"


def test_job_of_another_workspace_is_invisible(api: TestClient, workspace: str) -> None:
    other = add_workspace(container_of(api))
    foreign_job = _enqueue(api, other)
    csrf = _session(api, workspace, Role.ADMIN)
    assert api.get(f"/api/v1/jobs/{foreign_job}").status_code == 404
    r = api.post(f"/api/v1/jobs/{foreign_job}/cancel", headers=csrf_headers(csrf))
    assert r.status_code == 404
    # The foreign job is untouched.
    scope = WorkspaceScope(workspace_id=other, principal_id="usr_test")
    with container_of(api).uow(scope) as s:
        job = JobRepository(s).get(scope, foreign_job)
    assert job is not None
    assert job.status == "QUEUED"


def test_cancel_then_cancel_again_conflicts(api: TestClient, workspace: str) -> None:
    csrf = _session(api, workspace, Role.ANALYST)
    job_id = _enqueue(api, workspace)
    first = api.post(f"/api/v1/jobs/{job_id}/cancel", headers=csrf_headers(csrf))
    assert first.status_code == 200
    assert first.json()["status"] == "CANCELLED"
    second = api.post(f"/api/v1/jobs/{job_id}/cancel", headers=csrf_headers(csrf))
    assert second.status_code == 409
    assert second.json()["error_code"] == "STATE_CONFLICT"


def test_cancel_needs_csrf_token(api: TestClient, workspace: str) -> None:
    _session(api, workspace, Role.ANALYST)
    job_id = _enqueue(api, workspace)
    for headers in ({}, csrf_headers("0" * 64)):
        r = api.post(f"/api/v1/jobs/{job_id}/cancel", headers=headers)
        assert r.status_code == 403
        assert r.json()["error_code"] == "AUTH_ERROR"


def test_viewer_cannot_cancel(api: TestClient, workspace: str) -> None:
    csrf = _session(api, workspace, Role.VIEWER)
    job_id = _enqueue(api, workspace)
    r = api.post(f"/api/v1/jobs/{job_id}/cancel", headers=csrf_headers(csrf))
    assert r.status_code == 403
    assert r.json()["message"] == "permission denied"
