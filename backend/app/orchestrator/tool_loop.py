"""Turn tool execution and the bounded ToolLoop (TASK-AGENT-004, TASK-TOOL-005; design §34).

``TurnTools`` is the only way a turn runs a tool: it goes through ``ToolExecutor`` (registry,
strict args, permission, budget, evidence), applies the loop detector, and retries a failed
call once only when ``ToolResult.error.retryable`` says so (deterministic errors never repeat).

``ToolLoop`` lets the LLM ask for follow-up calls (drill-downs) after the plan ran. The LLM only
sees tools inside the turn's effective permissions; calls to anything else are rejected by the
gateway and never executed. Tool results go back to the model as untrusted, labelled data
(``ContextBuilder`` / ``wrap_untrusted``, ADR-0024), never in the system role. The loop stops
when the model stops calling tools, after ``max_loop_rounds``, when the tool budget is spent,
or when the loop detector sees an identical call again. Calls to a tool the plan already answered
are not run (B-4).
"""

import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from app.analytics.tools.analysis import ExplainChangeOutput
from app.evidence.recorder import CallKey
from app.llm.context import BuiltContext, ContextBuilder, ContextPart
from app.llm.gateway import LLMGateway
from app.llm.ports import LLMTask, Message
from app.llm.prompts import PromptRegistry
from app.llm.untrusted import InjectionHeuristics, wrap_untrusted
from app.orchestrator.domain import (
    MAX_STREAMED_FINDINGS,
    FindingView,
    ToolCallEvent,
    TurnState,
)
from app.orchestrator.state import TurnStateMachine
from app.tools.domain import ToolContext, ToolResult, ToolSpecForLLM
from app.tools.executor import ToolExecutor, TurnBudget

PROMPT = "tool_loop.select_tool"
CONTEXT_BUDGET = 6000
MAX_RESULT_CHARS = 1500
MAX_ATTEMPTS = 2  # one retry, only for retryable errors
REJECTED_NAME = "unknown.tool"  # a rejected name is untrusted model output; it is not echoed
EXPLAIN_CHANGE = "analytics.explain_change"
_ARG_NAME = re.compile(r"^[a-z_]{1,40}$")


@dataclass(frozen=True, slots=True)
class ExecutedCall:
    step_id: str
    name: str
    args: dict[str, Any]
    result: ToolResult


def args_summary(args: dict[str, Any]) -> str:
    """Argument names only (values can be untrusted text); odd names are masked."""
    return ", ".join(k if _ARG_NAME.match(k) else "?" for k in sorted(args))[:200]


def result_payload(call: ExecutedCall) -> str:
    r = call.result
    body: dict[str, Any] = {"tool": r.tool, "status": r.status, "evidence_id": r.evidence_id}
    if r.output is not None:
        body["output"] = r.output
    if r.error is not None:
        body["error"] = {"code": r.error.code, "message": r.error.message}
    if r.unavailable is not None:
        body["unavailable"] = r.unavailable.model_dump(mode="json")
    text = json.dumps(body, default=str, ensure_ascii=False)
    return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + "...(truncated)"


@dataclass
class TurnTools:
    executor: ToolExecutor
    ctx: ToolContext
    execution_id: str  # the turn's message id: evidence ids derive from it (design §74)
    machine: TurnStateMachine
    budget: TurnBudget
    calls: list[ExecutedCall] = field(default_factory=list)

    async def call(self, step_id: str, name: str, args: dict[str, Any]) -> ToolCallEvent | None:
        """Run one tool call; None when the loop detector refuses a repeated call."""
        if not self.machine.record_call(name, args):
            return None
        self.machine.to(TurnState.WAITING_FOR_TOOL)
        attempt = 1
        while True:
            key = CallKey(self.execution_id, step_id, attempt)
            run = self.executor.run(name, args, self.ctx, key, self.budget)
            result = await self.machine.bounded(run)
            if result.error is None or not result.error.retryable or attempt >= MAX_ATTEMPTS:
                break
            attempt += 1
        self.machine.to(TurnState.EXECUTING)
        self.calls.append(ExecutedCall(step_id, name, args, result))
        return ToolCallEvent(
            name=result.tool,
            args_summary=args_summary(args),
            status=result.status,
            evidence_id=result.evidence_id,
            error_code=result.error.code if result.error else None,
            findings=_findings(result),
        )


def _findings(result: ToolResult) -> list[FindingView] | None:
    if result.tool != EXPLAIN_CHANGE or result.status != "ok" or result.output is None:
        return None
    found = ExplainChangeOutput.model_validate(result.output).result.findings
    return [
        FindingView(
            factor=f.factor,
            change=f.magnitude,
            unit=f.unit,
            share=f.share_pct,
            relation_type=f.relation_type,
        )
        for f in found[:MAX_STREAMED_FINDINGS]
    ]


class ToolLoop:
    def __init__(
        self,
        gateway: LLMGateway,
        prompts: PromptRegistry,
        context: ContextBuilder,
        heuristics: InjectionHeuristics | None = None,
    ) -> None:
        self._gateway = gateway
        self._prompts = prompts
        self._context = context
        self._heuristics = heuristics or InjectionHeuristics()

    def _initial(self, question: str, calls: list[ExecutedCall]) -> BuiltContext:
        parts = [
            ContextPart(
                kind="tool_results",
                content=result_payload(c),
                trust="untrusted",
                provenance=[c.result.evidence_id or c.step_id],
            )
            for c in calls
        ]
        return self._context.build(
            self._prompts.get(PROMPT), {"question": question}, parts, CONTEXT_BUDGET
        )

    def _feed_back(self, ctx: BuiltContext, calls: list[ExecutedCall]) -> BuiltContext:
        added = []
        for c in calls:
            payload = result_payload(c)
            wrapped = wrap_untrusted(
                payload,
                kind="tool_results",
                sources=[c.result.evidence_id or c.step_id],
                flags=self._heuristics.scan(payload),
            )
            added.append(Message(role="user", content=wrapped, trust="untrusted"))
        tokens = ctx.tokens + sum(len(m.content) // 3 + 4 for m in added)
        return ctx.model_copy(update={"messages": [*ctx.messages, *added], "tokens": tokens})

    async def run(
        self, question: str, tools: TurnTools, specs: list[ToolSpecForLLM]
    ) -> AsyncIterator[ToolCallEvent]:
        machine = tools.machine
        ctx = self._initial(question, tools.calls)
        # B-4: a model asked for follow-ups re-called the plan's tool with partial arguments in
        # most live turns (TOOL_ERROR/DATA_ERROR noise). A tool the plan already answered is not
        # run again. ponytail: this also drops same-tool drill-downs (e.g. metric_query by a
        # dimension); allow them per argument set if a real question needs one.
        answered = {c.name for c in tools.calls if c.result.status == "ok"}
        for round_no in range(1, machine.limits.max_loop_rounds + 1):
            if not machine.can_call_tool():
                return
            machine.charge_tokens(ctx.tokens)
            step = await machine.bounded(self._gateway.tool_step(LLMTask.TOOL_LOOP, specs, ctx))
            spent = sum(len(json.dumps(c.arguments, default=str)) for c in step.calls)
            machine.charge_tokens((spent + len(step.answer or "")) // 3 + 1)
            for _ in step.rejected:
                yield ToolCallEvent(name=REJECTED_NAME, args_summary="", status="rejected")
            wanted = [c for c in step.calls if c.name not in answered]
            if not wanted:
                return
            new: list[ExecutedCall] = []
            for i, call in enumerate(wanted, start=1):
                if not machine.can_call_tool():
                    break
                event = await tools.call(f"L{round_no}_{i}", call.name, call.arguments)
                if event is None:  # identical call repeated: a degenerate loop
                    return
                new.append(tools.calls[-1])
                yield event
            ctx = self._feed_back(ctx, new)
