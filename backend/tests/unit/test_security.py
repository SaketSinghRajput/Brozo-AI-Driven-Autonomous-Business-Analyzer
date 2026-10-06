"""Unit tests for TASK-SEC-004/005/007/009/012/013 (no database needed)."""

import base64
import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.api.deps import current_principal, require
from app.core.clock import FrozenClock
from app.core.config import AppConfig, ConfigError, Environment, SecurityConfig
from app.core.errors import AuthenticationError, SecurityError
from app.core.logging import REDACTED, JsonFormatter
from app.main import create_app
from app.security import bootstrap, passwords
from app.security.audit import GENESIS_HASH, AuditKeys, chain_hash
from app.security.crypto import CredentialCipher
from app.security.middleware import BodyLimitMiddleware
from app.security.passwords import PasswordHasher, check_new_password
from app.security.permissions import ROLE_PERMISSIONS, Permission
from app.security.throttle import WindowCounter
from app.tools.domain import ToolPermission
from app.tools.permissions import ROLE_TOOL_PERMISSIONS, Role

P = Permission
SECRET = "s" * 32

# --- RBAC (design §59) -------------------------------------------------------------------

EXPECTED = {
    Role.VIEWER: {
        P.VIEW_DATASET, P.RUN_ANALYSIS, P.EXECUTE_TOOL, P.ACCESS_DOCUMENTS, P.READ_MEMORY,
        P.RUN_SIMULATION,
    },
    Role.ANALYST: {
        P.VIEW_DATASET, P.VIEW_QUERY, P.UPLOAD_DATASET, P.EDIT_SEMANTICS, P.RUN_ANALYSIS,
        P.EXECUTE_TOOL, P.TRAIN_MODEL, P.ACCESS_DOCUMENTS, P.READ_MEMORY, P.WRITE_MEMORY,
        P.RUN_SIMULATION,
    },
    Role.ADMIN: set(Permission),
}  # fmt: skip


@pytest.mark.parametrize("role", list(Role))
def test_role_matrix_matches_design(role: Role) -> None:
    assert ROLE_PERMISSIONS[role] == EXPECTED[role]


@pytest.mark.parametrize("role", list(Role))
def test_tool_permissions_agree_with_request_permissions(role: Role) -> None:
    tools = ROLE_TOOL_PERMISSIONS[role]
    perms = ROLE_PERMISSIONS[role]
    assert (ToolPermission.MODEL_RUN in tools) == (P.TRAIN_MODEL in perms)
    assert (ToolPermission.MEMORY_WRITE in tools) == (P.WRITE_MEMORY in perms)
    assert (ToolPermission.CODE_EXECUTION in tools) == (P.RUN_REPAIR in perms)


PUBLIC = {  # paths below /api/v1
    ("GET", "/health"),
    ("GET", "/health/ready"),  # TASK-INFRA-004: booleans only, no internals
    ("POST", "/auth/login"),
}
SESSION_ONLY = {("POST", "/auth/logout"), ("GET", "/auth/me")}


def _api_routes(routes: list[Any]) -> list[APIRoute]:
    """Flattens FastAPI >=0.141's lazily included routers (`_IncludedRouter`)."""
    found: list[APIRoute] = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append(route)
        elif hasattr(route, "original_router"):
            found += _api_routes(route.original_router.routes)
    return found


def _permissions(dependant: Dependant) -> set[Permission]:
    found = {dependant.call.permission} if isinstance(dependant.call, require) else set()
    for sub in dependant.dependencies:
        found |= _permissions(sub)
    return found


def _needs_session(dependant: Dependant) -> bool:
    return dependant.call is current_principal or any(
        _needs_session(d) for d in dependant.dependencies
    )


def test_every_route_declares_a_permission() -> None:
    """TASK-SEC-005: any new route without `require(...)` fails here unless listed above."""
    from app.api.router import api_router

    routes = _api_routes(api_router.routes)
    declared = {(m, r.path) for r in routes for m in r.methods or ()}
    assert declared >= PUBLIC | SESSION_ONLY | {("GET", "/jobs/{job_id}")}
    for route in routes:
        for method in route.methods or ():
            key = (method, route.path)
            if key in PUBLIC:
                continue
            assert _needs_session(route.dependant), f"{key} must require a session"
            if key not in SESSION_ONLY:
                assert len(_permissions(route.dependant)) == 1, f"{key} declares no permission"


# --- Passwords ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def hasher() -> PasswordHasher:
    return PasswordHasher(SecurityConfig(session_secret=SECRET))


def test_hash_is_argon2id_with_owasp_minimum(hasher: PasswordHasher) -> None:
    stored = hasher.hash("correct horse battery")
    assert stored.startswith("$argon2id$v=19$m=19456,t=2,p=1$")
    assert hasher.verify(stored, "correct horse battery")
    assert not hasher.verify(stored, "wrong horse battery")
    assert not hasher.verify("not-a-hash", "x")
    assert not hasher.needs_rehash(stored)


def test_unknown_user_still_costs_one_argon2_verification(
    hasher: PasswordHasher, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    argon2_class = type(hasher._argon2)
    real = argon2_class.verify

    def spy(self: Any, stored: str, password: str) -> bool:
        calls.append(stored)
        return real(self, stored, password)

    monkeypatch.setattr(argon2_class, "verify", spy)
    assert not hasher.verify(None, "anything at all")
    assert len(calls) == 1
    assert calls[0].startswith("$argon2id$")


def test_stronger_config_flags_old_hashes_for_rehash(hasher: PasswordHasher) -> None:
    stronger = PasswordHasher(SecurityConfig(session_secret=SECRET, argon2_time_cost=3))
    assert stronger.needs_rehash(hasher.hash("correct horse battery"))


def test_argon2_parameters_cannot_go_below_owasp_minimum() -> None:
    with pytest.raises(ValueError, match="argon2_memory_kib"):
        SecurityConfig(session_secret=SECRET, argon2_memory_kib=4096)
    # OWASP: m=19456 needs t>=2; t=1 is only acceptable with m>=47104 (L1).
    with pytest.raises(ValueError, match="OWASP"):
        SecurityConfig(session_secret=SECRET, argon2_memory_kib=19_456, argon2_time_cost=1)
    with pytest.raises(ValueError, match="OWASP"):
        SecurityConfig(session_secret=SECRET, argon2_memory_kib=47_103, argon2_time_cost=1)
    SecurityConfig(session_secret=SECRET, argon2_memory_kib=47_104, argon2_time_cost=1)
    SecurityConfig(session_secret=SECRET, argon2_memory_kib=19_456, argon2_time_cost=2)


def test_new_password_policy() -> None:
    check_new_password("x" * passwords.MIN_PASSWORD_LENGTH)
    for bad in ("x" * (passwords.MIN_PASSWORD_LENGTH - 1), "x" * 1025):
        with pytest.raises(ValueError, match="characters"):
            check_new_password(bad)


# --- Sessions / CSRF ---------------------------------------------------------------------


def _with_security(config: AppConfig, **security: Any) -> AppConfig:
    return config.model_copy(update={"security": SecurityConfig(session_secret=SECRET, **security)})


def test_csrf_token_is_bound_to_session_and_secret(config: AppConfig) -> None:
    auth = create_app(config).state.container.auth
    assert auth.csrf_token("a") == auth.csrf_token("a")
    assert auth.csrf_token("a") != auth.csrf_token("b")
    other = create_app(_with_security(config)).state.container.auth  # different secret
    assert other.csrf_token("a") != auth.csrf_token("a")


@pytest.mark.parametrize("token", [None, "", "x" * 129])
def test_missing_or_oversized_token_is_rejected_before_any_query(
    config: AppConfig, token: str | None
) -> None:
    with pytest.raises(AuthenticationError):
        create_app(config).state.container.auth.authenticate(token)


def test_cookie_name_uses_host_prefix_only_when_secure(config: AppConfig) -> None:
    insecure = _with_security(config, cookie_secure=False)
    assert create_app(config).state.container.auth.cookie_name == "__Host-session"
    assert create_app(insecure).state.container.auth.cookie_name == "session"


# --- Audit hash chain --------------------------------------------------------------------


KEY = b"k" * 32


def test_chain_hash_is_keyed_canonical_and_linked() -> None:
    a = chain_hash(KEY, GENESIS_HASH, {"x": 1, "y": "é"})
    assert a == chain_hash(KEY, GENESIS_HASH, {"y": "é", "x": 1})  # key order does not matter
    assert a != chain_hash(KEY, GENESIS_HASH, {"x": 2, "y": "é"})
    assert a != chain_hash(KEY, "1" * 64, {"x": 1, "y": "é"})
    assert a != chain_hash(b"j" * 32, GENESIS_HASH, {"x": 1, "y": "é"})  # L4: needs the key
    assert len(a) == 64


def test_audit_keys_from_config() -> None:
    derived = AuditKeys.from_config(SecurityConfig(session_secret=SECRET))
    assert derived.current_id == "derived"
    other = AuditKeys.from_config(SecurityConfig(session_secret="x" * 32))
    assert derived.current_key != other.current_key  # dev key follows session_secret
    rotated = AuditKeys.from_config(
        SecurityConfig(
            session_secret=SECRET,
            audit_key="a" * 32,
            audit_key_id="k2",
            audit_retired_keys={"k1": "b" * 32},
        )
    )
    assert rotated.current_id == "k2"
    assert rotated.current_key == b"a" * 32
    assert rotated.keys["k1"] == b"b" * 32
    with pytest.raises(ValueError, match="audit_key"):
        SecurityConfig(session_secret=SECRET, audit_key="short")


@pytest.mark.parametrize("env", [Environment.STAGING, Environment.PROD])
def test_audit_key_required_outside_dev(config: AppConfig, env: Environment) -> None:
    data = config.model_dump() | {"env": env}
    data["security"] = {"session_secret": SECRET}
    with pytest.raises(ValueError, match="audit_key"):
        AppConfig(_env_file=None, **data)
    data["security"]["audit_key"] = "a" * 32
    assert AppConfig(_env_file=None, **data).env is env


# --- Credential encryption ---------------------------------------------------------------


@pytest.fixture
def cipher() -> CredentialCipher:
    key = CredentialCipher.generate_key()
    return CredentialCipher.from_config(SecurityConfig(session_secret=SECRET, credential_key=key))


def test_credentials_round_trip_and_are_not_readable(cipher: CredentialCipher) -> None:
    stored = cipher.encrypt("pg-password-123", context="connector:c1")
    assert "pg-password-123" not in stored
    assert stored.startswith("v1.")
    assert stored != cipher.encrypt("pg-password-123", context="connector:c1")  # fresh nonce
    assert cipher.decrypt(stored, context="connector:c1") == "pg-password-123"


def test_wrong_key_context_or_tampering_fails(cipher: CredentialCipher) -> None:
    stored = cipher.encrypt("secret", context="connector:c1")
    other = CredentialCipher(base64.urlsafe_b64decode(CredentialCipher.generate_key()))
    raw = bytearray(base64.urlsafe_b64decode(stored[3:]))
    raw[-1] ^= 1
    tampered = "v1." + base64.urlsafe_b64encode(bytes(raw)).decode()
    attempts = [
        (other, stored, "connector:c1"),  # wrong key
        (cipher, stored, "connector:c2"),  # ciphertext moved to another connector
        (cipher, tampered, "connector:c1"),
        (cipher, "v0.abc", "connector:c1"),
        (cipher, "v1.!!!", "connector:c1"),
    ]
    for c, value, context in attempts:
        with pytest.raises(SecurityError):
            c.decrypt(value, context=context)


@pytest.mark.parametrize(
    "key", [None, "not base64 ***", base64.urlsafe_b64encode(b"short").decode()]
)
def test_bad_or_missing_key_is_a_config_error(key: str | None) -> None:
    with pytest.raises(ConfigError):
        CredentialCipher.from_config(SecurityConfig(session_secret=SECRET, credential_key=key))


def test_credential_key_is_never_in_repr() -> None:
    key = CredentialCipher.generate_key()
    assert key not in repr(SecurityConfig(session_secret=SECRET, credential_key=key))


# --- Headers and body limits (B1) --------------------------------------------------------


def test_security_headers_on_api_responses(client: TestClient) -> None:
    for r in (client.get("/api/v1/health"), client.get("/api/v1/nope")):
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["referrer-policy"] == "no-referrer"
        csp = r.headers["content-security-policy"]
        assert csp == "default-src 'none'; frame-ancestors 'none'"
        assert r.headers["cache-control"] == "no-store"
        assert r.headers["strict-transport-security"].startswith("max-age=")


def test_docs_page_keeps_working_without_the_api_csp(client: TestClient) -> None:
    r = client.get("/api/docs")
    assert r.status_code == 200
    assert "content-security-policy" not in r.headers
    assert r.headers["x-frame-options"] == "DENY"


def test_oversized_declared_body_is_413(client: TestClient) -> None:
    body = json.dumps({"email": "a@b.c", "password": "x" * (1024 * 1024 + 1)})
    r = client.post(
        "/api/v1/auth/login", content=body, headers={"content-type": "application/json"}
    )
    assert r.status_code == 413
    assert r.json()["error_code"] == "PAYLOAD_TOO_LARGE"


def test_oversized_streamed_body_is_413(client: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        for _ in range(20):
            yield b"x" * 65536  # 1.25 MiB, no Content-Length

    r = client.post(
        "/api/v1/auth/login", content=chunks(), headers={"content-type": "application/json"}
    )
    assert r.status_code == 413


def test_upload_paths_get_the_storage_limit() -> None:
    app = FastAPI()

    async def echo(request: Request) -> dict[str, int]:
        return {"size": len(await request.body())}

    app.add_api_route("/api/v1/datasets", echo, methods=["POST"])
    app.add_api_route("/api/v1/other", echo, methods=["POST"])
    app.add_middleware(BodyLimitMiddleware, max_body_bytes=10, max_upload_bytes=100)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.post("/api/v1/datasets", content=b"x" * 50).json() == {"size": 50}
    assert client.post("/api/v1/other", content=b"x" * 50).status_code == 413
    assert client.post("/api/v1/datasets", content=b"x" * 101).status_code == 413


# --- Log redaction (TASK-SEC-013) --------------------------------------------------------


def _formatted(fields: dict[str, Any]) -> dict[str, Any]:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "evt", None, None)
    record.fields = fields
    out: dict[str, Any] = json.loads(JsonFormatter().format(record))
    return out


def test_auth_material_is_redacted_by_key() -> None:
    out = _formatted(
        {
            "csrf_token": "c",
            "session_cookie": "s",
            "credential": "k",
            "nested": [{"password": "p", "ok": 1}],
        }
    )
    assert out["csrf_token"] == out["session_cookie"] == out["credential"] == REDACTED
    assert out["nested"] == [{"password": REDACTED, "ok": 1}]


def test_request_log_omits_query_string_and_body(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    client.get("/api/v1/health", params={"email": "person@example.test"})
    client.post(
        "/api/v1/auth/login",
        json={"email": "person@example.test", "password": "hunter2hunter2", "extra": 1},
    )
    output = "\n".join(JsonFormatter().format(r) for r in caplog.records)
    assert "http.request" in output
    assert "person@example.test" not in output
    assert "hunter2hunter2" not in output


def test_validation_errors_do_not_echo_credentials(client: TestClient) -> None:
    r = client.post(
        "/api/v1/auth/login",
        json={"email": "person@example.test", "password": "", "x": "hunter2hunter2"},
    )
    assert r.status_code == 422
    assert "person@example.test" not in r.text
    assert "hunter2hunter2" not in r.text


def test_login_without_content_type_is_422(client: TestClient) -> None:
    # L5: a JSON body sent with no Content-Type must not be parsed (strict_content_type).
    body = b'{"email": "person@example.test", "password": "hunter2hunter2"}'
    r = client.post("/api/v1/auth/login", content=body)
    assert "content-type" not in {k.lower() for k in r.request.headers}
    assert r.status_code == 422
    assert r.json()["error_code"] == "VALIDATION_ERROR"


def test_window_counter_limits_expires_and_stays_bounded() -> None:
    clock = FrozenClock(datetime(2030, 1, 1, tzinfo=UTC))
    counter = WindowCounter(limit=2, window_s=60, clock=clock, max_keys=3)
    counter.hit("a")
    assert counter.retry_after_s("a") == 0
    counter.hit("a")
    assert counter.retry_after_s("a") == 60
    clock.advance(timedelta(seconds=45))
    assert counter.retry_after_s("a") == 15
    clock.advance(timedelta(seconds=15))
    assert counter.retry_after_s("a") == 0  # window over
    for i in range(10):  # a spray of fresh keys cannot grow the table past max_keys
        counter.hit(f"k{i}")
    assert len(counter._hits) <= 3


# --- Bootstrap CLI (M1) ------------------------------------------------------------------


def test_bootstrap_concurrent_duplicate_prints_no_email(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    email = "dup@example.test"
    detail = Exception(f"duplicate key; DETAIL: Key (lower(email))=({email}) already exists.")

    def racing_insert(*_: object) -> tuple[str, str]:
        raise IntegrityError("INSERT INTO users ...", {"email": email}, detail)

    container = SimpleNamespace(engine=SimpleNamespace(dispose=lambda: None))
    monkeypatch.setattr(bootstrap, "load_config", lambda: None)
    monkeypatch.setattr(bootstrap, "build_container", lambda _: container)
    monkeypatch.setattr(bootstrap, "bootstrap_admin", racing_insert)
    monkeypatch.setenv("APP_BOOTSTRAP_PASSWORD", "x" * 20)
    assert bootstrap.main(["--email", email]) == 1
    out = capsys.readouterr()
    assert "already exists" in out.err
    assert email not in out.err + out.out
