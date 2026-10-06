"""ML tools end to end on PostgreSQL as ``analyzer_app`` (RLS on): ToolExecutor → ML tool →
model_runs row + evidence (dataset version, arguments, calculation naming model + run)."""

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text

from app.core.clock import FrozenClock
from app.core.ids import UuidV7Ids
from app.core.scope import SystemScope, WorkspaceScope
from app.evidence.recorder import CallKey, EvidenceRecorder
from app.evidence.repository import EvidenceRepository
from app.infra.db import UnitOfWork
from app.infra.storage import LocalStorage
from app.ml.repository import ModelRunRepository
from app.tools.domain import ToolContext, ToolPermission, ToolResult
from app.tools.executor import ToolExecutor, TurnBudget
from app.tools.registry import ToolRegistry, default_tools
from tests.integration.conftest import TestDatabase
from tests.unit.ml.conftest import ingest_to

INSERT_VERSION = text(
    "INSERT INTO dataset_versions (id, workspace_id, dataset_id, version_no, source_sha256, "
    "content_hash, storage_uri, row_count, state) "
    "VALUES (:id, :ws, :ds, 1, :src, :hash, :key, 1, 'PROFILED')"
)


def _version(uow: UnitOfWork, storage: LocalStorage, tmp: Path, ws: str, fixture: str) -> str:
    dsv, ds = f"dsv_{uuid.uuid4().hex[:12]}", f"ds_{uuid.uuid4().hex[:12]}"
    key = f"{dsv}.parquet"
    ingest_to(storage, tmp / dsv, fixture, key)
    with uow(WorkspaceScope(workspace_id=ws, principal_id="usr_ml")) as s:
        s.execute(
            text(
                "INSERT INTO datasets (id, workspace_id, name, source_type) "
                "VALUES (:id, :ws, 'ml', 'file_upload')"
            ),
            {"id": ds, "ws": ws},
        )
        s.execute(
            INSERT_VERSION,
            {
                "id": dsv,
                "ws": ws,
                "ds": ds,
                "src": "sha256:" + uuid.uuid4().hex * 2,
                "hash": "sha256:" + "ab" * 32,
                "key": key,
            },
        )
    return dsv


@pytest.fixture
def env(app_engine: Engine, database: TestDatabase, tmp_path: Path) -> Iterator[dict[str, Any]]:
    uow = UnitOfWork(app_engine)
    ws = f"ws_{uuid.uuid4().hex[:12]}"
    with uow(SystemScope("system:test")) as s:
        s.execute(text("INSERT INTO workspaces (id, name) VALUES (:id, 'ml')"), {"id": ws})
    storage = LocalStorage(tmp_path / "store")
    ids = UuidV7Ids()
    registry = ToolRegistry(default_tools(uow, storage, ids))
    clock = FrozenClock(datetime(2026, 9, 28, tzinfo=UTC))
    yield {
        "uow": uow,
        "ws": ws,
        "scope": WorkspaceScope(workspace_id=ws, principal_id="usr_ml"),
        "executor": ToolExecutor(registry, uow, EvidenceRecorder(clock), ids),
        "retail": _version(uow, storage, tmp_path, ws, "retail/orders.csv"),
        "short": _version(uow, storage, tmp_path, ws, "insufficient/sales.csv"),
    }
    # The shared test database is later downgraded by test_database; migration 0005's downgrade
    # cannot handle 'unavailable' tool runs, so the owner removes this test's refusal rows.
    owner = create_engine(database.owner_url)
    with owner.begin() as conn:
        conn.execute(text("DELETE FROM tool_runs WHERE workspace_id = :ws"), {"ws": ws})
    owner.dispose()


def _run(env: dict[str, Any], name: str, args: dict[str, Any], dsv: str) -> ToolResult:
    ctx = ToolContext(
        workspace_id=env["ws"],
        principal_id="usr_ml",
        trace_id="trace-ml",
        permissions=frozenset({ToolPermission.MODEL_RUN}),
        dataset_version_id=dsv,
    )
    key = CallKey(f"run_{uuid.uuid4().hex}", "step_ml")
    result: ToolResult = asyncio.run(
        env["executor"].run(name, args, ctx, key, TurnBudget(max_calls=10))
    )
    return result


def test_anomaly_tool_persists_model_run_and_evidence(env: dict[str, Any]) -> None:
    args = {"metric": "quantity", "id_column": "order_id"}
    result = _run(env, "ml.anomalies", args, env["retail"])
    assert result.status == "ok", result.error
    assert result.output is not None
    run_id = str(result.output["model_run_id"])
    scope = env["scope"]
    with env["uow"](scope) as s:
        run = ModelRunRepository(s).get(scope, run_id)
        evidence = EvidenceRepository(s).get(scope, str(result.evidence_id))
    assert run is not None
    assert run.params["dataset_version_id"] == env["retail"]
    assert run.metrics.values["flagged"] == 1
    assert evidence is not None
    assert evidence.dataset_version_id == env["retail"]
    assert evidence.arguments["metric"] == "quantity"
    assert evidence.calculation is not None
    assert run_id in evidence.calculation
    assert "robust_z (analyzer@" in evidence.calculation
    assert evidence.result.rows == result.output["rows"]
    # GAPS-6: evidence -> model run, and model run -> evidence, both resolve
    assert evidence.model is not None
    assert evidence.model.model_run_id == run_id
    assert evidence.model.model_id == "robust_z"
    assert evidence.model.model_version is not None
    assert evidence.model.model_version.startswith("analyzer@")
    assert run.evidence_id == result.evidence_id
    other = WorkspaceScope(workspace_id=f"ws_{uuid.uuid4().hex[:12]}", principal_id="usr_x")
    with env["uow"](other) as s:  # RLS: another workspace cannot see the run
        assert ModelRunRepository(s).get(other, run_id) is None


def test_forecast_refusal_is_recorded_without_model_run(env: dict[str, Any]) -> None:
    args = {"time_column": "date", "metric": "quantity", "horizon": 3, "grain": "month"}
    result = _run(env, "ml.forecast", args, env["short"])
    assert result.status == "unavailable"
    assert result.unavailable is not None
    assert result.unavailable.reason_code == "INSUFFICIENT_HISTORY"
    with env["uow"](env["scope"]) as s:
        n = s.execute(text("SELECT count(*) FROM model_runs")).scalar_one()
        runs = s.execute(text("SELECT status FROM tool_runs")).scalars().all()
    assert n == 0
    assert runs == ["unavailable"]


def test_forecast_tool_ok_with_estimate_label(env: dict[str, Any]) -> None:
    args = {
        "time_column": "order_date",
        "metric": "quantity",
        "multiply_by": ["unit_price"],
        "horizon": 3,
    }
    result = _run(env, "ml.forecast", args, env["retail"])
    assert result.status == "ok", result.error
    assert result.output is not None
    forecast = result.output["forecast"]
    assert isinstance(forecast, dict)
    assert forecast["label"] == "estimate"
    with env["uow"](env["scope"]) as s:
        payload = s.execute(
            text("SELECT payload FROM model_runs WHERE id = :id"),
            {"id": result.output["model_run_id"]},
        ).scalar_one()
    assert payload["selection"]["margin_rule"]
    assert payload["metrics"]["baseline"]["name"] == "naive"
