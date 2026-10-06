"""TASK-EVID-002/003 + TASK-TOOL-004: the executor guard chain and append-only evidence."""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine, text

from app.core.clock import FrozenClock
from app.core.ids import UuidV7Ids
from app.core.scope import SystemScope, WorkspaceScope
from app.evidence.recorder import CallKey, EvidenceRecorder
from app.evidence.repository import EvidenceRepository
from app.infra.db import UnitOfWork
from app.planner.domain import ReasonCode, Unavailability
from app.tools.domain import ToolContext, ToolMetadata, ToolPermission, ToolResult
from app.tools.executor import ToolExecutor, TurnBudget
from app.tools.registry import ToolRegistry

CONTENT_HASH = "sha256:" + "ab" * 32
INSERT_DATASET = text(
    "INSERT INTO datasets (id, workspace_id, name, source_type) VALUES (:id, :ws, 'orders', 'csv')"
)
INSERT_VERSION = text(
    "INSERT INTO dataset_versions (id, workspace_id, dataset_id, version_no, source_sha256, "
    "content_hash, state) VALUES (:id, :ws, :ds, 1, :src, :hash, 'INGESTED')"
)


class SumArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    column: str
    limit: int = 10


class SumOut(BaseModel):
    value: float
    unit: str
    rows: int


def _meta(name: str, *, timeout_s: float = 2, max_calls: int = 2) -> ToolMetadata:
    return ToolMetadata(
        name=name,
        version="1.0.0",
        description="deterministic test tool",
        permission=ToolPermission.SQL_READ,
        side_effects="none",
        timeout_s=timeout_s,
        max_calls_per_turn=max_calls,
        parallel_safe=True,
        runs_in="api",
    )


class SumTool:
    meta = _meta("analytics.test_sum")
    input_model = SumArgs
    output_model = SumOut

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, args: BaseModel, ctx: ToolContext) -> BaseModel | Unavailability:
        self.calls += 1
        return SumOut(value=42.5, unit="count", rows=3)


class SlowTool(SumTool):
    meta = _meta("analytics.test_slow", timeout_s=0.05)

    async def __call__(self, args: BaseModel, ctx: ToolContext) -> BaseModel | Unavailability:
        await asyncio.sleep(1)
        return SumOut(value=0, unit="count", rows=0)


class RefusingTool(SumTool):
    meta = _meta("analytics.test_refuse")

    async def __call__(self, args: BaseModel, ctx: ToolContext) -> BaseModel | Unavailability:
        return Unavailability(
            analysis="churn",
            reason_code=next(iter(ReasonCode)),
            message="no customer identifier",
        )


@pytest.fixture
def env(app_engine: Engine) -> dict[str, Any]:
    uow = UnitOfWork(app_engine)
    ws = f"ws_{uuid.uuid4().hex[:12]}"
    dsv = f"dsv_{uuid.uuid4().hex[:12]}"
    ds = f"ds_{uuid.uuid4().hex[:12]}"
    with uow(SystemScope("system:test")) as s:
        s.execute(text("INSERT INTO workspaces (id, name) VALUES (:id, 'ev')"), {"id": ws})
        s.execute(INSERT_DATASET, {"id": ds, "ws": ws})
        s.execute(
            INSERT_VERSION,
            {"id": dsv, "ws": ws, "ds": ds, "src": "sha256:" + "cd" * 32, "hash": CONTENT_HASH},
        )
    sum_tool = SumTool()
    registry = ToolRegistry([sum_tool, SlowTool(), RefusingTool()])  # type: ignore[list-item]
    clock = FrozenClock(datetime(2026, 9, 28, tzinfo=UTC))
    executor = ToolExecutor(registry, uow, EvidenceRecorder(clock), UuidV7Ids())
    ctx = ToolContext(
        workspace_id=ws,
        principal_id="usr_test",
        trace_id="trace-evidence01",
        permissions=frozenset({ToolPermission.SQL_READ}),
        dataset_version_id=dsv,
    )
    scope = WorkspaceScope(workspace_id=ws, principal_id="usr_test")
    return {
        "uow": uow,
        "executor": executor,
        "ctx": ctx,
        "tool": sum_tool,
        "scope": scope,
        "ds": ds,
    }


def _run(env: dict[str, Any], name: str, args: dict[str, Any], **kw: Any) -> ToolResult:
    key = kw.pop("key", CallKey(f"run_{uuid.uuid4().hex}", "step_1"))
    budget = kw.pop("budget", TurnBudget(max_calls=10))
    ctx = kw.pop("ctx", env["ctx"])
    result: ToolResult = asyncio.run(env["executor"].run(name, args, ctx, key, budget))
    return result


def _count(env: dict[str, Any], sql: str) -> int:
    with env["uow"](env["scope"]) as s:
        return int(s.execute(text(sql), {"ws": env["scope"].workspace_id}).scalar_one())


def test_ok_call_records_evidence_and_tool_run(env: dict[str, Any]) -> None:
    key = CallKey("run_1", "step_revenue")
    result = _run(env, "analytics.test_sum", {"column": "amount"}, key=key)
    assert result.status == "ok"
    assert result.output == {"value": 42.5, "unit": "count", "rows": 3}
    assert result.evidence_id == key.evidence_id
    with env["uow"](env["scope"]) as s:
        evidence = EvidenceRepository(s).get(env["scope"], key.evidence_id)
    assert evidence is not None
    assert evidence.dataset_id == env["ds"]
    assert evidence.dataset_content_hash == CONTENT_HASH  # from the DB, not the tool
    assert evidence.arguments == {"column": "amount", "limit": 10}
    assert (evidence.result.value, evidence.result.rows) == (42.5, 3)
    runs = "SELECT count(*) FROM tool_runs WHERE workspace_id = :ws AND evidence_id IS NOT NULL"
    assert _count(env, runs) == 1


def test_rerun_with_same_key_does_not_duplicate_evidence(env: dict[str, Any]) -> None:
    key = CallKey("run_2", "step_a", attempt=1)
    first = _run(env, "analytics.test_sum", {"column": "amount"}, key=key)
    again = _run(env, "analytics.test_sum", {"column": "amount"}, key=key)
    assert first.evidence_id == again.evidence_id
    assert _count(env, "SELECT count(*) FROM evidence WHERE workspace_id = :ws") == 1
    assert _count(env, "SELECT count(*) FROM tool_runs WHERE workspace_id = :ws") == 2


def test_permission_denied_never_executes_but_is_recorded(env: dict[str, Any]) -> None:
    ctx = env["ctx"].model_copy(update={"permissions": frozenset()})
    result = _run(env, "analytics.test_sum", {"column": "amount"}, ctx=ctx)
    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "AUTH_ERROR"
    assert env["tool"].calls == 0
    assert _count(env, "SELECT count(*) FROM tool_runs WHERE workspace_id = :ws") == 1
    assert _count(env, "SELECT count(*) FROM evidence WHERE workspace_id = :ws") == 0


@pytest.mark.parametrize(
    "args",
    [{"column": "amount", "limit": "5"}, {"column": "amount", "drop": True}, {}],
)
def test_strict_argument_validation(env: dict[str, Any], args: dict[str, Any]) -> None:
    result = _run(env, "analytics.test_sum", args)
    assert result.error is not None
    assert result.error.code == "TOOL_ERROR"
    assert result.error.message.startswith("invalid tool arguments")
    assert env["tool"].calls == 0


def test_unknown_tool_is_an_error_result(env: dict[str, Any]) -> None:
    result = _run(env, "analytics.does_not_exist", {})
    assert (result.status, result.tool) == ("error", "analytics.does_not_exist")
    garbage = _run(env, "DROP TABLE x", {})
    assert garbage.tool == "unknown.tool"


def test_turn_budget_limits_calls(env: dict[str, Any]) -> None:
    budget = TurnBudget(max_calls=10)
    errors = [_run(env, "analytics.test_sum", {"column": "a"}, budget=budget).error for _ in "abc"]
    assert errors[:2] == [None, None]
    assert errors[2] is not None
    assert errors[2].code == "RATE_LIMITED"  # max_calls_per_turn = 2


def test_timeout(env: dict[str, Any]) -> None:
    result = _run(env, "analytics.test_slow", {"column": "a"})
    assert result.error is not None
    assert (result.error.code, result.error.retryable) == ("TOOL_TIMEOUT", True)


def test_unavailable_is_recorded_without_evidence(env: dict[str, Any]) -> None:
    result = _run(env, "analytics.test_refuse", {"column": "a"})
    assert result.status == "unavailable"
    assert result.evidence_id is None
    status = "SELECT count(*) FROM tool_runs WHERE workspace_id = :ws AND status = 'unavailable'"
    assert _count(env, status) == 1


def test_evidence_repository_has_no_mutation_methods() -> None:
    public = {n for n in vars(EvidenceRepository) if not n.startswith("_")}
    assert public == {"insert", "get", "get_many", "rows"}  # rows: read-only result preview
