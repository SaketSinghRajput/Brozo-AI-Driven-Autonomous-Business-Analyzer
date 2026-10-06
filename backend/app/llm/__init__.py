"""LLM gateway package (design.md sections 29-32, 76-79).

Ports/contracts (TASK-LLM-001), fake provider (TASK-LLM-002), gateway + resilience
(TASK-LLM-003/004/009/010), prompt registry (TASK-LLM-005), context builder (TASK-LLM-006),
Ollama adapter (TASK-LLM-007), model router (TASK-LLM-012), untrusted-content handling
(TASK-SEC-010).
"""

from app.llm.context import BuiltContext, CharRatioTokenizer, ContextBuilder, ContextPart
from app.llm.errors import LLMFailure, LLMFailureReason
from app.llm.fake import FakeLLMProvider, ScriptExhaustedError
from app.llm.gateway import LLMGateway, RejectedToolCall, ToolCallsOrAnswer, gateway_from_config
from app.llm.ollama import OllamaAdapter
from app.llm.ports import (
    ChatChunk,
    ChatRequest,
    ChatResponse,
    EmbeddingProvider,
    GenParams,
    LLMCapabilities,
    LLMProvider,
    LLMTask,
    Message,
    ToolCall,
    Usage,
)
from app.llm.prompts import PromptRegistry, PromptTemplate
from app.llm.resilience import CircuitBreaker, HostAllowList
from app.llm.router import ModelProfile, ModelRouter
from app.llm.untrusted import InjectionHeuristics, wrap_untrusted

__all__ = [
    "BuiltContext",
    "CharRatioTokenizer",
    "ChatChunk",
    "ChatRequest",
    "ChatResponse",
    "CircuitBreaker",
    "ContextBuilder",
    "ContextPart",
    "EmbeddingProvider",
    "FakeLLMProvider",
    "GenParams",
    "HostAllowList",
    "InjectionHeuristics",
    "LLMCapabilities",
    "LLMFailure",
    "LLMFailureReason",
    "LLMGateway",
    "LLMProvider",
    "LLMTask",
    "Message",
    "ModelProfile",
    "ModelRouter",
    "OllamaAdapter",
    "PromptRegistry",
    "PromptTemplate",
    "RejectedToolCall",
    "ScriptExhaustedError",
    "ToolCall",
    "ToolCallsOrAnswer",
    "Usage",
    "gateway_from_config",
    "wrap_untrusted",
]
