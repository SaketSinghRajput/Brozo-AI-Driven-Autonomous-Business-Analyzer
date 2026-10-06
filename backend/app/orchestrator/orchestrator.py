"""Orchestrator.handle_turn (TASK-AGENT-007/008/010; design §33-34, §73, TRD §44 flow B).

message → IntentResolver → QuestionPlanner → plan steps via TurnTools → ToolLoop (drill-downs)
→ ResponseComposer → ValidationService → [INVALID: recompose once with the issue codes, then
REQUIRES_REVIEW] → segments → record → final. RCA-FIX: when the recomposed answer of an
explain_change / forecast / anomaly turn fails only on wording (``fallback.WORDING``), code
writes the answer from the tool results (``fallback.template_answer``) and validates it again;
it replaces the held answer only if it passes (``answer_source="template"``).

The orchestrator computes nothing and touches no database itself: tools run through
``ToolExecutor``, validation through the ``Validation`` port (``ValidationService``), and the
answer is stored by the ``TurnRecorder`` port. Every turn yields exactly one ``final`` event.

Failures (design §73): an unreachable LLM (circuit open, timeout, connection) or any other fatal
error moves the turn to FAILED → RECOVERED. The recovered answer is never LLM-written: it lists
only the verified tool results already computed in this turn (validated like any answer), or a
fixed "assistant unavailable" caveat. ``mode="degraded"`` tells the UI to show its banner.
Recompose, not replan, on INVALID: templates are deterministic, so a replan would rebuild the
same plan; the issues are about the wording. When every plan step fails or is unavailable the
turn is REFUSED with "Insufficient data" (deviation: design §33 has no EXECUTING → REFUSED).
"""

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import JsonValue

from app.core.clock import Clock
from app.core.errors import ApplicationError
from app.core.scope import WorkspaceScope
from app.llm.errors import LLMFailure, LLMFailureReason
from app.orchestrator.composer import ResponseComposer, extract_claims, verified_facts, views
from app.orchestrator.domain import (
    AnyTurnEvent,
    ErrorEvent,
    FinalEvent,
    Refusal,
    SegmentEvent,
    StatusEvent,
    TurnError,
    TurnLimits,
    TurnRecord,
    TurnRecorder,
    TurnRequest,
    TurnState,
    ValidationEvent,
)
from app.orchestrator.fallback import template_answer, wording_only
from app.orchestrator.intent import IntentResolver
from app.orchestrator.ml_answers import ml_segments
from app.orchestrator.planner import Clarification, QuestionPlanner
from app.orchestrator.state import TurnStateMachine
from app.orchestrator.tool_loop import MAX_ATTEMPTS, ToolLoop, TurnTools
from app.tools.domain import ToolContext
from app.tools.executor import ToolExecutor, TurnBudget
from app.tools.permissions import TurnProfile, effective_permissions
from app.tools.registry import ToolRegistry
from app.validator.domain import (
    AnswerSegment,
    CaveatSegment,
    TextSegment,
    ValidationRequest,
    ValidationResult,
    ValidationStatus,
)

log = logging.getLogger("app.orchestrator")
S = TurnState
SHOWN_STATUSES = frozenset({ValidationStatus.VALID, ValidationStatus.UNCERTAIN})
UNREACHABLE = frozenset(
    {
        LLMFailureReason.UNAVAILABLE,
        LLMFailureReason.CIRCUIT_OPEN,
        LLMFailureReason.TIMEOUT,
        LLMFailureReason.PROVIDER_ERROR,
        LLMFailureReason.HOST_NOT_ALLOWED,
    }
)
UNAVAILABLE = (
    "The assistant is unavailable right now. The dashboard and its verified figures are "
    "still available."
)
PARTIAL = "The assistant could not finish this answer; only verified tool results are shown."
HELD = "This answer could not be verified and is held for review; it is not shown as fact."


class Validation(Protocol):
    async def validate(
        self, scope: WorkspaceScope, req: ValidationRequest, *, repair_exhausted: bool = False
    ) -> ValidationResult: ...


@dataclass
class _Outcome:
    stored: list[AnswerSegment] = field(default_factory=list)  # persisted (held ones included)
    shown: list[AnswerSegment] = field(default_factory=list)  # streamed to the user
    validation: ValidationResult | None = None
    mode: Literal["normal", "degraded"] = "normal"
    refusal: Refusal | None = None
    answer_source: Literal["template"] | None = None

    def say(self, text: str, *, caveat: bool = False) -> None:
        seg: AnswerSegment = CaveatSegment(text=text) if caveat else TextSegment(text=text)
        self.stored, self.shown = [seg], [seg]

    def refuse(self, refusal: Refusal) -> None:
        self.refusal = refusal
        self.say(refusal.message)


class Orchestrator:
    def __init__(
        self,
        *,
        intents: IntentResolver,
        planner: QuestionPlanner,
        tool_loop: ToolLoop,
        composer: ResponseComposer,
        validation: Validation,
        executor: ToolExecutor,
        registry: ToolRegistry,
        recorder: TurnRecorder,
        clock: Clock,
        limits: TurnLimits | None = None,
        profile: TurnProfile = TurnProfile.CHAT_DEFAULT,
    ) -> None:
        self._intents = intents
        self._planner = planner
        self._loop = tool_loop
        self._composer = composer
        self._validation = validation
        self._executor = executor
        self._registry = registry
        self._recorder = recorder
        self._clock = clock
        self._limits = limits or TurnLimits()
        self._profile = profile

    async def handle_turn(self, req: TurnRequest) -> AsyncIterator[AnyTurnEvent]:
        machine = TurnStateMachine(self._limits)
        permissions = effective_permissions(req.role, self._profile)
        tool_ctx = ToolContext(
            workspace_id=req.scope.workspace_id,
            principal_id=req.scope.principal_id,
            trace_id=req.trace_id,
            permissions=permissions,
            dataset_version_id=req.dataset_version_id,
            semantic_model_version=req.semantic_model.version,
        )
        budget = TurnBudget(self._limits.max_tool_calls * MAX_ATTEMPTS)
        tools = TurnTools(self._executor, tool_ctx, req.message_id, machine, budget)
        outcome = _Outcome()
        try:
            async for event in self._run(req, machine, tools, outcome):
                yield event
        except Exception as exc:  # every failure ends in RECOVERED with a final event
            async for event in self._recover(req, machine, tools, outcome, exc):
                yield event
        for index, segment in enumerate(views(outcome.shown, tools.calls)):
            yield SegmentEvent(index=index, segment=segment)
        try:
            await self._recorder.record(req.scope, self._record(req, machine, tools, outcome))
        except Exception:
            log.exception("turn record failed", extra={"fields": {"trace_id": req.trace_id}})
            yield _error("SYSTEM_ERROR", "The answer could not be saved.", True, req.trace_id)
        log.info(
            "chat.turn",
            extra={"fields": {"trace_id": req.trace_id, "state": machine.state.value}},
        )
        yield FinalEvent(
            message_id=req.message_id,
            state=machine.state,
            mode=outcome.mode,
            validation_status=outcome.validation.status if outcome.validation else None,
            usage=machine.usage(),
            refusal=outcome.refusal,
            answer_source=outcome.answer_source,
        )

    async def _run(
        self, req: TurnRequest, machine: TurnStateMachine, tools: TurnTools, outcome: _Outcome
    ) -> AsyncIterator[AnyTurnEvent]:
        model = req.semantic_model
        yield StatusEvent(stage=machine.to(S.CLASSIFYING))
        today = self._clock.now().date()
        resolved = await self._intents.resolve(req.question, model, today, machine)
        if resolved.intent is None:
            if resolved.refusal is not None:
                outcome.refuse(resolved.refusal)
                machine.to(S.REFUSED)
            else:
                outcome.say(resolved.clarification or "")
                machine.to(S.NEEDS_CLARIFICATION)
            return
        yield StatusEvent(stage=machine.to(S.PLANNING))
        plan = self._planner.plan(resolved.intent, model, tools.ctx.permissions, req.facts)
        if isinstance(plan, Clarification):
            outcome.say(plan.message)
            machine.to(S.NEEDS_CLARIFICATION)
            return
        if isinstance(plan, Refusal):
            outcome.refuse(plan)
            machine.to(S.REFUSED)
            return
        yield StatusEvent(stage=machine.to(S.EXECUTING))
        for step in plan.steps:
            if not machine.can_call_tool():
                break
            call_event = await tools.call(step.id, step.tool, dict(step.args))
            if call_event is not None:
                yield call_event
        if not any(c.result.status == "ok" for c in tools.calls):
            outcome.refuse(Refusal(code="INSUFFICIENT_DATA", message=_insufficient(tools)))
            machine.to(S.REFUSED)
            return
        specs = self._registry.specs_for(tools.ctx.permissions)
        async for loop_event in self._loop.run(req.question, tools, specs):
            yield loop_event
        objective = plan.intent.objective
        async for event in self._compose(req, machine, tools, outcome, resolved.caveats, objective):
            yield event

    async def _compose(
        self,
        req: TurnRequest,
        machine: TurnStateMachine,
        tools: TurnTools,
        outcome: _Outcome,
        caveats: list[str],
        objective: str,
    ) -> AsyncIterator[AnyTurnEvent]:
        feedback: list[str] = []
        while True:
            yield StatusEvent(stage=machine.to(S.COMPOSING))
            segments = await self._composer.compose(req.question, tools.calls, feedback, machine)
            ml = ml_segments(tools.calls)  # ML numbers are never LLM-written
            segments += [TextSegment(text="\n"), *ml] if ml else []  # B-3: not "it.Forecast"
            segments += [CaveatSegment(text=c) for c in caveats]
            yield StatusEvent(stage=machine.to(S.VALIDATING))
            exhausted = machine.retries >= machine.limits.max_retries
            result = await machine.bounded(self._validate(req, tools, segments, exhausted))
            outcome.validation, outcome.stored = result, segments
            yield _validation_event(result)
            if result.status is ValidationStatus.INVALID and not exhausted:
                yield StatusEvent(stage=machine.to(S.RETRYING))
                feedback = sorted({i.code for i in result.issues if i.severity == "hard"})
                continue
            if result.status not in SHOWN_STATUSES and exhausted and wording_only(result):
                written = template_answer(objective, tools.calls)
                written += [CaveatSegment(text=c) for c in caveats] if written else []
                if written:  # revalidated on its own merits; shown only if it passes
                    retry = await machine.bounded(self._validate(req, tools, written, True))
                    yield _validation_event(retry)
                    if retry.status in SHOWN_STATUSES:
                        outcome.validation, outcome.stored = retry, written
                        result, segments, outcome.answer_source = retry, written, "template"
            if result.status in SHOWN_STATUSES:
                outcome.shown = segments
                machine.to(S.COMPLETED)
            else:
                outcome.shown = [CaveatSegment(text=HELD)]
                machine.to(S.REQUIRES_REVIEW)
            return

    async def _validate(
        self,
        req: TurnRequest,
        tools: TurnTools,
        segments: list[AnswerSegment],
        exhausted: bool,
    ) -> ValidationResult:
        evidence = [c.result.evidence_id for c in tools.calls if c.result.evidence_id]
        request = ValidationRequest(
            subject_type="answer",
            subject_id=req.message_id,
            segments=segments,
            evidence_ids=list(dict.fromkeys(evidence)),
            semantic_model_version=req.semantic_model.version,
        )
        return await self._validation.validate(req.scope, request, repair_exhausted=exhausted)

    async def _recover(
        self,
        req: TurnRequest,
        machine: TurnStateMachine,
        tools: TurnTools,
        outcome: _Outcome,
        exc: Exception,
    ) -> AsyncIterator[AnyTurnEvent]:
        unreachable = isinstance(exc, LLMFailure) and exc.reason in UNREACHABLE
        if isinstance(exc, ApplicationError):
            log.warning("turn failed: %s", exc.code, extra={"fields": {"trace_id": req.trace_id}})
            code, retryable = exc.code, exc.retryable
            message = UNAVAILABLE if unreachable else exc.message
        else:
            log.exception("turn crashed", extra={"fields": {"trace_id": req.trace_id}})
            code, message, retryable = "SYSTEM_ERROR", "The answer failed unexpectedly.", True
        if not machine.terminal and machine.state is not S.FAILED:
            machine.to(S.FAILED)
        yield _error(code, message, retryable, req.trace_id)
        outcome.mode = "degraded" if unreachable else "normal"
        outcome.validation = None
        outcome.say(UNAVAILABLE if unreachable else PARTIAL, caveat=True)
        facts = verified_facts(tools.calls)
        if facts:
            try:
                result = await self._validate(req, tools, facts, exhausted=True)
            except Exception:
                log.exception("recovery validation failed")
            else:
                outcome.validation = result
                yield _validation_event(result)
                if result.status in SHOWN_STATUSES:
                    outcome.stored = outcome.shown = [*outcome.shown, *facts]
        if machine.state is S.FAILED:
            machine.to(S.RECOVERED)

    def _record(
        self, req: TurnRequest, machine: TurnStateMachine, tools: TurnTools, outcome: _Outcome
    ) -> TurnRecord:
        validation = outcome.validation
        calls: list[JsonValue] = [
            {
                "step_id": c.step_id,
                "tool": c.result.tool,
                "status": c.result.status,
                "evidence_id": c.result.evidence_id,
                "error_code": c.result.error.code if c.result.error else None,
            }
            for c in tools.calls
        ]
        trace: dict[str, JsonValue] = {
            "states": [s.value for s in machine.history],
            "tool_calls": calls,
            "usage": machine.usage().model_dump(),
            "mode": outcome.mode,
            "refusal": outcome.refusal.code if outcome.refusal else None,
            "answer_source": outcome.answer_source,
            "trace_id": req.trace_id,
        }
        claims = extract_claims(outcome.stored, tools.calls) if validation else []
        return TurnRecord(
            message_id=req.message_id,
            conversation_id=req.conversation_id,
            state=machine.state,
            segments=views(outcome.stored, tools.calls),
            validation_id=validation.id if validation else None,
            validation_status=validation.status if validation else None,
            confidence=validation.confidence if validation else None,
            claims=claims,
            trace=trace,
        )


def _insufficient(tools: TurnTools) -> str:
    reasons = []
    for c in tools.calls:
        if c.result.unavailable is not None:
            reasons.append(c.result.unavailable.message)
        elif c.result.error is not None:
            reasons.append(c.result.error.message)
    detail = "; ".join(dict.fromkeys(reasons)) or "no analysis could be run"
    return f"Insufficient data: {detail}."


def _validation_event(result: ValidationResult) -> ValidationEvent:
    return ValidationEvent(
        validation_id=result.id,
        status=result.status,
        issues=result.issues,
        confidence=result.confidence,
    )


def _error(code: str, message: str, retryable: bool, trace_id: str) -> ErrorEvent:
    return ErrorEvent(
        error=TurnError(error_code=code, message=message, retryable=retryable, trace_id=trace_id)
    )
