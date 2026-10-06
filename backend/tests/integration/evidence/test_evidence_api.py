"""TASK-EVID-004/005 + TASK-TOOL-008: claims, the read API with redaction, evidence.get."""

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.api.deps import current_principal
from app.core.clock import SystemClock
from app.core.config import AppConfig, Environment
from app.core.errors import NotFoundError, SecurityError
from app.core.ids import UuidV7Ids
from app.evidence.api import REDACTED
from app.evidence.claims import ClaimRepository
from app.evidence.domain import Claim, Evidence, QueryInfo
from app.evidence.preview import rows_preview
from app.evidence.recorder import CallKey, EvidenceRecorder
from app.evidence.repository import EvidenceRepository
from app.infra.db import UnitOfWork
from app.main import create_app
from app.security.auth import Principal
from app.tools.domain import ToolContext, ToolPermission
from app.tools.executor import ToolExecutor, TurnBudget
from app.tools.permissions import Role
from app.tools.registry import ToolRegistry, default_tools
from tests.integration.validator.seed import Seeded, store_evidence, workspace
from tests.unit.validator.factories import AT, evidence

from ..conftest import TestDatabase

SQL = "SELECT sum(amount) FROM orders WHERE region = $1"


def _evidence(seeded: Seeded, suffix: str = "a") -> Evidence:
    record = evidence(
        f"ev_{seeded.dataset_version_id}_{suffix}",
        workspace_id=seeded.scope.workspace_id,
        dataset_id=seeded.dataset_id,
        dataset_version_id=seeded.dataset_version_id,
    )
    query = QueryInfo(engine="duckdb@1.4", sql=SQL, params={"region": "West"})
    return record.model_copy(update={"query": query})


def _claim(seeded: Seeded, *evidence_ids: str, **kw: Any) -> Claim:
    fields: dict[str, Any] = {"kind": "answer_sentence", "text": "Revenue fell 14.2%."} | kw
    return Claim(
        claim_id=f"cl_{seeded.dataset_version_id}_{len(evidence_ids)}{fields['kind'][0]}",
        workspace_id=seeded.scope.workspace_id,
        evidence_ids=list(evidence_ids),
        created_at=AT,
        **fields,
    )


@pytest.fixture
def api(database: TestDatabase, tmp_path: Path) -> Iterator[tuple[TestClient, Seeded]]:
    config = AppConfig(
        _env_file=None,
        env=Environment.TEST,
        database={"url": database.owner_url},
        storage={"datastore_path": tmp_path / "datastore"},
        security={"session_secret": "t" * 32},
    )
    app = create_app(config)
    with TestClient(app, raise_server_exceptions=False) as client:
        seeded = workspace(app.state.container.uow)
        app.dependency_overrides[current_principal] = lambda: _principal(seeded, Role.VIEWER)
        yield client, seeded


def _principal(seeded: Seeded, role: Role) -> Principal:
    scope = seeded.scope
    return Principal(
        user_id=scope.principal_id,
        email="viewer@example.test",
        workspace_id=scope.workspace_id,
        role=role,
        session_id="sess_test",
    )


def _uow(client: TestClient) -> UnitOfWork:
    uow: UnitOfWork = client.app.state.container.uow  # type: ignore[attr-defined]
    return uow


def test_evidence_sql_is_redacted_without_view_query(api: tuple[TestClient, Seeded]) -> None:
    client, seeded = api
    record = _evidence(seeded)
    store_evidence(_uow(client), seeded, record)
    body = client.get(f"/api/v1/evidence/{record.evidence_id}").json()
    assert body["query"] == {"engine": "duckdb@1.4", "sql": REDACTED, "params": {}}
    assert body["result"]["value"] == -0.142
    overrides = client.app.dependency_overrides  # type: ignore[attr-defined]
    overrides[current_principal] = lambda: _principal(seeded, Role.ANALYST)
    full = client.get(f"/api/v1/evidence/{record.evidence_id}").json()
    assert full["query"]["sql"] == SQL
    assert full["query"]["params"] == {"region": "West"}


def test_evidence_of_another_workspace_is_404(api: tuple[TestClient, Seeded]) -> None:
    client, _ = api
    other = workspace(_uow(client))
    record = _evidence(other)
    store_evidence(_uow(client), other, record)
    r = client.get(f"/api/v1/evidence/{record.evidence_id}")
    assert (r.status_code, r.json()["error_code"]) == (404, "NOT_FOUND")


def test_claim_read_model_embeds_redacted_evidence(api: tuple[TestClient, Seeded]) -> None:
    client, seeded = api
    first, second = _evidence(seeded, "a"), _evidence(seeded, "b")
    store_evidence(_uow(client), seeded, first, second)
    claim = _claim(seeded, first.evidence_id, second.evidence_id)
    with _uow(client)(seeded.scope) as s:
        assert ClaimRepository(s).insert(seeded.scope, claim) is True
        assert ClaimRepository(s).insert(seeded.scope, claim) is False  # idempotent
    body = client.get(f"/api/v1/claims/{claim.claim_id}").json()
    assert Claim.model_validate(body["claim"]) == claim
    assert [e["evidence_id"] for e in body["evidence"]] == [first.evidence_id, second.evidence_id]
    assert {e["query"]["sql"] for e in body["evidence"]} == {REDACTED}
    missing = client.get("/api/v1/claims/cl_missing")
    assert (missing.status_code, missing.json()["error_code"]) == (404, "NOT_FOUND")


def test_widget_claim_roundtrip(api: tuple[TestClient, Seeded]) -> None:
    client, seeded = api
    record = _evidence(seeded)
    store_evidence(_uow(client), seeded, record)
    claim = _claim(seeded, record.evidence_id, kind="widget", text=None, widget_id="w_revenue")
    with _uow(client)(seeded.scope) as s:
        ClaimRepository(s).insert(seeded.scope, claim)
        assert ClaimRepository(s).get(seeded.scope, claim.claim_id) == claim


def test_claim_must_cite_existing_evidence_of_its_workspace(
    api: tuple[TestClient, Seeded],
) -> None:
    client, seeded = api
    other = workspace(_uow(client))
    foreign = _evidence(other)
    store_evidence(_uow(client), other, foreign)
    with pytest.raises(NotFoundError), _uow(client)(seeded.scope) as s:
        ClaimRepository(s).insert(seeded.scope, _claim(seeded, foreign.evidence_id))
    with pytest.raises(SecurityError), _uow(client)(seeded.scope) as s:
        ClaimRepository(s).insert(seeded.scope, _claim(other, foreign.evidence_id))


def test_evidence_get_tool_through_the_executor(app_engine: Engine) -> None:
    uow = UnitOfWork(app_engine)
    seeded = workspace(uow)
    record = _evidence(seeded)
    store_evidence(uow, seeded, record)
    registry = ToolRegistry(default_tools(uow))
    executor = ToolExecutor(registry, uow, EvidenceRecorder(SystemClock()), UuidV7Ids())
    ctx = ToolContext(
        workspace_id=seeded.scope.workspace_id,
        principal_id="usr_test",
        trace_id="trace-evidence-get",
        permissions=frozenset({ToolPermission.MEMORY_READ}),
    )

    def call(evidence_id: str, step: str) -> Any:
        key = CallKey(f"turn_{seeded.dataset_version_id}", step)
        args = {"evidence_id": evidence_id}
        return asyncio.run(executor.run("evidence.get", args, ctx, key, TurnBudget(10)))

    ok = call(record.evidence_id, "s1")
    assert ok.status == "ok"
    assert ok.evidence_id is None  # reading evidence creates none
    assert ok.output["value"] == -0.142
    assert "sql" not in str(ok.output)
    missing = call("ev_does_not_exist", "s2")
    assert (missing.status, missing.error.code) == ("error", "NOT_FOUND")
    other = workspace(uow)
    foreign = _evidence(other)
    store_evidence(uow, other, foreign)
    assert call(foreign.evidence_id, "s3").error.code == "NOT_FOUND"
    assert "evidence.get" in {s.name for s in registry.specs_for({ToolPermission.MEMORY_READ})}


def test_evidence_data_serves_the_stored_rows_in_scope(api: tuple[TestClient, Seeded]) -> None:
    client, seeded = api
    record = _evidence(seeded, "rows")
    output = {
        "table": {
            "columns": [
                {"name": "Region", "type": "string", "role": "dimension"},
                {"name": "value", "type": "float", "role": "value", "unit": "currency:INR"},
            ],
            "rows": [["North", 245.0], ["South", 100.0]],
            "truncated": False,
        }
    }
    with _uow(client)(seeded.scope) as s:
        EvidenceRepository(s).insert(seeded.scope, record, rows_preview(output))
    body = client.get(f"/api/v1/evidence/{record.evidence_id}/data").json()  # as a Viewer
    assert body["rows"] == [
        {"Region": "North", "value": 245.0},
        {"Region": "South", "value": 100.0},
    ]
    assert [c["unit"] for c in body["columns"]] == [None, "currency:INR"]
    assert (body["truncated"], body["row_count"]) == (False, 2)
    plain = _evidence(seeded, "plain")  # evidence without a table: no rows, not an error
    store_evidence(_uow(client), seeded, plain)
    assert client.get(f"/api/v1/evidence/{plain.evidence_id}/data").json()["rows"] == []
    other = workspace(_uow(client))
    foreign = _evidence(other)
    store_evidence(_uow(client), other, foreign)
    r = client.get(f"/api/v1/evidence/{foreign.evidence_id}/data")
    assert (r.status_code, r.json()["error_code"]) == (404, "NOT_FOUND")
