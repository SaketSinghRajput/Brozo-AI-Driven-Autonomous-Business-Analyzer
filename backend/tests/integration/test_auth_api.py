"""TASK-SEC-004/005/007/013: login, logout, /me, session lifetime, fixation, log redaction."""

import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.clock import FrozenClock
from app.core.errors import AuthenticationError
from app.core.logging import JsonFormatter
from app.core.scope import SystemScope
from app.security.auth import AuthService
from app.tools.permissions import Role

from .auth_helpers import (
    PASSWORD,
    add_user,
    add_workspace,
    container_of,
    csrf_headers,
    login,
    make_app,
)
from .conftest import TestDatabase

BASE = "https://testserver"
COOKIE = "__Host-session"
JAR_DOMAIN = "testserver.local"  # httpx stores host-only cookies for dotless hosts here
TEST = SystemScope("system:test")


@pytest.fixture
def api(database: TestDatabase, tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(
        make_app(database, tmp_path), base_url=BASE, raise_server_exceptions=False
    ) as client:
        yield client


@pytest.fixture
def workspace(api: TestClient) -> str:
    return add_workspace(container_of(api))


@pytest.fixture
def admin(api: TestClient, workspace: str) -> str:
    return add_user(container_of(api), workspace, Role.ADMIN)


def test_login_sets_hardened_cookie_and_returns_csrf(api: TestClient, admin: str) -> None:
    r = api.post("/api/v1/auth/login", json={"email": admin, "password": PASSWORD})
    assert r.status_code == 200
    assert len(r.json()["csrf_token"]) == 64
    attributes = [a.strip().lower() for a in r.headers["set-cookie"].split(";")]
    assert attributes[0].startswith(COOKIE.lower() + "=")
    assert {"httponly", "secure", "samesite=lax", "path=/"} <= set(attributes)
    assert not any(a.startswith("domain=") for a in attributes)


def test_me_returns_user_workspace_role(api: TestClient, admin: str, workspace: str) -> None:
    csrf = login(api, admin)
    r = api.get("/api/v1/auth/me")
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == admin
    assert body["workspace_id"] == workspace
    assert body["role"] == "admin"
    assert body["csrf_token"] == csrf  # re-issued for SPA reloads


def test_me_without_session_is_401(api: TestClient) -> None:
    r = api.get("/api/v1/auth/me")
    assert r.status_code == 401
    assert r.json()["error_code"] == "AUTH_ERROR"
    api.cookies.set(COOKIE, "forged-token", domain=JAR_DOMAIN)
    assert api.get("/api/v1/auth/me").status_code == 401


def test_unknown_user_and_wrong_password_are_indistinguishable(api: TestClient, admin: str) -> None:
    wrong = api.post("/api/v1/auth/login", json={"email": admin, "password": "not it at all"})
    unknown = api.post(
        "/api/v1/auth/login", json={"email": "nobody@example.test", "password": PASSWORD}
    )
    assert wrong.status_code == unknown.status_code == 401
    keys = ("error_code", "message", "details", "retryable")
    assert [wrong.json()[k] for k in keys] == [unknown.json()[k] for k in keys]
    assert "set-cookie" not in wrong.headers


def test_disabled_user_gets_the_same_401(api: TestClient, admin: str) -> None:
    with container_of(api).uow(TEST) as s:
        s.execute(text("UPDATE users SET status = 'disabled' WHERE email = :e"), {"e": admin})
    r = api.post("/api/v1/auth/login", json={"email": admin, "password": PASSWORD})
    assert r.status_code == 401
    assert r.json()["message"] == "invalid email or password"


def test_email_match_is_case_insensitive(api: TestClient, admin: str) -> None:
    login(api, admin.upper())


def test_logout_needs_csrf_and_revokes_server_side(api: TestClient, admin: str) -> None:
    csrf = login(api, admin)
    token = api.cookies.get(COOKIE)
    assert token
    assert api.post("/api/v1/auth/logout").status_code == 403  # no CSRF header
    r = api.post("/api/v1/auth/logout", headers=csrf_headers(csrf))
    assert r.status_code == 204
    # Replaying the old cookie value must not work: the session is revoked in the database.
    api.cookies.set(COOKIE, token, domain=JAR_DOMAIN)
    assert api.get("/api/v1/auth/me").status_code == 401


def test_login_issues_new_session_and_revokes_presented_one(api: TestClient, admin: str) -> None:
    # Fixation: an attacker-chosen cookie value is never adopted.
    api.cookies.set(COOKIE, "attacker-chosen-value", domain=JAR_DOMAIN)
    login(api, admin)
    first = api.cookies.get(COOKIE)
    assert first
    assert first != "attacker-chosen-value"
    login(api, admin)  # re-login while holding `first`
    second = api.cookies.get(COOKIE)
    assert second
    assert second != first
    api.cookies.set(COOKIE, first, domain=JAR_DOMAIN)
    assert api.get("/api/v1/auth/me").status_code == 401


def test_login_picks_only_a_workspace_the_user_belongs_to(api: TestClient, admin: str) -> None:
    other = add_workspace(container_of(api))
    r = api.post(
        "/api/v1/auth/login",
        json={"email": admin, "password": PASSWORD, "workspace_id": other},
    )
    assert r.status_code == 403
    assert "set-cookie" not in r.headers
    # With a second membership the user can choose it explicitly.
    with container_of(api).uow(TEST) as s:
        s.execute(
            text(
                "INSERT INTO memberships (user_id, workspace_id, role) "
                "SELECT id, :ws, 'viewer' FROM users WHERE email = :e"
            ),
            {"ws": other, "e": admin},
        )
    login(api, admin, workspace_id=other)
    me = api.get("/api/v1/auth/me").json()
    assert (me["workspace_id"], me["role"]) == (other, "viewer")


def test_removed_membership_ends_the_session(api: TestClient, admin: str, workspace: str) -> None:
    login(api, admin)
    with container_of(api).uow(TEST) as s:
        s.execute(text("DELETE FROM memberships WHERE workspace_id = :ws"), {"ws": workspace})
    assert api.get("/api/v1/auth/me").status_code == 401


def test_unknown_login_fields_are_rejected(api: TestClient, admin: str) -> None:
    r = api.post("/api/v1/auth/login", json={"email": admin, "password": PASSWORD, "role": "admin"})
    assert r.status_code == 422


def test_login_requires_json_content_type(api: TestClient, admin: str) -> None:
    # A cross-site HTML form cannot send application/json (login CSRF).
    body = f'{{"email": "{admin}", "password": "{PASSWORD}"}}'
    r = api.post("/api/v1/auth/login", content=body, headers={"content-type": "text/plain"})
    assert r.status_code == 422


def _service(api: TestClient, clock: FrozenClock, **cfg: int) -> AuthService:
    container = container_of(api)
    security = container.config.security.model_copy(update=cfg)
    return AuthService(container.uow, clock, container.ids, security)


def test_idle_timeout(api: TestClient, admin: str) -> None:
    clock = FrozenClock(datetime(2030, 1, 1, tzinfo=UTC))
    auth = _service(api, clock, session_idle_s=60, session_ttl_s=3600)
    token = auth.login(admin, PASSWORD).token
    clock.advance(timedelta(seconds=59))
    auth.authenticate(token)  # activity resets the idle clock
    clock.advance(timedelta(seconds=59))
    auth.authenticate(token)
    clock.advance(timedelta(seconds=61))
    with pytest.raises(AuthenticationError):
        auth.authenticate(token)


def test_absolute_expiry_despite_activity(api: TestClient, admin: str) -> None:
    clock = FrozenClock(datetime(2030, 1, 1, tzinfo=UTC))
    auth = _service(api, clock, session_idle_s=60, session_ttl_s=120)
    token = auth.login(admin, PASSWORD).token
    for _ in range(2):
        clock.advance(timedelta(seconds=50))
        auth.authenticate(token)
    clock.advance(timedelta(seconds=21))  # 121 s after login
    with pytest.raises(AuthenticationError):
        auth.authenticate(token)


def test_logs_carry_no_secrets_or_pii(
    api: TestClient, admin: str, caplog: pytest.LogCaptureFixture
) -> None:
    """TASK-SEC-013: a full auth flow, including failures, never logs credentials,
    session or CSRF tokens, or the email address (not even from a query string)."""
    caplog.set_level(logging.DEBUG)
    api.post("/api/v1/auth/login", json={"email": admin, "password": "wrong password!!"})
    api.post("/api/v1/auth/login", json={"email": admin, "password": PASSWORD, "x": 1})
    csrf = login(api, admin)
    token = api.cookies.get(COOKIE)
    assert token
    api.get("/api/v1/auth/me", params={"email": admin})
    api.post("/api/v1/auth/logout", headers=csrf_headers(csrf))
    formatter = JsonFormatter()
    output = "\n".join(formatter.format(r) for r in caplog.records)
    assert "http.request" in output  # the request log did run
    for secret in (PASSWORD, "wrong password!!", token, csrf, admin):
        assert secret not in output
