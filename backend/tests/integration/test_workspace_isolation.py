"""TASK-DB-012 (PostgreSQL RLS) and TASK-SEC-006 (cross-workspace isolation, partial).

Two layers are proven separately:
- API: every route that takes an object id, called by workspace B's admin with workspace A's
  ids, answers 404/403 and leaves A's objects untouched. A new route with an unknown path
  parameter fails `test_every_id_route_hides_other_workspaces` until it is added to `ids`.
- Database: with RLS on, the app role sees only the scoped workspace's rows even for a query
  with no WHERE clause, cannot write rows into another workspace, and sees nothing unscoped.
"""

import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import ProgrammingError

from app.chat.repository import Conversation, ConversationRepository
from app.connectors.service import ConnectorCreate, ConnectorRepository
from app.core.ids import UuidV7Ids
from app.core.scope import SystemScope
from app.evidence.claims import ClaimRepository
from app.evidence.domain import Claim
from app.infra.db import UnitOfWork
from app.jobs.models import JobType
from app.jobs.repository import JobRepository
from app.security.audit import AuditKeys, AuditLog
from app.security.repository import AuthRepository
from app.tools.permissions import Role
from tests.integration.validator.seed import Seeded, store_evidence, workspace
from tests.unit.validator.factories import AT, evidence

from .auth_helpers import add_user, add_workspace, container_of, csrf_headers, login, make_app
from .conftest import TestDatabase

SYSTEM = SystemScope("system:test")
KEYS = AuditKeys(current_id="iso", keys={"iso": b"i" * 32})
# Every table that carries workspace data. `workspaces` is keyed by `id`; `users` has no
# workspace and is visible to SystemScope only.
WORKSPACE_TABLES = (
    "datasets",
    "dataset_versions",
    "dataset_profiles",
    "understandings",
    "domain_classifications",
    "semantic_models",
    "jobs",
    "evidence",
    "tool_runs",
    "claims",
    "validation_results",
    "memberships",
    "sessions",
    "audit_logs",
    "model_runs",
    "conversations",
    "messages",
    "connectors",
    "analysis_plans",
    "analysis_runs",
)
RLS_TABLES = (*WORKSPACE_TABLES, "workspaces", "users")
# Routes whose body must be valid for the request to reach the workspace-scoped lookup.
BODIES: dict[tuple[str, str], dict[str, Any]] = {
    ("PUT", "/api/v1/datasets/{dataset_id}/domain"): {"domain": "retail"},
    ("PATCH", "/api/v1/datasets/{dataset_id}/versions/{version_id}/semantic-model"): {
        "base_version": 1,
        "overrides": [{"field": "measures.Quantity.default_agg", "value": "avg"}],
    },
    ("POST", "/api/v1/chat/conversations/{conversation_id}/messages"): {"content": "hi"},
}
SEEDED = (
    "datasets",
    "dataset_versions",
    "evidence",
    "claims",
    "jobs",
    "audit_logs",
    "sessions",
    "conversations",
    "messages",
    "connectors",
)


@dataclass(frozen=True)
class WorkspaceA:
    seeded: Seeded
    evidence_id: str
    claim_id: str
    job_id: str
    conversation_id: str
    connector_id: str


def seed_workspace(uow: UnitOfWork) -> WorkspaceA:
    """A workspace with a dataset version, evidence, claim, job, audit row, user and session."""
    seeded = workspace(uow)
    scope = seeded.scope
    record = evidence(
        f"ev_{seeded.dataset_version_id}_iso",
        workspace_id=scope.workspace_id,
        dataset_id=seeded.dataset_id,
        dataset_version_id=seeded.dataset_version_id,
    )
    store_evidence(uow, seeded, record)
    claim = Claim(
        claim_id=f"cl_{seeded.dataset_version_id}_iso",
        workspace_id=scope.workspace_id,
        evidence_ids=[record.evidence_id],
        created_at=AT,
        kind="answer_sentence",
        text="Revenue fell 14.2%.",
    )
    ids = UuidV7Ids()
    job_id, user_id = ids.new("job"), ids.new("usr")
    with uow(scope) as s:
        ClaimRepository(s).insert(scope, claim)
        JobRepository(s).enqueue(scope, job_id, JobType.EVALUATE, {})
        AuditLog(s, KEYS).append(scope, action="t", resource="r", result="success")
    with uow(SYSTEM) as s:
        repo = AuthRepository(s)
        repo.create_user(SYSTEM, user_id, f"{user_id}@example.test", "x")
        repo.add_membership(SYSTEM, user_id, scope.workspace_id, Role.ADMIN)
        s.execute(
            text(
                "INSERT INTO sessions (id, user_id, workspace_id, created_at, last_seen_at, "
                "expires_at) VALUES (:id, :u, :ws, now(), now(), now() + interval '1 hour')"
            ),
            {"id": uuid.uuid4().hex, "u": user_id, "ws": scope.workspace_id},
        )
    conversation = Conversation(
        id=ids.new("cv"),
        owner_id=user_id,
        dataset_version_id=seeded.dataset_version_id,
        semantic_model_version=1,
        title="A's conversation",
        created_at=AT,
    )
    with uow(scope) as s:
        ConversationRepository(s).create(scope, conversation)
        ConversationRepository(s).add_question(scope, ids.new("msg"), conversation.id, "q?")
    connector_id = ids.new("conn")
    source = ConnectorCreate(
        name="src", host="db.example", database="d", username="u", password="p", table_name="t"
    )
    with uow(scope) as s:
        ConnectorRepository(s).insert(scope, connector_id, source, "v1.sealed")
    return WorkspaceA(
        seeded, record.evidence_id, claim.claim_id, job_id, conversation.id, connector_id
    )


@pytest.fixture
def owner(database: TestDatabase) -> Iterator[Engine]:
    engine = create_engine(database.owner_url)
    yield engine
    engine.dispose()


# --- Database layer (TASK-DB-012) --------------------------------------------------------


def test_rls_enabled_on_every_workspace_table_and_app_role_owns_none(owner: Engine) -> None:
    with owner.connect() as conn:
        tables = {
            r.relname: r
            for r in conn.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity, pg_get_userbyid(c.relowner) AS owner, "
                    "(SELECT count(*) FROM pg_policies p WHERE p.schemaname = 'public' "
                    " AND p.tablename = c.relname) AS policies "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relkind = 'r'"
                )
            )
        }
        with_workspace = set(
            conn.execute(
                text(
                    "SELECT table_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND column_name = 'workspace_id'"
                )
            ).scalars()
        )
    # A future table with a workspace_id column must get a policy (and be listed here).
    assert with_workspace <= set(WORKSPACE_TABLES)
    for name in RLS_TABLES:
        assert tables[name].relrowsecurity, f"RLS is off on {name}"
        assert tables[name].policies >= 1, f"{name} has no policy"
    # RLS does not apply to a table's owner: the app role must own nothing.
    assert all(t.owner != "analyzer_app" for t in tables.values())


def test_no_where_clause_returns_only_the_scoped_workspace(
    app_engine: Engine, owner: Engine
) -> None:
    uow = UnitOfWork(app_engine)  # least-privilege analyzer_app
    a = seed_workspace(uow).seeded.scope
    b = seed_workspace(uow).seeded.scope
    with owner.connect() as conn:  # the owner bypasses RLS: A's rows really exist
        for table in SEEDED:
            n = conn.execute(
                text(f"SELECT count(*) FROM {table} WHERE workspace_id = :ws"),  # noqa: S608
                {"ws": a.workspace_id},
            ).scalar_one()
            assert n >= 1, table
    with uow(b) as s:
        for table in WORKSPACE_TABLES:
            query = text(f"SELECT workspace_id FROM {table}")  # noqa: S608 (constant names)
            seen = set(s.execute(query).scalars())
            assert seen <= {b.workspace_id}, f"{table} leaked {seen}"
        assert s.execute(text("SELECT id FROM workspaces")).scalars().all() == [b.workspace_id]
        assert s.execute(text("SELECT count(*) FROM users")).scalar_one() == 0


def test_unscoped_app_connection_sees_nothing(app_engine: Engine) -> None:
    seed_workspace(UnitOfWork(app_engine))
    with app_engine.connect() as conn:
        for table in RLS_TABLES:
            query = text(f"SELECT count(*) FROM {table}")  # noqa: S608 (constant names)
            count = conn.execute(query).scalar_one()
            assert count == 0, table


def test_rows_cannot_be_written_into_another_workspace(app_engine: Engine) -> None:
    uow = UnitOfWork(app_engine)
    a = seed_workspace(uow).seeded.scope
    b = seed_workspace(uow).seeded.scope
    with pytest.raises(ProgrammingError, match="row-level security"), uow(b) as s:
        s.execute(
            text(
                "INSERT INTO datasets (id, workspace_id, name, source_type) "
                "VALUES (:id, :ws, 'x', 'csv')"
            ),
            {"id": f"ds_{uuid.uuid4().hex[:12]}", "ws": a.workspace_id},
        )
    with uow(b) as s:  # UPDATE/DELETE statements without a WHERE clause cannot reach A's rows
        s.execute(text("UPDATE jobs SET cancel_requested = true"))
        s.execute(text("DELETE FROM sessions"))
    with uow(a) as s:
        assert s.execute(text("SELECT bool_or(cancel_requested) FROM jobs")).scalar() is False
        assert s.execute(text("SELECT count(*) FROM sessions")).scalar_one() == 1


def test_worker_claims_jobs_across_workspaces(app_engine: Engine) -> None:
    """The worker dequeues under SystemScope (`app.system`), so it still sees every workspace."""
    uow = UnitOfWork(app_engine)
    mine = {seed_workspace(uow).job_id, seed_workspace(uow).job_id}
    claimed: set[str] = set()
    worker = SystemScope("system:worker")
    while True:
        with uow(worker) as s:
            job = JobRepository(s).claim(worker, "iso-worker", [JobType.EVALUATE.value])
        if job is None:
            break
        claimed.add(job.id)
    assert mine <= claimed


# --- API layer (TASK-SEC-006, partial) ---------------------------------------------------


@pytest.fixture
def api(database: TestDatabase, tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(
        make_app(database, tmp_path), base_url="https://testserver", raise_server_exceptions=False
    ) as client:
        yield client


def _routes(routes: list[Any]) -> list[APIRoute]:
    found: list[APIRoute] = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append(route)
        elif hasattr(route, "original_router"):  # FastAPI >=0.141 lazy include
            found += _routes(route.original_router.routes)
    return found


def test_every_id_route_hides_other_workspaces(api: TestClient) -> None:
    container = container_of(api)
    with container.uow(SYSTEM) as s:  # the API runs as the app role, so RLS is in force
        assert s.execute(text("SELECT current_user")).scalar() == "analyzer_app"
    a = seed_workspace(container.uow)
    ids = {
        "dataset_id": a.seeded.dataset_id,
        "version_id": a.seeded.dataset_version_id,
        "evidence_id": a.evidence_id,
        "claim_id": a.claim_id,
        "job_id": a.job_id,
        "conversation_id": a.conversation_id,
        "connector_id": a.connector_id,
        "name": "Region",  # semantic dimension name (dimension values route)
    }
    csrf = login(api, add_user(container, add_workspace(container), Role.ADMIN))  # B's admin
    checked = 0
    for route in _routes(api.app.routes):  # type: ignore[attr-defined]
        params = re.findall(r"{(\w+)}", route.path)
        if not params:
            continue
        missing = [p for p in params if p not in ids]
        assert not missing, f"add {missing} for {route.path} to the isolation suite"
        path = route.path.format(**ids)
        for method in route.methods or ():
            body = BODIES.get((method, route.path))  # a valid body: reach the id lookup, not 422
            r = api.request(method, path, headers=csrf_headers(csrf), json=body)
            assert r.status_code in (403, 404), f"{method} {path} -> {r.status_code}"
            checked += 1
    assert checked >= 12  # jobs x2, evidence, claims, datasets x5, chat x3 (at time of writing)
    with container.uow(a.seeded.scope) as s:  # POST /jobs/{id}/cancel did not touch A's job
        job = JobRepository(s).get(a.seeded.scope, a.job_id)
    assert job is not None
    assert job.cancel_requested is False


def test_lists_do_not_include_other_workspaces(api: TestClient) -> None:
    container = container_of(api)
    a = seed_workspace(container.uow)
    login(api, add_user(container, add_workspace(container), Role.ADMIN))
    r = api.get("/api/v1/datasets")
    assert r.status_code == 200
    assert a.seeded.dataset_id not in r.text
