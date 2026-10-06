"""TASK-LLM-003/004/009/010/012: LLMGateway behaviour against the scripted FakeLLMProvider."""

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any, Literal

import pytest
from pydantic import BaseModel

from app.core.config import LLMConfig
from app.llm.context import BuiltContext
from app.llm.errors import LLMFailure, LLMFailureReason
from app.llm.fake import FakeLLMProvider
from app.llm.gateway import LLMGateway, gateway_from_config
from app.llm.ports import (
    ChatChunk,
    ChatRequest,
    ChatResponse,
    LLMCapabilities,
    LLMProvider,
    LLMTask,
    Message,
    ToolCall,
    Usage,
)
from app.llm.prompts import PromptRegistry
from app.llm.resilience import CircuitBreaker
from app.llm.router import ModelProfile, ModelRouter
from app.tools.domain import ToolSpecForLLM

pytestmark = pytest.mark.anyio

PROMPTS = PromptRegistry()
TASK = LLMTask.INTENT


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Intent(BaseModel):
    route: Literal["structured", "document"]
    metric: str | None


def _resp(text: str | None = None, calls: list[ToolCall] | None = None) -> ChatResponse:
    return ChatResponse(
        text=text,
        tool_calls=calls or [],
        model_version="fake-1",
        usage=Usage(prompt_tokens=10, completion_tokens=3),
        finish_reason="tool_calls" if calls else "stop",
    )


def _ctx(tokens: int = 50) -> BuiltContext:
    return BuiltContext(
        messages=[Message(role="system", content="sys"), Message(role="user", content="q")],
        prompt_id="intent.classify",
        prompt_version="abc123",
        tokens=tokens,
    )


def _caps(
    structured: Literal["grammar", "json_mode", "none"] = "grammar",
    tools: Literal["native", "prompted", "none"] = "native",
) -> LLMCapabilities:
    return LLMCapabilities(
        chat=True, stream=True, structured_output=structured, tool_calls=tools, max_context=4096
    )


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _gateway(
    provider: LLMProvider,
    *,
    caps: LLMCapabilities | None = None,
    sleep: Sleeps | None = None,
    breaker: CircuitBreaker | None = None,
    **kwargs: Any,
) -> LLMGateway:
    profile = ModelProfile(
        name="default",
        provider="fake",
        model_version="fake-1",
        max_context=4096,
        capabilities=caps or _caps(),
    )
    return LLMGateway(
        ModelRouter({"default": profile}),
        {"default": provider},
        PROMPTS,
        sleep=sleep or Sleeps(),
        breaker_factory=(lambda: breaker) if breaker else CircuitBreaker,
        **kwargs,
    )


def _transient() -> LLMFailure:
    return LLMFailure(LLMFailureReason.UNAVAILABLE, "down")


VALID = '{"route":"structured","metric":"revenue"}'


# -- retry / breaker / timeout / accounting ----------------------------------------------------


async def test_transient_failures_retried_with_bounded_exponential_backoff() -> None:
    sleeps = Sleeps()
    fake = FakeLLMProvider([_transient(), _transient(), _resp(VALID)])
    result = await _gateway(fake, sleep=sleeps).structured(TASK, Intent, _ctx())
    assert result.route == "structured"
    assert sleeps.calls == [0.5, 1.0]
    assert len(fake.requests) == 3


async def test_retries_stop_at_max_attempts() -> None:
    fake = FakeLLMProvider([_transient()] * 5)
    with pytest.raises(LLMFailure) as err:
        await _gateway(fake).structured(TASK, Intent, _ctx())
    assert err.value.reason is LLMFailureReason.UNAVAILABLE
    assert len(fake.requests) == 3


async def test_non_transient_failure_not_retried() -> None:
    fake = FakeLLMProvider([LLMFailure(LLMFailureReason.UNSUPPORTED, "400", retryable=False)])
    with pytest.raises(LLMFailure):
        await _gateway(fake).structured(TASK, Intent, _ctx())
    assert len(fake.requests) == 1


async def test_non_llm_exceptions_propagate_without_retry() -> None:
    fake = FakeLLMProvider([ValueError("bug")])
    with pytest.raises(ValueError, match="bug"):
        await _gateway(fake).structured(TASK, Intent, _ctx())
    assert len(fake.requests) == 1


class SlowProvider:
    capabilities = _caps()

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.active = 0
        self.max_active = 0

    async def chat(self, req: ChatRequest) -> ChatResponse:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(self.delay)
        self.active -= 1
        return _resp(VALID)

    async def stream(self, req: ChatRequest) -> AsyncIterator[ChatChunk]:
        await asyncio.sleep(self.delay)
        yield ChatChunk(delta_text="late", done=True)


async def test_timeout_is_a_transient_failure() -> None:
    slow = SlowProvider(delay=1.0)
    with pytest.raises(LLMFailure) as err:
        await _gateway(slow, timeout_s=0.01, max_attempts=2).structured(TASK, Intent, _ctx())
    assert err.value.reason is LLMFailureReason.TIMEOUT


async def test_semaphore_bounds_concurrency() -> None:
    slow = SlowProvider(delay=0.02)
    gateway = _gateway(slow, max_concurrency=1)
    await asyncio.gather(*(gateway.structured(TASK, Intent, _ctx()) for _ in range(4)))
    assert slow.max_active == 1


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _state(breaker: CircuitBreaker) -> str:
    return breaker.state  # plain str: stops mypy narrowing the property between asserts


def test_breaker_opens_half_opens_and_closes() -> None:
    clock = Clock()
    breaker = CircuitBreaker(failure_threshold=2, reset_after_s=10, now=clock)
    breaker.record_failure()
    assert _state(breaker) == "closed"
    breaker.record_failure()
    assert _state(breaker) == "open"
    with pytest.raises(LLMFailure) as err:
        breaker.before_call()
    assert err.value.reason is LLMFailureReason.CIRCUIT_OPEN
    clock.t = 10
    assert _state(breaker) == "half_open"
    breaker.before_call()  # the single trial
    with pytest.raises(LLMFailure):
        breaker.before_call()  # a second concurrent trial is refused
    breaker.record_success()
    assert _state(breaker) == "closed"


def test_failed_half_open_trial_reopens() -> None:
    clock = Clock()
    breaker = CircuitBreaker(failure_threshold=1, reset_after_s=5, now=clock)
    breaker.record_failure()
    clock.t = 5
    breaker.before_call()
    breaker.record_failure()
    assert _state(breaker) == "open"


async def test_gateway_opens_breaker_and_short_circuits() -> None:
    breaker = CircuitBreaker(failure_threshold=3, reset_after_s=60, now=Clock())
    fake = FakeLLMProvider([_transient()] * 3)
    gateway = _gateway(fake, breaker=breaker)
    with pytest.raises(LLMFailure):
        await gateway.structured(TASK, Intent, _ctx())
    assert _state(breaker) == "open"
    with pytest.raises(LLMFailure) as err:
        await gateway.structured(TASK, Intent, _ctx())
    assert err.value.reason is LLMFailureReason.CIRCUIT_OPEN
    assert len(fake.requests) == 3  # no provider call while open


async def test_non_transient_failures_do_not_trip_breaker() -> None:
    breaker = CircuitBreaker(failure_threshold=1, reset_after_s=60, now=Clock())
    fake = FakeLLMProvider([LLMFailure(LLMFailureReason.UNSUPPORTED, "400", retryable=False)])
    with pytest.raises(LLMFailure):
        await _gateway(fake, breaker=breaker).structured(TASK, Intent, _ctx())
    assert _state(breaker) == "closed"


async def test_token_accounting_and_call_log_carries_prompt_identity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeLLMProvider([_resp(VALID)])
    gateway = _gateway(fake)
    with caplog.at_level(logging.INFO, logger="app.llm"):
        await gateway.structured(TASK, Intent, _ctx())
    assert gateway.token_usage == {"intent.prompt": 10, "intent.completion": 3}
    record = next(r for r in caplog.records if r.getMessage() == "llm.call")
    fields = record.__dict__["fields"]
    assert fields["prompt_id"] == "intent.classify"
    assert fields["prompt_version"] == "abc123"
    assert fields["outcome"] == "ok"


# -- router ------------------------------------------------------------------------------------


async def test_context_too_large_rejected_before_any_call() -> None:
    fake = FakeLLMProvider([])
    with pytest.raises(LLMFailure) as err:
        await _gateway(fake).structured(TASK, Intent, _ctx(tokens=4000))
    assert err.value.reason is LLMFailureReason.CONTEXT_TOO_LARGE
    assert fake.requests == []


def test_router_rejects_policy_with_unknown_profile() -> None:
    profile = ModelProfile(
        name="default", provider="f", model_version="m", max_context=100, capabilities=_caps()
    )
    with pytest.raises(ValueError, match="unknown profiles"):
        ModelRouter({"default": profile}, {LLMTask.INTENT: "strong"})


# -- structured output -------------------------------------------------------------------------


async def test_structured_sends_schema_when_provider_constrains() -> None:
    fake = FakeLLMProvider([_resp(VALID)])
    await _gateway(fake).structured(TASK, Intent, _ctx())
    req = fake.requests[0]
    assert req.json_schema == Intent.model_json_schema()
    assert '"route"' in req.messages[-1].content  # schema also stated in the prompt


async def test_structured_prompted_only_when_no_constrained_decoding() -> None:
    fake = FakeLLMProvider([_resp(VALID)])
    await _gateway(fake, caps=_caps(structured="none")).structured(TASK, Intent, _ctx())
    assert fake.requests[0].json_schema is None


async def test_one_repair_attempt_with_validation_errors() -> None:
    fake = FakeLLMProvider([_resp('{"route":"sideways"}'), _resp(VALID)])
    result = await _gateway(fake).structured(TASK, Intent, _ctx())
    assert result.metric == "revenue"
    repair = fake.requests[1]
    assert repair.messages[-2].role == "assistant"
    assert "route" in repair.messages[-1].content
    assert repair.prompt_id == "intent.classify+common.structured_repair"


async def test_second_invalid_output_is_a_typed_failure() -> None:
    fake = FakeLLMProvider([_resp("not json"), _resp('{"route": 1}')])
    with pytest.raises(LLMFailure) as err:
        await _gateway(fake).structured(TASK, Intent, _ctx())
    assert err.value.reason is LLMFailureReason.STRUCTURED_OUTPUT_INVALID
    assert err.value.retryable is False
    assert len(fake.requests) == 2  # exactly one repair


async def test_strict_validation_rejects_coercion() -> None:
    class Count(BaseModel):
        n: int

    fake = FakeLLMProvider([_resp('{"n": "3"}'), _resp('{"n": "3"}')])
    with pytest.raises(LLMFailure):
        await _gateway(fake).structured(TASK, Count, _ctx())


async def test_semantic_check_failure_triggers_repair() -> None:
    def metric_known(intent: Intent) -> list[str]:
        return [] if intent.metric == "revenue" else [f"unknown metric {intent.metric!r}"]

    fake = FakeLLMProvider([_resp('{"route":"structured","metric":"vibes"}'), _resp(VALID)])
    result = await _gateway(fake).structured(TASK, Intent, _ctx(), check=metric_known)
    assert result.metric == "revenue"
    assert "unknown metric 'vibes'" in fake.requests[1].messages[-1].content


# -- tool calls --------------------------------------------------------------------------------

TOOLS = [
    ToolSpecForLLM(
        name="analytics.metric_query",
        description="Compute a metric.",
        input_schema={"type": "object", "properties": {"metric": {"type": "string"}}},
    )
]


async def test_tool_step_returns_offered_calls_and_rejects_unknown() -> None:
    calls = [
        ToolCall(id="call_0", name="analytics.metric_query", arguments={"metric": "revenue"}),
        ToolCall(id="call_1", name="sql.drop_everything", arguments={}),
    ]
    fake = FakeLLMProvider([_resp(calls=calls)])
    result = await _gateway(fake).tool_step(LLMTask.TOOL_LOOP, TOOLS, _ctx())
    assert [c.name for c in result.calls] == ["analytics.metric_query"]
    assert [r.name for r in result.rejected] == ["sql.drop_everything"]
    sent = fake.requests[0].tools
    assert sent is not None
    assert sent[0]["function"]["name"] == "analytics.metric_query"


async def test_tool_step_plain_answer() -> None:
    fake = FakeLLMProvider([_resp("I cannot answer that with the available tools.")])
    result = await _gateway(fake).tool_step(LLMTask.TOOL_LOOP, TOOLS, _ctx())
    assert result.calls == []
    assert result.answer is not None


async def test_tool_step_requires_native_capability() -> None:
    fake = FakeLLMProvider([])
    gateway = _gateway(fake, caps=_caps(tools="none"))
    with pytest.raises(LLMFailure) as err:
        await gateway.tool_step(LLMTask.TOOL_LOOP, TOOLS, _ctx())
    assert err.value.reason is LLMFailureReason.UNSUPPORTED


# -- streaming ---------------------------------------------------------------------------------


async def test_stream_text_forwards_chunks() -> None:
    fake = FakeLLMProvider([_resp("The quick brown fox jumps.")])
    gateway = _gateway(fake)
    text = "".join([d async for d in gateway.stream_text(LLMTask.COMPOSE, _ctx())])
    assert text == "The quick brown fox jumps."


async def test_stream_cancellation_releases_semaphore() -> None:
    fake = FakeLLMProvider([_resp("x" * 100), _resp(VALID)])
    gateway = _gateway(fake, max_concurrency=1)
    stream = gateway.stream_text(LLMTask.COMPOSE, _ctx())
    assert await anext(stream)
    await stream.aclose()  # type: ignore[attr-defined]  # consumer disconnects mid-stream
    async with asyncio.timeout(1):  # would deadlock if the semaphore leaked
        await gateway.structured(TASK, Intent, _ctx())


async def test_stalled_stream_times_out() -> None:
    gateway = _gateway(SlowProvider(delay=1.0), timeout_s=0.01)
    with pytest.raises(LLMFailure) as err:
        async for _ in gateway.stream_text(LLMTask.COMPOSE, _ctx()):
            pass
    assert err.value.reason is LLMFailureReason.TIMEOUT


# -- composition -------------------------------------------------------------------------------


def test_gateway_from_config_refuses_non_allow_listed_host() -> None:
    # model_construct bypasses LLMConfig's startup validator so the runtime guard is exercised.
    config = LLMConfig.model_construct(
        **{**LLMConfig().model_dump(), "endpoint": "http://llm.example.com:11434"}
    )
    with pytest.raises(LLMFailure) as err:
        gateway_from_config(config)
    assert err.value.reason is LLMFailureReason.HOST_NOT_ALLOWED


def test_gateway_from_config_default_is_local_ollama() -> None:
    assert isinstance(gateway_from_config(LLMConfig()), LLMGateway)
