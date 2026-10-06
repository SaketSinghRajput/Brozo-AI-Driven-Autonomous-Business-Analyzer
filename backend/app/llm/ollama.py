"""Ollama adapter — the one local runtime adapter (TASK-LLM-007; runtime per RES-002/ADR-0007).

Talks to a local Ollama server over its native HTTP API with httpx; no SDK. Only `/api/chat`
is used: never the model-management endpoints (`/api/create`, `/api/push`, `/api/pull`,
`/api/blobs`), which have had unauthenticated CVEs (RES-002).

Hardening: the endpoint must pass `HostAllowList` before a client is created; `trust_env=False`
so HTTP(S)_PROXY env vars can never route prompts off-host; redirects are not followed.

Structured output: Ollama's `format` accepts a JSON Schema and constrains decoding to it, so
capabilities report `structured_output="grammar"`. Tool calls are native (`tools` /
`message.tool_calls`, arguments arrive as an object, no call id — we synthesise `call_<i>`).
"""

import json
from collections.abc import AsyncIterator
from typing import Any, Literal

import httpx

from app.llm.errors import LLMFailure, LLMFailureReason
from app.llm.ports import (
    ChatChunk,
    ChatRequest,
    ChatResponse,
    LLMCapabilities,
    Message,
    ToolCall,
    Usage,
)
from app.llm.resilience import HostAllowList


def _message(m: Message) -> dict[str, Any]:
    out: dict[str, Any] = {"role": m.role, "content": m.content}
    if m.role == "tool" and m.name:
        out["tool_name"] = m.name
    return out


def _to_failure(exc: Exception) -> LLMFailure:
    if isinstance(exc, httpx.TimeoutException):
        return LLMFailure(LLMFailureReason.TIMEOUT, "local LLM request timed out")
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        # 5xx / 429 are transient (model loading, overloaded); other 4xx are our bug or an
        # unsupported feature and must not be retried.
        retryable = status >= 500 or status == 429
        reason = LLMFailureReason.PROVIDER_ERROR if retryable else LLMFailureReason.UNSUPPORTED
        return LLMFailure(reason, f"local LLM returned HTTP {status}", retryable=retryable)
    return LLMFailure(LLMFailureReason.UNAVAILABLE, "local LLM unreachable")


class OllamaAdapter:
    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        allow_list: HostAllowList,
        timeout_s: float = 60.0,
        num_ctx: int = 8192,
        tool_calls: Literal["native", "prompted", "none"] = "native",
        transport: httpx.AsyncBaseTransport | None = None,
        think: bool | None = None,  # Ollama "think"; sent only when set
    ) -> None:
        allow_list.check(endpoint)
        self.model = model
        self._num_ctx = num_ctx
        self._think = think
        self.capabilities = LLMCapabilities(
            chat=True,
            stream=True,
            structured_output="grammar",
            tool_calls=tool_calls,
            max_context=num_ctx,
        )
        self._client = httpx.AsyncClient(
            base_url=endpoint,
            timeout=httpx.Timeout(timeout_s, connect=5.0),
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _payload(self, req: ChatRequest, *, stream: bool) -> dict[str, Any]:
        options: dict[str, Any] = {"temperature": req.params.temperature, "num_ctx": self._num_ctx}
        if req.params.max_tokens is not None:
            options["num_predict"] = req.params.max_tokens
        if req.params.seed is not None:
            options["seed"] = req.params.seed
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [_message(m) for m in req.messages],
            "stream": stream,
            "options": options,
        }
        if req.json_schema is not None:
            payload["format"] = req.json_schema
        if req.tools:
            payload["tools"] = req.tools
        if self._think is not None:
            payload["think"] = self._think
        return payload

    async def chat(self, req: ChatRequest) -> ChatResponse:
        try:
            r = await self._client.post("/api/chat", json=self._payload(req, stream=False))
            r.raise_for_status()
            body = r.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise _to_failure(exc) from exc
        message = body.get("message") or {}
        calls = [
            ToolCall(
                id=f"call_{i}",
                name=str(c["function"]["name"]),
                arguments=c["function"].get("arguments") or {},
            )
            for i, c in enumerate(message.get("tool_calls") or [])
        ]
        finish: Literal["stop", "tool_calls", "length"] = (
            "tool_calls" if calls else "length" if body.get("done_reason") == "length" else "stop"
        )
        return ChatResponse(
            text=message.get("content") or None,
            tool_calls=calls,
            model_version=f"ollama:{body.get('model', self.model)}",
            usage=Usage(
                prompt_tokens=int(body.get("prompt_eval_count") or 0),
                completion_tokens=int(body.get("eval_count") or 0),
            ),
            finish_reason=finish,
        )

    async def stream(self, req: ChatRequest) -> AsyncIterator[ChatChunk]:
        try:
            async with self._client.stream(
                "POST", "/api/chat", json=self._payload(req, stream=True)
            ) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line:
                        continue
                    chunk = json.loads(line)
                    delta = (chunk.get("message") or {}).get("content") or ""
                    if delta:
                        yield ChatChunk(delta_text=delta, done=False)
                    if chunk.get("done"):
                        yield ChatChunk(delta_text="", done=True)
                        return
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise _to_failure(exc) from exc
        raise LLMFailure(LLMFailureReason.PROVIDER_ERROR, "local LLM stream ended without done")
