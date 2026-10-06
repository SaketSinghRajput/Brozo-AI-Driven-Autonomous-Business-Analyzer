"""Chat-turn contracts (TASK-AGENT-001/005, TASK-CHAT-003; design §32-34, TRD §8.5 and §42).

TurnEvent protocol, streamed in this order for one turn (every turn ends with exactly one
``final``; ``error`` precedes ``final`` when the turn failed or degraded):

    status{stage}*  tool_call{name, args_summary, status, evidence_id}*  status{VALIDATING}
    validation{status, issues, confidence}  segment{index, segment}*  final{state, ...}

Segments are streamed only after validation: an unvalidated number is never shown, and a
REQUIRES_REVIEW answer is held (only a caveat explaining the hold is streamed). Number segments
carry the value and unit read from the tool result, so the UI renders numbers from evidence,
never from LLM text.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.core.scope import WorkspaceScope
from app.data.semantic.domain import SemanticModel
from app.evidence.domain import EvidenceId
from app.planner.facts import DataFacts
from app.tools.permissions import Role
from app.validator.domain import (
    CaveatSegment,
    ConfidenceSummary,
    TextSegment,
    ValidationIssue,
    ValidationStatus,
)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TurnState(StrEnum):
    """design §33. Deviation: any non-terminal state may go to FAILED on a fatal error."""

    IDLE = "IDLE"
    CLASSIFYING = "CLASSIFYING"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    REFUSED = "REFUSED"
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    WAITING_FOR_TOOL = "WAITING_FOR_TOOL"
    COMPOSING = "COMPOSING"
    VALIDATING = "VALIDATING"
    RETRYING = "RETRYING"
    COMPLETED = "COMPLETED"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"
    FAILED = "FAILED"
    RECOVERED = "RECOVERED"


class TurnLimits(Contract):
    """Per-turn budgets (design §33); every one is checked in code, not by the LLM."""

    max_steps: Annotated[int, Field(ge=1)] = 40  # state transitions
    max_tool_calls: Annotated[int, Field(ge=1)] = 8
    max_retries: Annotated[int, Field(ge=0, le=2)] = 1  # RETRYING entries
    max_llm_tokens: Annotated[int, Field(ge=1)] = 40_000  # estimated prompt + completion
    wall_clock_timeout_s: Annotated[float, Field(gt=0)] = 300.0
    max_loop_rounds: Annotated[int, Field(ge=0)] = 2  # ToolLoop LLM rounds after the plan


class Refusal(Contract):
    code: Literal["UNSUPPORTED", "UNKNOWN_NAME", "INSUFFICIENT_DATA", "NOT_AVAILABLE"]
    message: str  # user-safe; never echoes the question
    suggestions: list[str] = []


# --- events -------------------------------------------------------------------------------


class NumberView(Contract):
    """A number segment as shown: the validator's NumberSegment plus the evidence value."""

    type: Literal["number"] = "number"
    evidence_id: EvidenceId
    field: str
    format: str
    value: float | None
    unit: str | None
    # B-5: a direction word ("fell by", "a ... decrease") already states the sign, so the value
    # is displayed without it (validator.text.direction_at); ``value`` itself stays signed.
    absolute: bool = False


DisplaySegment = Annotated[TextSegment | NumberView | CaveatSegment, Field(discriminator="type")]


class StatusEvent(Contract):
    type: Literal["status"] = "status"
    stage: TurnState


MAX_STREAMED_FINDINGS = 10


class FindingView(Contract):
    """One ranked RCA factor for the RCA panel (``analytics.explain_change`` finding)."""

    factor: str  # "Category=Electronics"
    change: float  # change of the metric carried by this member, in the metric's unit
    unit: str | None
    share: float | None  # share of the total change, a fraction; None when undefined
    relation_type: Literal["contribution", "association", "correlation", "causation"]


class ToolCallEvent(Contract):
    type: Literal["tool_call"] = "tool_call"
    name: str
    args_summary: str  # argument names only; values may be untrusted text
    status: Literal["ok", "unavailable", "error", "rejected"]
    evidence_id: EvidenceId | None = None
    error_code: str | None = None
    # explain_change only: the top MAX_STREAMED_FINDINGS ranked factors (all rows: evidence data)
    findings: Annotated[list[FindingView], Field(max_length=MAX_STREAMED_FINDINGS)] | None = None


class SegmentEvent(Contract):
    type: Literal["segment"] = "segment"
    index: Annotated[int, Field(ge=0)]
    segment: DisplaySegment


class ValidationEvent(Contract):
    type: Literal["validation"] = "validation"
    validation_id: str
    status: ValidationStatus
    issues: list[ValidationIssue]
    confidence: ConfidenceSummary


class TurnUsage(Contract):
    steps: int
    tool_calls: int
    llm_tokens: int  # estimated (CharRatioTokenizer), not the provider's count
    retries: int


class FinalEvent(Contract):
    type: Literal["final"] = "final"
    message_id: str  # the stored assistant message (TRD §42 answer_id)
    state: TurnState
    mode: Literal["normal", "degraded"] = "normal"
    validation_status: ValidationStatus | None = None
    usage: TurnUsage
    refusal: Refusal | None = None  # REFUSED turns: reason code, message, suggestions
    # "template": the answer was written by code from the analysis results because the
    # assistant's wording did not pass verification (app.orchestrator.fallback); None otherwise.
    answer_source: Literal["template"] | None = None


class TurnError(Contract):
    error_code: str
    message: str
    retryable: bool
    trace_id: str | None


class ErrorEvent(Contract):
    type: Literal["error"] = "error"
    error: TurnError


AnyTurnEvent = (
    StatusEvent | ToolCallEvent | SegmentEvent | ValidationEvent | FinalEvent | ErrorEvent
)


class TurnEvent(Contract):
    """Exported contract (shared/schemas/turn_event.schema.json): one SSE ``data`` payload.

    ``seq`` equals the SSE ``id`` and ``event.type`` the SSE ``event`` name.
    """

    schema_version: Literal["1.0"] = "1.0"
    seq: Annotated[int, Field(ge=0)]
    event: Annotated[AnyTurnEvent, Field(discriminator="type")]


# --- turn input / record -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TurnRequest:
    scope: WorkspaceScope
    role: Role
    question: str
    conversation_id: str
    message_id: str  # id of the assistant message this turn produces
    dataset_version_id: str
    semantic_model: SemanticModel
    trace_id: str
    facts: DataFacts | None = None  # profile + understanding; needed by forecast/anomaly plans


class ClaimDraft(Contract):
    text: str
    evidence_ids: Annotated[list[EvidenceId], Field(min_length=1)]


class TurnRecord(Contract):
    """What is persisted for one assistant answer (messages row + claims)."""

    message_id: str
    conversation_id: str
    state: TurnState
    segments: list[DisplaySegment]  # numbers carry their evidence value + unit (history view)
    validation_id: str | None
    validation_status: ValidationStatus | None = None
    confidence: ConfidenceSummary | None = None
    claims: list[ClaimDraft]
    trace: dict[str, JsonValue]  # states, tool calls, usage (persisted turn states, AGENT-001)


class TurnRecorder(Protocol):
    async def record(self, scope: WorkspaceScope, record: TurnRecord) -> None: ...
