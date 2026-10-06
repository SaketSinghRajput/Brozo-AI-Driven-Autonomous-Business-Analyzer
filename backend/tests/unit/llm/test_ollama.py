"""TASK-LLM-007: OllamaAdapter wire mapping and error mapping, via httpx.MockTransport (no LLM)."""

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.core.config import LLMConfig
from app.llm.errors import LLMFailure, LLMFailureReason
from app.llm.ollama import OllamaAdapter
from app.llm.ports import ChatRequest, GenParams, Message
from app.llm.resilience import HostAllowList

pytestmark = pytest.mark.anyio

LOCAL = HostAllowList(["127.0.0.1", "localhost"])


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _adapter(handler: Callable[[httpx.Request], httpx.Response]) -> OllamaAdapter:
    return OllamaAdapter(
        endpoint="http://127.0.0.1:11434",
        model="qwen2.5:7b",
        allow_list=LOCAL,
        num_ctx=4096,
        transport=httpx.MockTransport(handler),
    )


def _req(**kw: Any) -> ChatRequest:
    return ChatRequest(
        messages=[
            Message(role="system", content="sys"),
            Message(role="tool", content='{"v": 1}', name="analytics.stats"),
        ],
        prompt_id="p",
        prompt_version="v",
        trace_id="t",
        **kw,
    )


def _ok(message: dict[str, Any], **extra: Any) -> httpx.Response:
    body = {
        "model": "qwen2.5:7b",
        "message": {"role": "assistant", **message},
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 12,
        "eval_count": 5,
        **extra,
    }
    return httpx.Response(200, json=body)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://api.example.com:11434",
        "http://user:pw@127.0.0.1:11434",
        "file:///etc/passwd",
        "http://127.0.0.1.nip.io:11434",
    ],
)
def test_non_allow_listed_endpoint_refused(endpoint: str) -> None:
    with pytest.raises(LLMFailure) as err:
        OllamaAdapter(endpoint=endpoint, model="m", allow_list=LOCAL)
    assert err.value.reason is LLMFailureReason.HOST_NOT_ALLOWED
    assert err.value.retryable is False


@pytest.mark.parametrize("think", [None, True, False])
async def test_think_is_sent_only_when_configured(think: bool | None) -> None:
    """F3: Ollama's `think` flag passes through only when set (default: field absent)."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _ok({"content": "{}"})

    adapter = OllamaAdapter(
        endpoint="http://127.0.0.1:11434",
        model="qwen3.5:4b",
        allow_list=LOCAL,
        think=think,
        transport=httpx.MockTransport(handler),
    )
    await adapter.chat(_req())
    assert ("think" in seen["body"]) is (think is not None)
    if think is not None:
        assert seen["body"]["think"] is think


def test_llm_config_think_defaults_to_unset() -> None:
    assert LLMConfig().think is None
    assert LLMConfig(think=False).think is False
    assert LLMConfig().model == "qwen2.5:7b"  # the default model is unchanged


async def test_request_payload_mapping() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return _ok({"content": "{}"})

    schema: dict[str, Any] = {"type": "object"}
    tools: list[dict[str, Any]] = [{"type": "function", "function": {"name": "analytics.stats"}}]
    await _adapter(handler).chat(
        _req(json_schema=schema, tools=tools, params=GenParams(max_tokens=50, seed=7))
    )
    body = seen["body"]
    assert seen["path"] == "/api/chat"
    assert body["model"] == "qwen2.5:7b"
    assert body["stream"] is False
    assert body["format"] == schema
    assert body["tools"] == tools
    assert body["options"] == {"temperature": 0.0, "num_ctx": 4096, "num_predict": 50, "seed": 7}
    assert body["messages"][1] == {
        "role": "tool",
        "content": '{"v": 1}',
        "tool_name": "analytics.stats",
    }


async def test_response_mapping_text_and_usage() -> None:
    resp = await _adapter(lambda _: _ok({"content": "hello"})).chat(_req())
    assert (resp.text, resp.finish_reason, resp.model_version) == (
        "hello",
        "stop",
        "ollama:qwen2.5:7b",
    )
    assert (resp.usage.prompt_tokens, resp.usage.completion_tokens) == (12, 5)


async def test_response_mapping_tool_calls() -> None:
    message = {
        "content": "",
        "tool_calls": [
            {"function": {"index": 0, "name": "ml.forecast", "arguments": {"horizon": 6}}}
        ],
    }
    resp = await _adapter(lambda _: _ok(message)).chat(_req())
    assert resp.finish_reason == "tool_calls"
    assert resp.text is None
    assert resp.tool_calls[0].model_dump() == {
        "id": "call_0",
        "name": "ml.forecast",
        "arguments": {"horizon": 6},
    }


async def test_length_finish_reason() -> None:
    resp = await _adapter(lambda _: _ok({"content": "cut"}, done_reason="length")).chat(_req())
    assert resp.finish_reason == "length"


@pytest.mark.parametrize(
    ("status", "reason", "retryable"),
    [
        (500, LLMFailureReason.PROVIDER_ERROR, True),
        (503, LLMFailureReason.PROVIDER_ERROR, True),
        (429, LLMFailureReason.PROVIDER_ERROR, True),
        (400, LLMFailureReason.UNSUPPORTED, False),
        (404, LLMFailureReason.UNSUPPORTED, False),
    ],
)
async def test_http_errors_mapped(status: int, reason: LLMFailureReason, retryable: bool) -> None:
    adapter = _adapter(lambda _: httpx.Response(status, json={"error": "x"}))
    with pytest.raises(LLMFailure) as err:
        await adapter.chat(_req())
    assert (err.value.reason, err.value.retryable) == (reason, retryable)


async def test_connection_and_timeout_errors_are_transient() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(LLMFailure) as err:
        await _adapter(refuse).chat(_req())
    assert (err.value.reason, err.value.retryable) == (LLMFailureReason.UNAVAILABLE, True)
    with pytest.raises(LLMFailure) as err:
        await _adapter(slow).chat(_req())
    assert (err.value.reason, err.value.retryable) == (LLMFailureReason.TIMEOUT, True)


async def test_stream_parses_ndjson() -> None:
    lines = [
        {"message": {"content": "Hel"}, "done": False},
        {"message": {"content": "lo"}, "done": False},
        {"message": {"content": ""}, "done": True, "eval_count": 2},
    ]
    body = "\n".join(json.dumps(line) for line in lines) + "\n"
    adapter = _adapter(lambda _: httpx.Response(200, content=body.encode()))
    chunks = [c async for c in adapter.stream(_req())]
    assert "".join(c.delta_text for c in chunks) == "Hello"
    assert [c.done for c in chunks] == [False, False, True]


async def test_stream_without_done_is_an_error() -> None:
    body = json.dumps({"message": {"content": "partial"}, "done": False}) + "\n"
    adapter = _adapter(lambda _: httpx.Response(200, content=body.encode()))
    with pytest.raises(LLMFailure) as err:
        _ = [c async for c in adapter.stream(_req())]
    assert err.value.reason is LLMFailureReason.PROVIDER_ERROR


def test_proxy_environment_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """trust_env=False: a proxy env var must never route prompts off-host."""
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.com:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.example.com:3128")
    adapter = OllamaAdapter(endpoint="http://127.0.0.1:11434", model="m", allow_list=LOCAL)
    # httpx mounts one proxy transport per env proxy when trust_env is on; none may exist.
    assert adapter._client._mounts == {}
    control = httpx.AsyncClient()
    assert control._mounts != {}  # proves the env vars would have been honoured by default
