"""PlanExecutor (TASK-ANALYSIS-007, design §18).

Runs a ``ValidatedPlan`` as a DAG. Every step goes through the ``ToolExecutor`` (guards, tool_run
row and evidence), with the evidence key ``(run_id, step_id, attempt)``.

- Fan-out: ready steps run concurrently up to ``max_parallel``; a step whose tool is not
  ``parallel_safe`` runs alone.
- Retries only for ``ToolResult.error.retryable`` (timeouts, lost DB connections), at most
  ``max_retries`` with exponential backoff; deterministic errors fail at once.
- Per-step timeout around the tool call (the tool's own ``timeout_s`` still applies inside).
- A step whose dependency did not succeed is SKIPPED; a typed ``unavailable`` tool result is
  UNAVAILABLE (a gap the dashboard shows, not a failure).
- Cancellation: ``cancelled()`` is polled; no new steps start, in-flight steps are cancelled and
  every unfinished step is marked CANCELLED.
- Every transition is reported to ``on_step`` (persistence) before the next step starts.
Run state: COMPLETED when every step SUCCEEDED/UNAVAILABLE, CANCELLED when cancelled, PARTIAL when
at least one step succeeded, FAILED otherwise.
"""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from graphlib import TopologicalSorter

from app.evidence.recorder import CallKey
from app.planner.domain import AnalysisPlan, AnalysisStep
from app.planner.runs import FINISHED_STEPS, AnalysisRun, RunState, StepRecord, StepState
from app.tools.domain import ErrorInfo, ToolContext
from app.tools.executor import ToolExecutor, TurnBudget
from app.tools.registry import ToolRegistry

StepSink = Callable[[StepRecord], None]
TIMEOUT = ErrorInfo(code="TOOL_TIMEOUT", message="step timed out", retryable=True)


@dataclass(frozen=True)
class ExecutionSettings:
    max_parallel: int = 4
    step_timeout_s: float = 120.0
    max_retries: int = 2
    backoff_s: float = 0.5
    poll_s: float = 0.2  # how often cancellation is checked while steps are in flight


def run_state(steps: dict[str, StepRecord], cancelled: bool) -> RunState:
    states = [s.state for s in steps.values()]
    if cancelled:
        return RunState.CANCELLED
    if all(s in FINISHED_STEPS for s in states):
        return RunState.COMPLETED
    return RunState.PARTIAL if StepState.SUCCEEDED in states else RunState.FAILED


class PlanExecutor:
    def __init__(
        self,
        tools: ToolExecutor,
        registry: ToolRegistry,
        settings: ExecutionSettings | None = None,
    ) -> None:
        self._tools = tools
        self._registry = registry
        self._settings = settings or ExecutionSettings()

    async def execute(
        self,
        run: AnalysisRun,
        plan: AnalysisPlan,
        ctx: ToolContext,
        on_step: StepSink,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> tuple[RunState, dict[str, StepRecord]]:
        steps = {s.id: s for s in plan.steps}
        records = {
            s.id: run.steps.get(s.id) or StepRecord(step_id=s.id, tool=s.tool) for s in plan.steps
        }
        graph = TopologicalSorter({s.id: s.depends_on for s in plan.steps})
        graph.prepare()
        queue: list[str] = []
        in_flight: dict[asyncio.Task[StepRecord], str] = {}
        stopped = False

        async def settle(record: StepRecord) -> None:
            records[record.step_id] = record
            await asyncio.to_thread(on_step, record)
            graph.done(record.step_id)

        while True:
            queue += graph.get_ready()
            stopped = stopped or cancelled()
            while queue and not stopped and len(in_flight) < self._settings.max_parallel:
                sid = queue[0]
                record = records[sid]
                if record.state in FINISHED_STEPS:  # resumed run: keep the earlier result
                    queue.pop(0)
                    graph.done(sid)
                    continue
                if any(records[d].state is not StepState.SUCCEEDED for d in steps[sid].depends_on):
                    queue.pop(0)
                    await settle(record.model_copy(update={"state": StepState.SKIPPED}))
                    continue
                exclusive = not self._parallel_safe(steps[sid])
                if in_flight and (exclusive or self._exclusive_running(in_flight, steps)):
                    break
                queue.pop(0)
                task = asyncio.create_task(self._run_step(steps[sid], record, run.id, ctx, on_step))
                in_flight[task] = sid
            if not in_flight:
                if stopped or not graph.is_active():
                    break
                continue  # settled skips/resumed steps made new steps ready
            done, _ = await asyncio.wait(
                in_flight, timeout=self._settings.poll_s, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                in_flight.pop(task)
                await settle(task.result())
            if cancelled() and not stopped:
                stopped = True
                for task in in_flight:
                    task.cancel()
                await asyncio.gather(*in_flight, return_exceptions=True)
                in_flight.clear()
        if stopped:
            for sid, record in records.items():
                if record.state not in FINISHED_STEPS | {StepState.FAILED, StepState.SKIPPED}:
                    records[sid] = record.model_copy(update={"state": StepState.CANCELLED})
                    await asyncio.to_thread(on_step, records[sid])
        return run_state(records, stopped), records

    def _parallel_safe(self, step: AnalysisStep) -> bool:
        return self._registry.get(step.tool).meta.parallel_safe

    def _exclusive_running(
        self, in_flight: dict[asyncio.Task[StepRecord], str], steps: dict[str, AnalysisStep]
    ) -> bool:
        return any(not self._parallel_safe(steps[sid]) for sid in in_flight.values())

    async def _run_step(
        self,
        step: AnalysisStep,
        record: StepRecord,
        run_id: str,
        ctx: ToolContext,
        on_step: StepSink,
    ) -> StepRecord:
        started = time.perf_counter()
        attempt = max(record.attempts, 1)  # a RUNNING step of a crashed run keeps its attempt
        last = self._settings.max_retries + 1
        extra: dict[str, object]
        while True:
            running = record.model_copy(update={"state": StepState.RUNNING, "attempts": attempt})
            await asyncio.to_thread(on_step, running)
            key = CallKey(execution_id=run_id, step_id=step.id, attempt=attempt)
            try:
                async with asyncio.timeout(self._settings.step_timeout_s):
                    result = await self._tools.run(
                        step.tool, dict(step.args), ctx, key, TurnBudget(max_calls=1)
                    )
                error = result.error
            except TimeoutError:
                result, error = None, TIMEOUT
            elapsed = int((time.perf_counter() - started) * 1000)
            if result is not None and result.status == "ok":
                state, extra = StepState.SUCCEEDED, {"evidence_id": result.evidence_id}
            elif result is not None and result.status == "unavailable":
                state, extra = StepState.UNAVAILABLE, {"unavailable": result.unavailable}
            elif error is not None and error.retryable and attempt < last:
                await asyncio.sleep(self._settings.backoff_s * 2 ** (attempt - 1))
                attempt += 1
                continue
            else:
                state, extra = StepState.FAILED, {"error": error}
            return running.model_copy(update={"state": state, "duration_ms": elapsed, **extra})
