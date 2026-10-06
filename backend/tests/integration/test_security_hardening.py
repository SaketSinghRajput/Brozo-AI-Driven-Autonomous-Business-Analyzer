"""Security review fixes (SEC-HARDEN): M1 bound parameters never reach exception text;
M2 login throttling and bounded denial auditing."""

import traceback
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.clock import FrozenClock
from app.core.config import DatabaseConfig
from app.core.errors import AuthenticationError, RateLimitedError
from app.core.scope import WorkspaceScope
from app.infra.db import make_engine
from app.security.auth import AuthService
from app.tools.permissions import Role

from .auth_helpers import PASSWORD, add_user, add_workspace, container_of, login, make_app
from .conftest import TestDatabase

EMAIL = "leak-check@example.test"
LOGIN = "/api/v1/auth/login"


def test_db_error_text_does_not_contain_bound_parameters(database: TestDatabase) -> None:
    engine = make_engine(DatabaseConfig(url=database.owner_url))
    try:
        with pytest.raises(DBAPIError) as info, engine.connect() as conn:
            # Fails server-side without PostgreSQL echoing the value in its own message.
            conn.execute(
                text("SELECT 1 / 0 WHERE CAST(:email AS text) IS NOT NULL"), {"email": EMAIL}
            )
    finally:
        engine.dispose()
    logged = "".join(traceback.format_exception(info.value))
    assert "division by zero" in logged
    assert EMAIL not in logged


@pytest.mark.parametrize("table", ["users", "memberships", "sessions", "audit_logs"])
def test_read_only_role_cannot_read_auth_tables(database: TestDatabase, table: str) -> None:
    """L3: analyzer_ro (analytics) must not see password hashes, emails, roles or sessions."""
    engine = make_engine(DatabaseConfig(url=database.owner_url))
    try:
        with engine.connect() as conn:
            allowed = conn.execute(
                text("SELECT has_table_privilege('analyzer_ro', :t, 'SELECT')"), {"t": table}
            ).scalar_one()
    finally:
        engine.dispose()
    assert allowed is False


# --- M2: login throttling --------------------------------------------------------------


@pytest.fixture
def api(database: TestDatabase, tmp_path: Path) -> Iterator[TestClient]:
    app = make_app(
        database,
        tmp_path,
        login_max_failures_per_email=3,
        login_max_failures_per_ip=7,
        denial_audit_max_per_principal=2,
    )
    with TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as c:
        yield c


def _count_verifications(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    hasher = container_of(api).auth.hasher
    calls: list[int] = []
    real = hasher.verify

    def spy(stored: str | None, password: str) -> bool:
        calls.append(1)
        return real(stored, password)

    monkeypatch.setattr(hasher, "verify", spy)
    return calls


def _fail(api: TestClient, email: str, n: int) -> None:
    for _ in range(n):
        r = api.post(LOGIN, json={"email": email, "password": "wrong password!!"})
        assert r.status_code == 401


def test_email_lockout_rejects_before_argon2_with_retry_after(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = container_of(api)
    email = add_user(container, add_workspace(container), Role.ANALYST)
    _fail(api, email, 3)
    calls = _count_verifications(api, monkeypatch)
    r = api.post(LOGIN, json={"email": email.upper(), "password": PASSWORD})  # right password
    assert r.status_code == 429
    assert r.json()["error_code"] == "RATE_LIMITED"
    assert r.json()["retryable"] is True
    assert 0 < int(r.headers["retry-after"]) <= 900
    assert "set-cookie" not in r.headers
    assert calls == []  # no Argon2 work while locked out


def test_unknown_and_known_emails_lock_out_identically(api: TestClient) -> None:
    container = container_of(api)
    known = add_user(container, add_workspace(container), Role.ANALYST)
    unknown = "nobody-here@example.test"
    replies = []
    for email in (known, unknown):
        _fail(api, email, 3)
        r = api.post(LOGIN, json={"email": email, "password": "wrong password!!"})
        replies.append((r.status_code, r.json()["error_code"], r.json()["message"]))
    assert replies[0] == replies[1]
    assert replies[0][0] == 429


def test_ip_limit_spans_emails(api: TestClient) -> None:
    for i in range(7):
        _fail(api, f"spray-{i}@example.test", 1)
    r = api.post(LOGIN, json={"email": "spray-new@example.test", "password": "x" * 12})
    assert r.status_code == 429


def _service(api: TestClient, clock: FrozenClock, **cfg: Any) -> AuthService:
    container = container_of(api)
    security = container.config.security.model_copy(update=cfg)
    return AuthService(container.uow, clock, container.ids, security)


def test_success_resets_the_email_counter(api: TestClient) -> None:
    # Frozen clock: the fixed window can never roll over mid-test, however slow the host is.
    container = container_of(api)
    email = add_user(container, add_workspace(container), Role.ANALYST)
    auth = _service(api, FrozenClock(datetime.now(UTC)), login_max_failures_per_email=3)

    def fail(n: int) -> None:
        for _ in range(n):
            with pytest.raises(AuthenticationError):
                auth.login(email, "wrong password!!", client_ip="10.0.0.9")

    fail(2)
    auth.login(email, PASSWORD, client_ip="10.0.0.9")
    fail(2)  # would be throttled on the 3rd+ without the reset
    assert auth.login(email, PASSWORD, client_ip="10.0.0.9").principal.email == email


def test_lockout_expires_after_the_window(api: TestClient) -> None:
    container = container_of(api)
    email = add_user(container, add_workspace(container), Role.ANALYST)
    clock = FrozenClock(datetime(2030, 1, 1, tzinfo=UTC))
    auth = _service(api, clock, login_max_failures_per_email=2, login_failure_window_s=60)
    for _ in range(2):
        with pytest.raises(AuthenticationError):
            auth.login(email, "wrong password!!", client_ip="10.0.0.1")
    with pytest.raises(RateLimitedError):
        auth.login(email, PASSWORD, client_ip="10.0.0.2")
    clock.advance(timedelta(seconds=61))
    assert auth.login(email, PASSWORD, client_ip="10.0.0.1").principal.email == email


def test_denial_audit_rows_are_bounded_per_principal(api: TestClient) -> None:
    """A Viewer spamming 403s gets 403 every time but cannot grow the audit chain unboundedly."""
    container = container_of(api)
    workspace = add_workspace(container)
    csrf = login(api, add_user(container, workspace, Role.VIEWER))
    for _ in range(6):
        r = api.post("/api/v1/audit/verify", headers={"X-CSRF-Token": csrf})
        assert r.status_code == 403
    scope = WorkspaceScope(workspace_id=workspace, principal_id="usr_t")
    with container.uow(scope) as s:
        rows = s.execute(
            text(
                "SELECT count(*) FROM audit_logs WHERE workspace_id = :ws "
                "AND action = 'authz.check' AND result = 'denied'"
            ),
            {"ws": workspace},
        ).scalar_one()
    assert rows == 2
