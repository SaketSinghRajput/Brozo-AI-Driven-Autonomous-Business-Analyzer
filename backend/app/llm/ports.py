"""LLM gateway ports and wire contracts (design.md sections 29-31, trd.md sections 19, 21.1).

Everything here is a Protocol or a frozen Pydantic model: consumers (the gateway,
the orchestrator, tests) depend on these, never on a concrete provider. No network
code, no provider SDKs (CLAUDE.md section 20).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Any, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LLMTask(StrEnum):
    """The fixed task set the model router dispatches on (design.md section 30)."""

    INTENT = "intent"
    CLARIFY = "clarify"
    PLAN_INTENT = "plan_intent"
    TOOL_LOOP = "tool_loop"
    COMPOSE = "compose"
    ENTAILMENT_CHECK = "entailment_check"
    SUMMARIZE_MEMORY = "summarize_memory"
    REPAIR_DIAGNOSE = "repair_diagnose"
    REPAIR_PATCH = "repair_patch"


class LLMCapabilities(BaseModel):
    """What a provider can do, so the gateway/router can pick a strategy per model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    chat: bool
    stream: bool
    structured_output: Literal["grammar", "json_mode", "none"]
    tool_calls: Literal["native", "prompted", "none"]
    max_context: int


class Message(BaseModel):
    """One turn in a chat request.

    design.md sections 31/78: untrusted content (tool results, documents, memory,
    retrieved text) is data and must never occupy the system role.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["system", "user", "assistant", "tool"]
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    trust: Literal["trusted", "untrusted"] = "trusted"

    @model_validator(mode="after")
    def _untrusted_content_never_in_system_role(self) -> Self:
        if self.role == "system" and self.trust == "untrusted":
            raise ValueError("untrusted content must never be placed in the system role")
        return self


class ToolCall(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str
    arguments: dict[str, Any]


class GenParams(BaseModel):
    """Generation parameters. Default is deterministic-leaning, per trd.md section 19.3."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    temperature: float = 0.0
    max_tokens: int | None = None
    seed: int | None = None


class ChatRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    messages: list[Message]
    response_schema_name: str | None = None
    json_schema: dict[str, Any] | None = None
    tools: list[dict[str, Any]] | None = None
    params: GenParams = Field(default_factory=GenParams)
    prompt_id: str
    prompt_version: str
    trace_id: str


class Usage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_tokens: int
    completion_tokens: int


class ChatResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    model_version: str
    usage: Usage
    finish_reason: Literal["stop", "tool_calls", "length", "error"]


class ChatChunk(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    delta_text: str
    done: bool


class LLMProvider(Protocol):
    """Port implemented by every chat-capable adapter (fake, Ollama, llama.cpp, ...)."""

    capabilities: LLMCapabilities

    async def chat(self, req: ChatRequest) -> ChatResponse: ...

    def stream(self, req: ChatRequest) -> AsyncIterator[ChatChunk]: ...


class EmbeddingProvider(Protocol):
    """Separate port: embeddings are not a chat provider's responsibility (design.md section 29)."""

    model_version: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...
