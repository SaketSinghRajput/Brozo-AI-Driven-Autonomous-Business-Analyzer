"""TASK-TOOL-009 (executor attacks are denied) and TASK-TOOL-005 (error mapping + retry policy).

Runs the real ToolExecutor guard chain (MemoryExecutor swaps only persistence)."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import OperationalError

from app.analytics.sql.engine import AnalyticalEngine
from app.analytics.tools import analytics_tools
from app.evidence.recorder import CallKey
from app.orchestrator.domain import TurnLimits, TurnState
from app.orchestrator.state import TurnStateMachine
from app.orchestrator.tool_loop import TurnTools
from app.planner.domain import Filter
from app.tools.domain import ToolContext, ToolMetadata, ToolPermission, ToolResult
from app.tools.executor import TurnBudget
from app.tools.permissions import Role, TurnProfile, effective_permissions
from app.tools.registry import ToolRegistry
from tests.unit.analytics.conftest import semantic_model, small_orders
from tests.unit.orchestrator.fakes import MemoryExecutor, MemorySource

MODEL = semantic_model()
CHAT = effective_permissions(Role.ANALYST, TurnProfile.CHAT_DEFAULT)


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Out(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    value: float


class FakeTool:
    input_model: type[BaseModel] = NoArgs
    output_model: type[BaseModel] = Out

    def __init__(
        self,
        name: str,
        behaviour: Callable[[], Awaitable[BaseModel]],
        permission: ToolPermission = ToolPermission.PYTHON_ANALYTICS,
        timeout_s: float = 1.0,
    ) -> None:
        self.meta = ToolMetadata(
            name=name,
            version="1.0.0",
            description="test tool",
            permission=permission,
            side_effects="none",
            timeout_s=timeout_s,
            max_calls_per_turn=8,
            parallel_safe=True,
            runs_in="api",
        )
        self._behaviour = behaviour
        self.calls = 0

    async def __call__(self, args: BaseModel, ctx: ToolContext) -> BaseModel:
        self.calls += 1
        return await self._behaviour()


async def _ok() -> BaseModel:
    return Out(value=1.0)


async def _slow() -> BaseModel:
    await asyncio.sleep(1)
    return Out(value=1.0)


async def _bad_internal_request() -> BaseModel:
    return Filter.model_validate({"dimension": "d", "op": "in", "value": []})


async def _db_down() -> BaseModel:
    raise OperationalError("SELECT 1", {}, Exception("connection lost"))


async def _crash() -> BaseModel:
    raise RuntimeError("boom with /secret/path")


def _setup(*tools: FakeTool) -> tuple[MemoryExecutor, ToolContext]:
    source = MemorySource(MODEL, small_orders())
    registry = ToolRegistry([*analytics_tools(source, AnalyticalEngine()), *tools])
    ctx = ToolContext(
        workspace_id="ws_abcdef",
        principal_id="usr_1",
        trace_id="t",
        permissions=CHAT,
        dataset_version_id=MODEL.dataset_version_id,
        semantic_model_version=1,
    )
    return MemoryExecutor(registry), ctx


def _run(
    executor: MemoryExecutor,
    ctx: ToolContext,
    name: str,
    args: dict[str, Any],
    budget: TurnBudget | None = None,
) -> ToolResult:
    key = CallKey("turn_1", f"s{len(executor.runs)}")
    return asyncio.run(executor.run(name, args, ctx, key, budget or TurnBudget(10)))


# --- TOOL-009: attacks ---------------------------------------------------------------------


def test_forbidden_tool_is_denied_and_never_runs() -> None:
    code_tool = FakeTool("repair.run_code", _ok, ToolPermission.CODE_EXECUTION)
    executor, ctx = _setup(code_tool)
    result = _run(executor, ctx, "repair.run_code", {})
    assert result.error is not None
    assert (result.status, result.error.code) == ("error", "AUTH_ERROR")
    assert code_tool.calls == 0
    assert result.evidence_id is None


def test_unknown_tool_is_rejected() -> None:
    executor, ctx = _setup()
    result = _run(executor, ctx, "admin.drop_everything", {})
    assert result.error is not None
    assert (result.error.code, result.error.retryable) == ("TOOL_ERROR", False)


@pytest.mark.parametrize(
    "args",
    [
        {"metric": "Revenue", "sql": "DROP TABLE evidence"},  # smuggled extra field
        {"metric": "Revenue", "limit": "10"},  # no silent coercion (strict)
        {"metric": ["Revenue"]},
        {"metric": "Revenue", "filters": [{"dimension": "Region", "op": "eq", "value": {}}]},
        {},
    ],
)
def test_argument_smuggling_is_rejected_before_the_tool_runs(args: dict[str, Any]) -> None:
    executor, ctx = _setup()
    result = _run(executor, ctx, "analytics.metric_query", args)
    assert result.error is not None
    assert (result.status, result.error.code, result.error.retryable) == (
        "error",
        "TOOL_ERROR",
        False,
    )
    assert executor.evidence == {}


def test_sql_in_names_and_values_never_reaches_the_query_text() -> None:
    executor, ctx = _setup()
    named = _run(executor, ctx, "analytics.metric_query", {"metric": "Revenue'; DROP TABLE x;--"})
    assert named.error is not None
    assert named.error.code == "SCHEMA_ERROR"
    evil = "North' OR '1'='1"
    args = {"metric": "Revenue", "filters": [{"dimension": "Region", "op": "eq", "value": evil}]}
    valued = _run(executor, ctx, "analytics.metric_query", args)
    assert valued.status == "ok"
    assert valued.output is not None
    query: Any = valued.output["query"]
    assert evil not in query["sql"]
    assert evil in query["params"].values()


def test_budget_exhaustion_denies_further_calls() -> None:
    tool = FakeTool("test.ok", _ok)
    executor, ctx = _setup(tool)
    budget = TurnBudget(2)
    results = [_run(executor, ctx, "test.ok", {}, budget) for _ in range(3)]
    assert [r.status for r in results] == ["ok", "ok", "error"]
    assert results[2].error is not None
    assert (results[2].error.code, results[2].error.retryable) == ("RATE_LIMITED", False)
    assert tool.calls == 2


def test_viewer_cannot_run_model_tools_in_chat() -> None:
    model_tool = FakeTool("ml.fit", _ok, ToolPermission.MODEL_RUN)
    executor, ctx = _setup(model_tool)
    viewer = ctx.model_copy(
        update={"permissions": effective_permissions(Role.VIEWER, TurnProfile.CHAT_DEFAULT)}
    )
    assert _run(executor, viewer, "ml.fit", {}).status == "error"
    assert _run(executor, ctx, "ml.fit", {}).status == "ok"  # analyst may
    assert model_tool.calls == 1


# --- TOOL-005: error mapping and retry policy ---------------------------------------------


@pytest.mark.parametrize(
    ("behaviour", "code", "retryable"),
    [
        (_slow, "TOOL_TIMEOUT", True),
        (_db_down, "DATABASE_ERROR", True),
        (_bad_internal_request, "TOOL_ERROR", False),
        (_crash, "TOOL_ERROR", False),
    ],
)
def test_error_mapping(
    behaviour: Callable[[], Awaitable[BaseModel]], code: str, retryable: bool
) -> None:
    executor, ctx = _setup(FakeTool("test.flaky", behaviour, timeout_s=0.05))
    result = _run(executor, ctx, "test.flaky", {})
    assert result.error is not None
    assert (result.error.code, result.error.retryable) == (code, retryable)
    assert "/secret" not in result.error.message


@pytest.mark.parametrize(("behaviour", "attempts"), [(_db_down, 2), (_crash, 1)])
def test_turn_retries_only_retryable_errors(
    behaviour: Callable[[], Awaitable[BaseModel]], attempts: int
) -> None:
    tool = FakeTool("test.flaky", behaviour)
    executor, ctx = _setup(tool)
    machine = TurnStateMachine(TurnLimits())
    for state in (TurnState.CLASSIFYING, TurnState.PLANNING, TurnState.EXECUTING):
        machine.to(state)
    tools = TurnTools(executor, ctx, "msg_1", machine, TurnBudget(10))
    event = asyncio.run(tools.call("S1", "test.flaky", {}))
    assert event is not None
    assert event.status == "error"
    assert tool.calls == attempts
    assert machine.tool_calls == 1  # a retry is not a new call for the loop budget
