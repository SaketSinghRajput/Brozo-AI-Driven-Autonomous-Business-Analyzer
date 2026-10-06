"""TASK-SQL-007 / TASK-TOOL-006 / TASK-ANALYSIS-016 against real PostgreSQL: every analytics
tool call through the ToolExecutor persists Evidence with the compiled query, its parameters,
the dataset version (+ content hash) and the result hash; workspaces stay isolated; a stored
file that no longer matches its content hash is refused; the API returns the evidence id."""

import asyncio
import dataclasses
import hashlib
import io
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.analytics.source import DatasetSource
from app.analytics.sql.engine import ENGINE_VERSION, AnalyticalEngine
from app.analytics.tools import analytics_tools
from app.api.deps import current_principal, get_container
from app.core.clock import FrozenClock
from app.core.config import AppConfig
from app.core.ids import UuidV7Ids
from app.core.scope import SystemScope, WorkspaceScope
from app.evidence.domain import Evidence
from app.evidence.recorder import CallKey, EvidenceRecorder
from app.evidence.repository import EvidenceRepository
from app.infra.db import UnitOfWork
from app.infra.storage import LocalStorage
from app.main import create_app
from app.tools.domain import ToolContext, ToolPermission, ToolResult
from app.tools.executor import ToolExecutor, TurnBudget
from app.tools.registry import ToolRegistry
from tests.principals import principal_for
from tests.unit.analytics.conftest import semantic_model, small_orders

ALL = frozenset({ToolPermission.SQL_READ, ToolPermission.PYTHON_ANALYTICS})
AUG = {"start": "2026-08-01", "end": "2026-08-31"}


def _hex() -> str:
    return uuid.uuid4().hex[:12]


def _seed(uow: UnitOfWork, storage: LocalStorage) -> dict[str, str]:
    ws, ds, dsv = f"ws_{_hex()}", f"ds_{_hex()}", f"dsv_{_hex()}"
    buf = io.BytesIO()
    pq.write_table(small_orders(), buf)
    data = buf.getvalue()
    key = f"{ws}/{dsv}/data.parquet"
    with storage.writer(key) as f:
        f.write(data)
    content_hash = f"sha256:{hashlib.sha256(data).hexdigest()}"
    model = semantic_model(dataset_version_id=dsv, semantic_model_id=f"sm_{_hex()}")
    with uow(SystemScope("system:test")) as s:
        s.execute(text("INSERT INTO workspaces (id, name) VALUES (:id, 'a')"), {"id": ws})
        s.execute(
            text(
                "INSERT INTO datasets (id, workspace_id, name, source_type) "
                "VALUES (:id, :ws, 'orders', 'file_upload')"
            ),
            {"id": ds, "ws": ws},
        )
        s.execute(
            text(
                "INSERT INTO dataset_versions (id, workspace_id, dataset_id, version_no, "
                "source_sha256, content_hash, storage_uri, row_count, state) VALUES "
                "(:id, :ws, :ds, 1, :src, :hash, :uri, 7, 'READY')"
            ),
            {
                "id": dsv,
                "ws": ws,
                "ds": ds,
                "src": "sha256:" + "0" * 64,
                "hash": content_hash,
                "uri": key,
            },
        )
        s.execute(
            text(
                "INSERT INTO semantic_models (id, workspace_id, dataset_version_id, version, "
                "model, status) VALUES (:id, :ws, :dsv, 1, CAST(:model AS jsonb), 'active')"
            ),
            {"id": model.semantic_model_id, "ws": ws, "dsv": dsv, "model": model.model_dump_json()},
        )
    return {"ws": ws, "ds": ds, "dsv": dsv, "key": key, "hash": content_hash}


@pytest.fixture
def env(app_engine: Engine, tmp_path: Path) -> dict[str, Any]:
    uow = UnitOfWork(app_engine)
    root = tmp_path / "store"
    storage = LocalStorage(root)
    clock = FrozenClock(datetime(2026, 9, 28, tzinfo=UTC))
    registry = ToolRegistry(analytics_tools(DatasetSource(uow, storage), AnalyticalEngine()))
    executor = ToolExecutor(registry, uow, EvidenceRecorder(clock), UuidV7Ids())
    seeded = _seed(uow, storage)
    ctx = ToolContext(
        workspace_id=seeded["ws"],
        principal_id="usr_test",
        trace_id="trace-analytics",
        permissions=ALL,
        dataset_version_id=seeded["dsv"],
        semantic_model_version=1,
    )
    return {
        **seeded,
        "engine": app_engine,
        "root": root,
        "uow": uow,
        "storage": storage,
        "clock": clock,
        "registry": registry,
        "executor": executor,
        "ctx": ctx,
    }


def _run(env: dict[str, Any], tool: str, args: dict[str, Any], **kw: Any) -> ToolResult:
    key = CallKey(f"run_{uuid.uuid4().hex}", "s1")
    ctx = kw.get("ctx", env["ctx"])
    result: ToolResult = asyncio.run(
        env["executor"].run(tool, args, ctx, key, TurnBudget(max_calls=10))
    )
    return result


def _output(result: ToolResult) -> Any:
    assert result.status == "ok", result.error
    return result.output


def _evidence(env: dict[str, Any], evidence_id: str | None) -> Evidence:
    assert evidence_id is not None
    scope = WorkspaceScope(workspace_id=env["ws"], principal_id="usr_test")
    with env["uow"](scope) as s:
        evidence = EvidenceRepository(s).get(scope, evidence_id)
    assert evidence is not None
    return evidence


def test_metric_query_persists_full_evidence(env: dict[str, Any]) -> None:
    args = {"metric": "Revenue", "time": {"grain": "month", **AUG}, "compare_to": "previous_period"}
    result = _run(env, "analytics.metric_query", args)
    assert _output(result)["value"] == 540.0  # hand-computed (conftest): Aug revenue
    ev = _evidence(env, result.evidence_id)
    assert (ev.dataset_version_id, ev.dataset_id) == (env["dsv"], env["ds"])
    assert ev.dataset_content_hash == env["hash"]
    assert ev.semantic_model_version == 1
    assert ev.operation == "analytics.metric_query"
    assert ev.query is not None
    assert ev.query.engine == ENGINE_VERSION
    assert "$p0" in ev.query.sql
    assert "2026-08-01" not in ev.query.sql
    assert ev.query.params == {
        "p0": "2026-08-01",
        "p1": "2026-08-31",
        "p2": "2026-07-01",
        "p3": "2026-07-31",
    }
    assert (ev.result.value, ev.result.unit, ev.result.rows) == (540.0, "currency:INR", 1)
    assert ev.result.result_hash.startswith("sha256:")


def test_every_analytics_tool_records_evidence(env: dict[str, Any]) -> None:
    calls: dict[str, dict[str, Any]] = {
        "analytics.growth": {"metric": "Revenue", **AUG},
        "analytics.rank": {"metric": "Revenue", "dimension": "Region", "top_n": 1},
        "analytics.stats": {"metric": "Revenue", "by": "Region", "metric_b": "Quantity"},
        "analytics.trend": {"metric": "Revenue", "start": "2026-06-01", "end": "2026-08-31"},
        "analytics.explain_change": {
            "metric": "Revenue",
            "current_start": "2026-08-01",
            "current_end": "2026-08-31",
            "dimensions": ["Region"],
            "history_periods": 2,
        },
    }
    outputs: dict[str, Any] = {}
    for tool, args in calls.items():
        result = _run(env, tool, args)
        outputs[tool] = _output(result)
        ev = _evidence(env, result.evidence_id)
        assert ev.query is not None, tool
        assert ev.query.sql.startswith("SELECT "), tool
        assert 'FROM "data"' in ev.query.sql, tool
        assert ev.dataset_content_hash == env["hash"], tool
    assert outputs["analytics.growth"]["value"] == pytest.approx(195 / 345)  # 345 → 540, fraction
    assert outputs["analytics.growth"]["unit"] == "pct"
    assert outputs["analytics.rank"]["ranking"]["items"][0]["key"] == "North"  # 445 vs 440
    assert outputs["analytics.trend"]["series"]["missing_periods"] == ["2026-06-01"]
    rca = outputs["analytics.explain_change"]
    assert rca["value"] == 195.0
    assert rca["result"]["findings"][0]["relation_type"] == "contribution"


def test_growth_without_a_base_is_undefined(env: dict[str, Any]) -> None:
    args = {"metric": "Revenue", "start": "2026-07-01", "end": "2026-07-31"}
    out = _output(_run(env, "analytics.growth", args))
    assert out["value"] is None  # June has no rows: no base, never 0 % or inf
    assert out["growth"]["undefined"] == "MISSING_VALUE"


def test_other_workspace_cannot_read_the_dataset(env: dict[str, Any]) -> None:
    other = _seed(env["uow"], env["storage"])
    ctx = env["ctx"].model_copy(update={"dataset_version_id": other["dsv"]})
    result = _run(env, "analytics.metric_query", {"metric": "Revenue"}, ctx=ctx)
    assert result.error is not None
    assert (result.status, result.error.code) == ("error", "NOT_FOUND")


def test_tampered_file_is_refused(env: dict[str, Any]) -> None:
    path = env["root"] / env["key"]
    path.chmod(0o640)
    path.write_bytes(path.read_bytes() + b"x")
    result = _run(env, "analytics.metric_query", {"metric": "Revenue"})
    assert result.error is not None
    assert result.error.code == "SECURITY_ERROR"


def test_unknown_metric_is_a_schema_error_without_evidence(env: dict[str, Any]) -> None:
    result = _run(env, "analytics.metric_query", {"metric": "revenu"})
    assert (result.status, result.evidence_id) == ("error", None)
    assert result.error is not None
    assert result.error.code == "SCHEMA_ERROR"
    assert "Revenue" in result.error.message  # suggestion from the semantic model


@pytest.fixture
def api(env: dict[str, Any], config: AppConfig) -> Iterator[TestClient]:
    app = create_app(config)
    container = dataclasses.replace(
        app.state.container,
        clock=env["clock"],
        ids=UuidV7Ids(),
        engine=env["engine"],
        uow=env["uow"],
        storage=env["storage"],
        tools=env["registry"],
        tool_executor=env["executor"],
    )
    app.dependency_overrides[get_container] = lambda: container
    app.dependency_overrides[current_principal] = lambda: principal_for(
        WorkspaceScope(workspace_id=env["ws"], principal_id="usr_test")
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def test_api_query_returns_table_and_evidence(api: TestClient, env: dict[str, Any]) -> None:
    body = {
        "dataset_version_id": env["dsv"],
        "semantic_model_version": 1,
        "request": {
            "metric": "Revenue",
            "dimensions": ["Region"],
            "filters": [{"dimension": "Category", "op": "eq", "value": "Electronics"}],
        },
    }
    r = api.post("/api/v1/analytics/query", json=body)
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["table"]["rows"] == [["North", 300.0], ["South", 400.0]]  # drill-down
    ev = _evidence(env, payload["evidence_id"])
    assert ev.query is not None
    assert ev.query.params == {"p0": "Electronics"}
    with env["uow"](SystemScope("system:test")) as s:
        runs = s.execute(
            text("SELECT count(*) FROM tool_runs WHERE evidence_id = :ev"),
            {"ev": payload["evidence_id"]},
        ).scalar_one()
    assert runs == 1


def test_api_errors_are_mapped(api: TestClient, env: dict[str, Any]) -> None:
    base = {"dataset_version_id": env["dsv"], "semantic_model_version": 1}
    unknown = api.post("/api/v1/analytics/query", json={**base, "request": {"metric": "nope"}})
    assert (unknown.status_code, unknown.json()["error_code"]) == (422, "SCHEMA_ERROR")
    missing = api.post(
        "/api/v1/analytics/query",
        json={**base, "semantic_model_version": 9, "request": {"metric": "Revenue"}},
    )
    assert (missing.status_code, missing.json()["error_code"]) == (404, "NOT_FOUND")
