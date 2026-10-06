"""PlanExecutor (TASK-ANALYSIS-007): DAG order, fan-out, retries, timeout, cancel, resume."""

import asyncio
from collections.abc import Callable
from typing import Any, cast

from pydantic import BaseModel

from app.evidence.recorder import CallKey
from app.planner.domain import (
    AnalysisIntent,
    AnalysisPlan,
    AnalysisStep,
    ReasonCode,
    Unavailability,
)
from app.planner.executor import ExecutionSettings, PlanExecutor
from app.planner.runs import AnalysisRun, RunState, StepRecord, StepState
from app.tools.domain import ErrorInfo, ToolContext, ToolMetadata, ToolPermission, ToolResult
from app.tools.executor import ToolExecutor, TurnBudget
from app.tools.registry import ToolRegistry

CTX = ToolContext(
    workspace_id="ws_t", principal_id="p", trace_id="t", permissions=frozenset(ToolPermission)
)
FAST = ExecutionSettings(max_parallel=2, step_timeout_s=0.2, backoff_s=0, poll_s=0.01)
Script = Callable[[CallKey], ToolResult]


class _Tool:
    input_model = BaseModel
    output_model = BaseModel

    def __init__(self, name: str, parallel_safe: bool = True) -> None:
        self.meta = ToolMetadata(
            name=name,
            version="1.0.0",
            description=name,
            permission=ToolPermission.SQL_READ,
            side_effects="none",
            timeout_s=5,
            max_calls_per_turn=8,
            parallel_safe=parallel_safe,
            runs_in="api",
        )

    async def __call__(self, args: BaseModel, ctx: ToolContext) -> BaseModel:
        return args


def ok(key: CallKey) -> ToolResult:
    return ToolResult(
        tool="t.a",
        tool_version="1.0.0",
        status="ok",
        output={"value": 1},
        evidence_id=key.evidence_id,
        duration_ms=1,
    )


def err(retryable: bool) -> Script:
    error = ErrorInfo(code="X", message="x", retryable=retryable)
    return lambda _: ToolResult(
        tool="t.a", tool_version="1.0.0", status="error", error=error, duration_ms=1
    )


class FakeTools:
    """Plays a script per step: one result per attempt (the last one repeats)."""

    def __init__(self, scripts: dict[str, list[Script]], delay: float = 0.0) -> None:
        self.scripts = scripts
        self.delay = delay
        self.calls: list[tuple[str, int]] = []
        self.running = 0
        self.peak = 0

    async def run(
        self, name: str, args: dict[str, Any], ctx: ToolContext, key: CallKey, budget: TurnBudget
    ) -> ToolResult:
        self.calls.append((key.step_id, key.attempt))
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            await asyncio.sleep(float(args.get("sleep", self.delay)))
            script = self.scripts.get(key.step_id, [ok])
            return script[min(key.attempt, len(script)) - 1](key)
        finally:
            self.running -= 1


def plan(*steps: tuple[str, list[str]], **args: Any) -> AnalysisPlan:
    return AnalysisPlan(
        plan_id="pl_x",
        version=1,
        dataset_version_id="dsv_x",
        semantic_model_version=1,
        intent=AnalysisIntent(objective="overview", source="system_default"),
        steps=[
            AnalysisStep(
                id=i, type="aggregate", tool="t.a", args=args, depends_on=d, expected_output="X"
            )
            for i, d in steps
        ],
    )


def new_run(p: AnalysisPlan, **steps: StepRecord) -> AnalysisRun:
    return AnalysisRun(
        id="ar_x",
        plan_id=p.plan_id,
        dataset_version_id="dsv_x",
        semantic_model_version=1,
        state=RunState.RUNNING,
        steps=steps,
    )


def execute(
    tools: FakeTools,
    p: AnalysisPlan,
    run: AnalysisRun | None = None,
    cancelled: Callable[[], bool] = lambda: False,
    registry: ToolRegistry | None = None,
) -> tuple[RunState, dict[str, StepRecord], list[StepRecord]]:
    sink: list[StepRecord] = []
    registry = registry or ToolRegistry([_Tool("t.a")])
    executor = PlanExecutor(cast(ToolExecutor, tools), registry, FAST)
    coro = executor.execute(run or new_run(p), p, CTX, sink.append, cancelled)
    state, records = asyncio.run(coro)
    return state, records, sink


def test_dag_runs_dependencies_first_and_records_evidence() -> None:
    tools = FakeTools({})
    state, records, sink = execute(tools, plan(("S3", ["S1", "S2"]), ("S1", []), ("S2", [])))
    assert state is RunState.COMPLETED
    assert tools.calls[-1] == ("S3", 1)
    assert all(r.state is StepState.SUCCEEDED and r.evidence_id for r in records.values())
    assert records["S1"].evidence_id == CallKey("ar_x", "S1", 1).evidence_id
    assert [r.state for r in sink if r.step_id == "S3"] == [StepState.RUNNING, StepState.SUCCEEDED]


def test_retryable_errors_are_retried_deterministic_ones_are_not() -> None:
    tools = FakeTools({"S1": [err(True), ok], "S2": [err(False)]})
    state, records, _ = execute(tools, plan(("S1", []), ("S2", []), ("S3", ["S2"])))
    assert (records["S1"].state, records["S1"].attempts) == (StepState.SUCCEEDED, 2)
    assert (records["S2"].state, records["S2"].attempts) == (StepState.FAILED, 1)
    assert records["S2"].error is not None
    assert records["S3"].state is StepState.SKIPPED
    assert ("S3", 1) not in tools.calls
    assert state is RunState.PARTIAL


def test_retries_stop_at_the_limit_and_all_failed_is_failed() -> None:
    state, records, _ = execute(FakeTools({"S1": [err(True)]}), plan(("S1", [])))
    assert (records["S1"].state, records["S1"].attempts) == (StepState.FAILED, 3)
    assert state is RunState.FAILED


def test_step_timeout_is_a_retryable_tool_timeout() -> None:
    state, records, _ = execute(FakeTools({}), plan(("S1", []), sleep=1.0))
    assert records["S1"].error is not None
    assert records["S1"].error.code == "TOOL_TIMEOUT"
    assert records["S1"].attempts == 3
    assert state is RunState.FAILED


def test_unavailable_is_a_gap_not_a_failure() -> None:
    gap = Unavailability(
        analysis="forecast", reason_code=ReasonCode.INSUFFICIENT_HISTORY, message="m"
    )
    result = ToolResult(
        tool="t.a", tool_version="1.0.0", status="unavailable", unavailable=gap, duration_ms=1
    )
    state, records, _ = execute(FakeTools({"S1": [lambda _: result]}), plan(("S1", [])))
    assert records["S1"].state is StepState.UNAVAILABLE
    assert records["S1"].unavailable == gap
    assert state is RunState.COMPLETED


def test_fan_out_respects_max_parallel_and_exclusive_tools() -> None:
    tools = FakeTools({}, delay=0.03)
    execute(tools, plan(*[(f"S{i}", []) for i in range(6)]))
    assert tools.peak == 2
    serial = FakeTools({}, delay=0.03)
    registry = ToolRegistry([_Tool("t.a", parallel_safe=False)])
    execute(serial, plan(*[(f"S{i}", []) for i in range(3)]), registry=registry)
    assert serial.peak == 1
    assert len(serial.calls) == 3


def test_cancellation_stops_new_steps_and_marks_the_rest() -> None:
    tools = FakeTools({}, delay=0.05)
    state, records, _ = execute(
        tools, plan(("S1", []), ("S2", ["S1"])), cancelled=lambda: bool(tools.calls)
    )
    assert state is RunState.CANCELLED
    assert {r.state for r in records.values()} == {StepState.CANCELLED}
    assert tools.calls == [("S1", 1)]


def test_resumed_run_keeps_finished_steps_and_reuses_the_attempt() -> None:
    p = plan(("S1", []), ("S2", ["S1"]))
    done = StepRecord(
        step_id="S1", tool="t.a", state=StepState.SUCCEEDED, attempts=1, evidence_id="ev_old"
    )
    crashed = StepRecord(step_id="S2", tool="t.a", state=StepState.RUNNING, attempts=2)
    tools = FakeTools({})
    state, records, _ = execute(tools, p, new_run(p, S1=done, S2=crashed))
    assert tools.calls == [("S2", 2)]
    assert records["S1"].evidence_id == "ev_old"
    assert state is RunState.COMPLETED


def test_empty_plan_completes() -> None:
    state, records, _ = execute(FakeTools({}), plan())
    assert (state, records) == (RunState.COMPLETED, {})
