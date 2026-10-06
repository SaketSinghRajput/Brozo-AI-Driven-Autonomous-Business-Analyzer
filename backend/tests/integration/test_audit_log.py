"""TASK-DB-009 / TASK-SEC-012: hash-chained audit log, tamper detection, verifier job, and the
first-admin bootstrap."""

import threading
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import ProgrammingError

from app.core.scope import SystemScope, WorkspaceScope
from app.infra.db import UnitOfWork
from app.jobs.models import JobType
from app.jobs.worker import Worker
from app.security.audit import (
    GENESIS_HASH,
    AuditKeys,
    AuditLog,
    chain_hash,
    hashed_fields,
    make_verify_handler,
)
from app.security.bootstrap import bootstrap_admin
from app.security.members import add_member
from app.tools.permissions import Role

from .auth_helpers import PASSWORD, add_user, container_of, csrf_headers, login, make_app
from .conftest import TestDatabase

KEYS = AuditKeys(current_id="t1", keys={"t1": b"t" * 32})


@pytest.fixture
def uow(app_engine: Engine) -> UnitOfWork:
    return UnitOfWork(app_engine)  # connects as the least-privilege analyzer_app role


@pytest.fixture
def owner(database: TestDatabase) -> Iterator[Engine]:
    engine = create_engine(database.owner_url)
    yield engine
    engine.dispose()


def _scope() -> WorkspaceScope:
    return WorkspaceScope(workspace_id=f"ws_{uuid.uuid4().hex[:12]}", principal_id="usr_t")


def _append(uow: UnitOfWork, scope: WorkspaceScope, n: int) -> list[int]:
    with uow(scope) as s:
        for i in range(n):
            AuditLog(s, KEYS).append(
                scope, action="test.event", resource="r", result="success", metadata={"n": i}
            )
        seqs = s.execute(
            text("SELECT seq FROM audit_logs WHERE workspace_id = :ws ORDER BY seq"),
            {"ws": scope.workspace_id},
        ).scalars()
        return list(seqs)


def _verify(
    uow: UnitOfWork, scope: WorkspaceScope | SystemScope, keys: AuditKeys | None = None
) -> tuple[int, int | None]:
    keys = keys or KEYS
    with uow(scope) as s:
        report = AuditLog(s, keys).verify(scope)
    return report.checked, report.broken_at_seq


def test_chain_verifies(uow: UnitOfWork) -> None:
    scope = _scope()
    _append(uow, scope, 3)
    assert _verify(uow, scope) == (3, None)


def test_app_role_cannot_rewrite_history(uow: UnitOfWork) -> None:
    scope = _scope()
    _append(uow, scope, 1)
    for stmt in ("UPDATE audit_logs SET action = 'x'", "DELETE FROM audit_logs"):
        with pytest.raises(ProgrammingError, match="permission denied"), uow(scope) as s:
            s.execute(text(stmt))


@pytest.mark.parametrize(
    "tamper",
    [
        "UPDATE audit_logs SET metadata = '{\"n\": 99}' WHERE seq = :seq",
        "UPDATE audit_logs SET result = 'failure' WHERE seq = :seq",
        "DELETE FROM audit_logs WHERE seq = :seq",
    ],
)
def test_tampering_is_detected(uow: UnitOfWork, owner: Engine, tamper: str) -> None:
    scope, other = _scope(), _scope()
    seqs = _append(uow, scope, 4)
    _append(uow, other, 2)
    with owner.begin() as conn:  # the owner role bypasses the append-only grant
        conn.execute(text(tamper), {"seq": seqs[1]})
    checked, broken = _verify(uow, scope)
    assert checked == 1
    # An edited row fails itself; after a deletion the next row's prev_hash no longer links.
    assert broken in (seqs[1], seqs[2])
    assert _verify(uow, other) == (2, None)  # chains are per workspace


def test_full_rewrite_without_the_key_is_detected(uow: UnitOfWork, owner: Engine) -> None:
    """L4: an attacker with DB write access recomputes the whole chain consistently, but
    without the server's audit key (a plain SHA-256 chain or a guessed key both fail)."""
    scope = _scope()
    seqs = _append(uow, scope, 3)
    forger = AuditKeys(current_id="t1", keys={"t1": b"x" * 32})
    with owner.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT seq, id, at, actor, workspace_id, action, resource, result, trace_id, "
                "metadata, key_id FROM audit_logs WHERE workspace_id = :ws ORDER BY seq"
            ),
            {"ws": scope.workspace_id},
        ).mappings()
        prev = GENESIS_HASH
        for row in list(rows):
            fields = {**hashed_fields(row), "result": "denied"}  # rewrite history
            digest = chain_hash(forger.current_key, prev, fields)
            conn.execute(
                text(
                    "UPDATE audit_logs SET result = 'denied', prev_hash = :p, hash = :h "
                    "WHERE seq = :seq"
                ),
                {"p": prev, "h": digest, "seq": row["seq"]},
            )
            prev = digest
    assert _verify(uow, scope, forger) == (3, None)  # self-consistent under the forged key
    assert _verify(uow, scope) == (0, seqs[0])  # ...but not under the real one


def test_rotated_key_still_verifies_old_rows(uow: UnitOfWork) -> None:
    scope = _scope()
    seqs = _append(uow, scope, 2)  # written under t1
    rotated = AuditKeys(current_id="t2", keys={"t1": b"t" * 32, "t2": b"u" * 32})
    with uow(scope) as s:
        AuditLog(s, rotated).append(scope, action="after.rotation", resource="r", result="success")
    assert _verify(uow, scope, rotated) == (3, None)
    checked, broken = _verify(uow, scope)  # the old keyring does not know t2
    assert checked == 2
    assert broken is not None
    assert broken > seqs[1]


def test_concurrent_appends_do_not_fork_the_chain(uow: UnitOfWork) -> None:
    scope = _scope()
    threads = [threading.Thread(target=_append, args=(uow, scope, 10)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert _verify(uow, scope) == (40, None)


def test_metadata_must_not_carry_data_values(uow: UnitOfWork) -> None:
    scope = _scope()
    with pytest.raises(ValueError, match="id/hash/flag"), uow(scope) as s:
        AuditLog(s, KEYS).append(
            scope,
            action="a",
            resource="r",
            result="success",
            metadata={"revenue": 4829312.5},  # type: ignore[dict-item]
        )


@pytest.fixture
def api(database: TestDatabase, tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(
        make_app(database, tmp_path), base_url="https://testserver", raise_server_exceptions=False
    ) as client:
        yield client


def test_bootstrap_admin_then_verify_job(api: TestClient, owner: Engine) -> None:
    container = container_of(api)
    email = f"admin-{uuid.uuid4().hex[:8]}@example.test"
    workspace_id, _ = bootstrap_admin(container, email, PASSWORD, "Demo")
    with pytest.raises(ValueError, match="already exists"):
        bootstrap_admin(container, email.upper(), PASSWORD, "Again")
    with pytest.raises(ValueError, match="characters"):
        bootstrap_admin(container, "short@example.test", "too short", "X")

    csrf = login(api, email)
    assert api.get("/api/v1/auth/me").json()["role"] == "admin"
    handlers = {
        JobType.AUDIT_VERIFY.value: make_verify_handler(container.uow, container.auth.audit_keys)
    }
    worker = Worker(container.uow, handlers)

    def run_verify() -> tuple[str, str | None, str | None]:
        r = api.post("/api/v1/audit/verify", headers=csrf_headers(csrf))
        assert r.status_code == 202
        assert worker.run_once()
        job = api.get(f"/api/v1/jobs/{r.json()['id']}").json()
        return job["status"], job["result_ref"], (job["error"] or {}).get("code")

    status, result_ref, _ = run_verify()
    assert status == "COMPLETED"
    assert result_ref is not None
    assert result_ref.startswith("audit_verified:")
    assert int(result_ref.split(":")[1]) >= 1  # at least the login event

    with owner.begin() as conn:
        conn.execute(
            text(
                "UPDATE audit_logs SET actor = 'someone-else' "
                "WHERE workspace_id = :ws AND action = 'auth.login'"
            ),
            {"ws": workspace_id},
        )
    status, _, code = run_verify()
    assert (status, code) == ("FAILED", "SECURITY_ERROR")


def test_members_cli_adds_new_and_existing_users_and_audits(api: TestClient) -> None:
    """L-2: viewer/analyst users without code; existing users keep their password."""
    container = container_of(api)
    ws_a, _ = bootstrap_admin(container, f"a-{uuid.uuid4().hex[:8]}@example.test", PASSWORD, "A")
    ws_b, _ = bootstrap_admin(container, f"b-{uuid.uuid4().hex[:8]}@example.test", PASSWORD, "B")
    email = f"member-{uuid.uuid4().hex[:8]}@example.test"
    asked: list[str] = []

    def password() -> str:
        asked.append("x")
        return PASSWORD

    user_id, created = add_member(container, email, ws_a, Role.VIEWER, password)
    assert created
    assert asked == ["x"]
    login(api, email)
    assert api.get("/api/v1/auth/me").json()["role"] == "viewer"

    same, created = add_member(container, email.upper(), ws_b, Role.ANALYST, password)
    assert (same, created, asked) == (user_id, False, ["x"])  # no password for an existing user
    with pytest.raises(ValueError, match="already a member"):
        add_member(container, email, ws_a, Role.ADMIN, password)
    with pytest.raises(ValueError, match="workspace not found"):
        add_member(container, email, "ws_doesnotexist", Role.VIEWER, password)
    with pytest.raises(ValueError, match="characters"):
        add_member(container, f"n-{uuid.uuid4().hex[:8]}@example.test", ws_a, Role.VIEWER, str)

    with container.uow(SystemScope("system:test")) as s:
        events = s.execute(
            text(
                "SELECT metadata FROM audit_logs WHERE workspace_id IS NULL "
                "AND action = 'membership.add' AND resource = :uid ORDER BY seq"
            ),
            {"uid": user_id},
        ).scalars()
        added = [(m["workspace_id"], m["role"], m["user_created"]) for m in events]
        report = AuditLog(s, container.auth.audit_keys).verify(SystemScope("system:test"))
    assert added == [(ws_a, "viewer", True), (ws_b, "analyst", False)]
    assert report.ok


def test_verify_requires_view_audit_and_denial_is_audited(api: TestClient) -> None:
    container = container_of(api)
    workspace_id, _ = bootstrap_admin(
        container, f"a-{uuid.uuid4().hex[:8]}@example.test", PASSWORD, "Demo"
    )
    csrf = login(api, add_user(container, workspace_id, Role.ANALYST))
    r = api.post("/api/v1/audit/verify", headers=csrf_headers(csrf))
    assert r.status_code == 403
    scope = WorkspaceScope(workspace_id=workspace_id, principal_id="usr_t")
    with container.uow(scope) as s:
        denied = (
            s.execute(
                text(
                    "SELECT resource FROM audit_logs WHERE workspace_id = :ws "
                    "AND action = 'authz.check' AND result = 'denied'"
                ),
                {"ws": workspace_id},
            )
            .scalars()
            .all()
        )
        jobs = s.execute(
            text("SELECT count(*) FROM jobs WHERE workspace_id = :ws"), {"ws": workspace_id}
        ).scalar_one()
    assert denied == ["VIEW_AUDIT"]
    assert jobs == 0
