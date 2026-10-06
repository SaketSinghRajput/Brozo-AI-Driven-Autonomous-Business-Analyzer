"""TASK-LLM-002: behavioural tests for FakeLLMProvider."""

import pytest

from app.core.errors import LLMError
from app.llm.fake import FakeLLMProvider, ScriptExhaustedError
from app.llm.ports import ChatRequest, ChatResponse, LLMProvider, Message, ToolCall, Usage

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _req(text: str = "hi") -> ChatRequest:
    return ChatRequest(
        messages=[Message(role="user", content=text)],
        prompt_id="p",
        prompt_version="1",
        trace_id="t",
    )


def _resp(text: str) -> ChatResponse:
    return ChatResponse(
        text=text,
        model_version="fake-1",
        usage=Usage(prompt_tokens=1, completion_tokens=1),
        finish_reason="stop",
    )


async def test_script_ordering() -> None:
    provider = FakeLLMProvider(script=[_resp("first"), _resp("second")])
    first = await provider.chat(_req())
    second = await provider.chat(_req())
    assert (first.text, second.text) == ("first", "second")


async def test_records_every_request_in_order() -> None:
    provider = FakeLLMProvider(script=[_resp("a"), _resp("b")])
    await provider.chat(_req("one"))
    await provider.chat(_req("two"))
    assert [r.messages[0].content for r in provider.requests] == ["one", "two"]


async def test_tool_call_step() -> None:
    tool_response = ChatResponse(
        tool_calls=[ToolCall(id="tc1", name="analytics.stats", arguments={"metric": "revenue"})],
        model_version="fake-1",
        usage=Usage(prompt_tokens=5, completion_tokens=5),
        finish_reason="tool_calls",
    )
    provider = FakeLLMProvider(script=[tool_response])
    result = await provider.chat(_req())
    assert result.tool_calls[0].name == "analytics.stats"


async def test_invalid_json_output_step_is_passed_through_unvalidated() -> None:
    """The fake does not validate JSON-ness; that is the gateway/consumer's job."""
    provider = FakeLLMProvider(script=[_resp("{not valid json")])
    result = await provider.chat(_req())
    assert result.text == "{not valid json"


async def test_failure_injection_llm_error() -> None:
    provider = FakeLLMProvider(script=[LLMError("upstream unavailable")])
    with pytest.raises(LLMError, match="upstream unavailable"):
        await provider.chat(_req())


async def test_failure_injection_timeout() -> None:
    provider = FakeLLMProvider(script=[TimeoutError("deadline exceeded")])
    with pytest.raises(TimeoutError):
        await provider.chat(_req())


async def test_request_is_recorded_even_when_the_step_raises() -> None:
    provider = FakeLLMProvider(script=[LLMError("boom")])
    with pytest.raises(LLMError):
        await provider.chat(_req())
    assert len(provider.requests) == 1


async def test_exhaustion_raises_clear_error() -> None:
    provider = FakeLLMProvider(script=[_resp("only one")])
    await provider.chat(_req())
    with pytest.raises(ScriptExhaustedError, match="exhausted"):
        await provider.chat(_req())


async def test_stream_chunks_reassemble_to_original_text() -> None:
    original = "The quick brown fox jumps over the lazy dog."
    provider = FakeLLMProvider(script=[_resp(original)])
    chunks = [chunk async for chunk in provider.stream(_req())]
    assert "".join(c.delta_text for c in chunks) == original
    assert [c.done for c in chunks] == [False] * (len(chunks) - 1) + [True]


async def test_stream_records_request_and_consumes_a_script_step() -> None:
    provider = FakeLLMProvider(script=[_resp("abc"), _resp("def")])
    _ = [chunk async for chunk in provider.stream(_req("streamed"))]
    assert provider.requests[0].messages[0].content == "streamed"
    second = await provider.chat(_req())
    assert second.text == "def"


async def test_fake_provider_satisfies_llm_provider_port() -> None:
    """Structural check: FakeLLMProvider is a valid LLMProvider (also checked by mypy strict)."""
    provider: LLMProvider = FakeLLMProvider(script=[_resp("ok")])
    result = await provider.chat(_req())
    assert result.text == "ok"
