"""Helpers for integration tests that need real sessions (TASK-SEC-004/005)."""

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.core.config import AppConfig, Environment
from app.core.container import Container
from app.main import create_app
from app.security.auth import CSRF_HEADER
from app.security.bootstrap import BOOTSTRAP
from app.security.repository import AuthRepository
from app.tools.permissions import Role

from .conftest import TestDatabase

PASSWORD = "correct horse battery staple"


def make_app(database: TestDatabase, tmp_path: Path, **security: Any) -> FastAPI:
    config = AppConfig(
        _env_file=None,
        env=Environment.TEST,
        database={"url": database.owner_url},
        storage={"datastore_path": tmp_path / "datastore"},
        security={"session_secret": "t" * 32, **security},
    )
    app = create_app(config)
    # Run as the least-privilege app role, as in compose: grants and RLS policies apply (the
    # owner/superuser would bypass RLS). The engine is lazy, so every connection gets the role.
    event.listen(app.state.container.engine, "connect", _as_app_role)
    return app


def _as_app_role(dbapi_conn: Any, _: Any) -> None:
    dbapi_conn.autocommit = True
    dbapi_conn.execute("SET ROLE analyzer_app")
    dbapi_conn.autocommit = False


def container_of(client: TestClient) -> Container:
    container: Container = client.app.state.container  # type: ignore[attr-defined]
    return container


def add_workspace(container: Container) -> str:
    workspace_id = container.ids.new("ws")
    with container.uow(BOOTSTRAP) as s:
        AuthRepository(s).create_workspace(BOOTSTRAP, workspace_id, "test")
    return workspace_id


def add_user(container: Container, workspace_id: str, role: Role, *, email: str = "") -> str:
    """Creates a user (unique email unless given) with one membership; returns the email."""
    user_id = container.ids.new("usr")
    email = email or f"{user_id}@example.test"
    with container.uow(BOOTSTRAP) as s:
        repo = AuthRepository(s)
        repo.create_user(BOOTSTRAP, user_id, email, container.auth.hasher.hash(PASSWORD))
        repo.add_membership(BOOTSTRAP, user_id, workspace_id, role)
    return email


def login(client: TestClient, email: str, password: str = PASSWORD, **extra: Any) -> str:
    """Logs in (cookie kept by the client) and returns the CSRF token."""
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password, **extra})
    assert r.status_code == 200, r.text
    csrf: str = r.json()["csrf_token"]
    return csrf


def csrf_headers(csrf: str) -> dict[str, str]:
    return {CSRF_HEADER: csrf}
