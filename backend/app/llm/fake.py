"""FakeLLMProvider: a scripted, in-memory LLMProvider for tests (TASK-LLM-002).

No network. A script is an ordered queue of steps; each step is either the next
ChatResponse to hand back, or an exception instance to raise instead (LLMError,
TimeoutError, ...). This single shape covers every case the task calls for: a
text response and a structured-JSON response are both just a ChatResponse.text;
tool calls are ChatResponse.tool_calls; invalid JSON is a ChatResponse.text that
happens not to parse (validity is the consumer's concern, not the fake's);
failure injection is a scripted exception.
"""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator, Iterable

from app.llm.ports import ChatChunk, ChatRequest, ChatResponse, LLMCapabilities

_DEFAULT_CAPABILITIES = LLMCapabilities(
    chat=True,
    stream=True,
    structured_output="json_mode",
    tool_calls="native",
    max_context=8192,
)

# ponytail: fixed 8-char stream chunks, not word/token-aware; swap for a real
# tokenizer-based splitter only if a test needs word/token boundaries.
_STREAM_CHUNK_SIZE = 8


class ScriptExhaustedError(RuntimeError):
    """Raised when FakeLLMProvider receives a call after its script has run out."""

    def __init__(self, calls_made: int) -> None:
        super().__init__(
            f"FakeLLMProvider script exhausted: no step left for call #{calls_made + 1}"
        )


class FakeLLMProvider:
    """Implements the LLMProvider port by replaying a fixed, ordered script."""

    def __init__(
        self,
        script: Iterable[ChatResponse | BaseException],
        *,
        capabilities: LLMCapabilities = _DEFAULT_CAPABILITIES,
    ) -> None:
        self.capabilities = capabilities
        self._steps: deque[ChatResponse | BaseException] = deque(script)
        self.requests: list[ChatRequest] = []

    def _next_step(self) -> ChatResponse:
        if not self._steps:
            raise ScriptExhaustedError(len(self.requests))
        step = self._steps.popleft()
        if isinstance(step, BaseException):
            raise step
        return step

    async def chat(self, req: ChatRequest) -> ChatResponse:
        self.requests.append(req)
        return self._next_step()

    async def stream(self, req: ChatRequest) -> AsyncIterator[ChatChunk]:
        self.requests.append(req)
        response = self._next_step()
        text = response.text or ""
        for start in range(0, len(text), _STREAM_CHUNK_SIZE):
            yield ChatChunk(delta_text=text[start : start + _STREAM_CHUNK_SIZE], done=False)
        yield ChatChunk(delta_text="", done=True)
