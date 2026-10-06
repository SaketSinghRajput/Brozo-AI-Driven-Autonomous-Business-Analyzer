"""LLMGateway — the only way application code talks to a model (design.md sections 29-32).

Per call: route (ModelRouter, context fit) → circuit breaker → concurrency semaphore → timeout →
provider → token accounting + one `llm.call` log line carrying prompt id/version. Only
transient failures (timeout, unreachable, 5xx/429) are retried, with bounded exponential
backoff; they are also the only failures that count toward the breaker.

- `structured()` (TASK-LLM-004): schema-constrained decoding when the provider supports it,
  the schema is always also stated in the prompt, then strict Pydantic validation + optional
  semantic checks; one repair round with the errors; then STRUCTURED_OUTPUT_INVALID.
- `tool_step()` (TASK-LLM-009): native tool calling; calls to tools that were not offered are
  returned as rejections, never executed. Argument validation against the tool's Pydantic
  input model stays in ToolExecutor (single choke point, design section 19).
- `stream_text()` (TASK-LLM-010): forwards text deltas; per-chunk idle timeout; closing the
  consumer's iterator closes the provider stream and frees the semaphore. Streams are not
  retried (partial output may already be shown).
"""

import asyncio
import json
import logging
import time
from collections import Counter
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import LLMConfig
from app.core.logging import trace_id_var
from app.core.observability import LLM_CALLS, LLM_LATENCY, LLM_TOKENS, span
from app.llm.context import BuiltContext
from app.llm.errors import LLMFailure, LLMFailureReason
from app.llm.ollama import OllamaAdapter
from app.llm.ports import ChatRequest, ChatResponse, LLMProvider, LLMTask, Message, ToolCall
from app.llm.prompts import PromptRegistry
from app.llm.resilience import CircuitBreaker, HostAllowList
from app.llm.router import DEFAULT_PROFILE, ModelProfile, ModelRouter
from app.tools.domain import ToolSpecForLLM

log = logging.getLogger("app.llm")

MAX_REPAIR_ERRORS = 10


class RejectedToolCall(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    reason: str


class ToolCallsOrAnswer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    calls: list[ToolCall] = Field(default_factory=list)
    rejected: list[RejectedToolCall] = Field(default_factory=list)
    answer: str | None = None
    model_version: str


def _estimate_tokens(messages: Sequence[Message]) -> int:
    # ponytail: same conservative 3 chars/token as CharRatioTokenizer, for gateway-added text.
    return sum(len(m.content) // 3 + 4 for m in messages)


class LLMGateway:
    def __init__(
        self,
        router: ModelRouter,
        providers: dict[str, LLMProvider],
        prompts: PromptRegistry,
        *,
        max_concurrency: int = 1,
        max_attempts: int = 3,
        backoff_s: float = 0.5,
        timeout_s: float = 60.0,
        breaker_factory: Callable[[], CircuitBreaker] = CircuitBreaker,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._router = router
        self._providers = providers
        self._prompts = prompts
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._max_attempts = max_attempts
        self._backoff_s = backoff_s
        self._timeout_s = timeout_s
        self._breakers = {name: breaker_factory() for name in providers}
        self._sleep = sleep
        self.token_usage: Counter[str] = Counter()  # "<task>.prompt" / "<task>.completion"

    def breaker(self, profile_name: str) -> CircuitBreaker:
        return self._breakers[profile_name]

    # -- plumbing ---------------------------------------------------------------------------

    def _request(
        self,
        prompt_id: str,
        prompt_version: str,
        messages: list[Message],
        *,
        json_schema: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatRequest:
        return ChatRequest(
            messages=messages,
            json_schema=json_schema,
            tools=tools,
            prompt_id=prompt_id,
            prompt_version=prompt_version,
            trace_id=trace_id_var.get() or "none",
        )

    def _log(self, task: LLMTask, req: ChatRequest, profile: ModelProfile, **fields: Any) -> None:
        LLM_CALLS.labels(task.value, str(fields.get("outcome"))).inc()
        if "latency_ms" in fields:
            LLM_LATENCY.labels(task.value).observe(fields["latency_ms"] / 1000)
        for kind in ("prompt", "completion"):
            if f"{kind}_tokens" in fields:
                LLM_TOKENS.labels(task.value, kind).inc(fields[f"{kind}_tokens"])
        log.info(
            "llm.call",
            extra={
                "fields": {
                    "task": task.value,
                    "prompt_id": req.prompt_id,
                    "prompt_version": req.prompt_version,
                    "profile": profile.name,
                    "model_version": profile.model_version,
                    **fields,
                }
            },
        )

    async def _chat(self, task: LLMTask, profile: ModelProfile, req: ChatRequest) -> ChatResponse:
        # One span per logical call (all attempts); per-attempt detail is in the llm.call logs.
        with span("llm.call", task=task.value, profile=profile.name, prompt_id=req.prompt_id):
            return await self._chat_attempts(task, profile, req)

    async def _chat_attempts(
        self, task: LLMTask, profile: ModelProfile, req: ChatRequest
    ) -> ChatResponse:
        provider, breaker = self._providers[profile.name], self._breakers[profile.name]
        attempt = 0
        while True:
            attempt += 1
            breaker.before_call()
            started = time.perf_counter()
            try:
                async with self._semaphore, asyncio.timeout(self._timeout_s):
                    resp = await provider.chat(req)
            except TimeoutError:
                failure = LLMFailure(LLMFailureReason.TIMEOUT, "local LLM request timed out")
            except LLMFailure as exc:
                failure = exc
            else:
                breaker.record_success()
                self.token_usage[f"{task.value}.prompt"] += resp.usage.prompt_tokens
                self.token_usage[f"{task.value}.completion"] += resp.usage.completion_tokens
                self._log(
                    task,
                    req,
                    profile,
                    attempt=attempt,
                    outcome="ok",
                    latency_ms=round((time.perf_counter() - started) * 1000),
                    prompt_tokens=resp.usage.prompt_tokens,
                    completion_tokens=resp.usage.completion_tokens,
                )
                return resp
            if failure.retryable:
                breaker.record_failure()
            self._log(task, req, profile, attempt=attempt, outcome=failure.reason.value)
            if not failure.retryable or attempt >= self._max_attempts:
                raise failure
            await self._sleep(self._backoff_s * 2 ** (attempt - 1))

    # -- public API -------------------------------------------------------------------------

    async def structured[T: BaseModel](
        self,
        task: LLMTask,
        schema: type[T],
        ctx: BuiltContext,
        *,
        check: Callable[[T], list[str]] | None = None,
    ) -> T:
        schema_json = schema.model_json_schema()
        _, instruction = self._prompts.get("common.structured_output").render(
            schema=json.dumps(schema_json)
        )
        messages = [*ctx.messages, Message(role="user", content=instruction)]
        profile = self._router.select(task, ctx.tokens + _estimate_tokens(messages[-1:]))
        constrained = profile.capabilities.structured_output != "none"
        req = self._request(
            ctx.prompt_id,
            ctx.prompt_version,
            messages,
            json_schema=schema_json if constrained else None,
        )
        resp = await self._chat(task, profile, req)
        value, errors = _accept(schema, resp.text, check)
        if value is not None:
            return value

        repair = self._prompts.get("common.structured_repair")
        _, repair_text = repair.render(errors="\n".join(errors[:MAX_REPAIR_ERRORS]))
        messages += [
            Message(role="assistant", content=resp.text or ""),
            Message(role="user", content=repair_text),
        ]
        repair_req = self._request(
            f"{ctx.prompt_id}+{repair.id}",
            f"{ctx.prompt_version}+{repair.version}",
            messages,
            json_schema=req.json_schema,
        )
        resp = await self._chat(task, profile, repair_req)
        value, errors = _accept(schema, resp.text, check)
        if value is not None:
            return value
        raise LLMFailure(
            LLMFailureReason.STRUCTURED_OUTPUT_INVALID,
            f"{schema.__name__} output invalid after one repair: {'; '.join(errors[:3])}",
            retryable=False,
        )

    async def tool_step(
        self, task: LLMTask, tools: list[ToolSpecForLLM], ctx: BuiltContext
    ) -> ToolCallsOrAnswer:
        profile = self._router.select(task, ctx.tokens)
        if profile.capabilities.tool_calls != "native":
            raise LLMFailure(
                LLMFailureReason.UNSUPPORTED,
                f"profile {profile.name!r} has no native tool calling",
                retryable=False,
            )
        offered = {t.name for t in tools}
        payload: list[dict[str, Any]] = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                },
            }
            for t in tools
        ]
        req = self._request(ctx.prompt_id, ctx.prompt_version, list(ctx.messages), tools=payload)
        resp = await self._chat(task, profile, req)
        return ToolCallsOrAnswer(
            calls=[c for c in resp.tool_calls if c.name in offered],
            rejected=[
                RejectedToolCall(name=c.name, reason="tool not offered in this turn")
                for c in resp.tool_calls
                if c.name not in offered
            ],
            answer=None if resp.tool_calls else resp.text,
            model_version=resp.model_version,
        )

    async def stream_text(self, task: LLMTask, ctx: BuiltContext) -> AsyncIterator[str]:
        profile = self._router.select(task, ctx.tokens)
        provider, breaker = self._providers[profile.name], self._breakers[profile.name]
        req = self._request(ctx.prompt_id, ctx.prompt_version, list(ctx.messages))
        breaker.before_call()
        async with self._semaphore:
            chunks = provider.stream(req)
            try:
                while True:
                    try:
                        async with asyncio.timeout(self._timeout_s):
                            chunk = await anext(chunks)
                    except StopAsyncIteration:
                        break
                    except TimeoutError:
                        raise LLMFailure(
                            LLMFailureReason.TIMEOUT, "local LLM stream stalled"
                        ) from None
                    if chunk.delta_text:
                        yield chunk.delta_text
                    if chunk.done:
                        break
            except LLMFailure as failure:
                if failure.retryable:
                    breaker.record_failure()
                self._log(task, req, profile, attempt=1, outcome=failure.reason.value)
                raise
            finally:
                if isinstance(chunks, AsyncGenerator):
                    await chunks.aclose()
        breaker.record_success()
        self._log(task, req, profile, attempt=1, outcome="ok", streamed=True)


def _accept[T: BaseModel](
    schema: type[T], text: str | None, check: Callable[[T], list[str]] | None
) -> tuple[T | None, list[str]]:
    try:
        value = schema.model_validate_json(text or "", strict=True)
    except ValidationError as exc:
        return None, [
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors(include_input=False, include_url=False)
        ]
    problems = check(value) if check else []
    return (None, problems) if problems else (value, [])


def gateway_from_config(config: LLMConfig, prompts: PromptRegistry | None = None) -> LLMGateway:
    """Composition helper: the MVP single `default` profile on the local Ollama server."""
    adapter = OllamaAdapter(
        endpoint=str(config.endpoint),
        model=config.model,
        allow_list=HostAllowList(config.allowed_hosts),
        timeout_s=config.timeout_s,
        num_ctx=config.num_ctx,
    )
    profile = ModelProfile(
        name=DEFAULT_PROFILE,
        provider="ollama",
        model_version=f"ollama:{config.model}",
        max_context=config.num_ctx,
        capabilities=adapter.capabilities,
    )
    return LLMGateway(
        ModelRouter({DEFAULT_PROFILE: profile}),
        {DEFAULT_PROFILE: adapter},
        prompts or PromptRegistry(),
        max_concurrency=config.max_concurrency,
        max_attempts=config.max_attempts,
        timeout_s=config.timeout_s,
        breaker_factory=lambda: CircuitBreaker(
            config.breaker_failure_threshold, config.breaker_reset_s
        ),
    )
