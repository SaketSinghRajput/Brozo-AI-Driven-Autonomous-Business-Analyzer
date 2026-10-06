"""In-memory stand-ins for the database-backed parts of a chat turn (no PostgreSQL, no model).

``MemoryExecutor`` keeps the real ToolExecutor guard chain (registry, strict args, permission,
budget, timeout, output validation) and only swaps persistence: evidence and tool runs go to
dicts. ``MemoryValidation`` runs the real v1 ValidationPipeline over that evidence. The LLM is the
scripted ``FakeLLMProvider`` behind a real ``LLMGateway``.
"""

import asyncio
import json
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

import pyarrow as pa

from app.analytics.source import DatasetSource, LoadedDataset
from app.analytics.sql.engine import AnalyticalEngine
from app.analytics.tools import analytics_tools
from app.core.clock import FrozenClock
from app.core.ids import SequentialIds
from app.core.scope import WorkspaceScope
from app.data.semantic.domain import SemanticModel
from app.evidence.domain import Evidence, EvidenceResult
from app.evidence.preview import RowsPreview, rows_preview
from app.evidence.recorder import CallKey, EvidenceRecorder, result_hash
from app.infra.db import UnitOfWork
from app.llm.context import ContextBuilder
from app.llm.fake import FakeLLMProvider
from app.llm.gateway import LLMGateway
from app.llm.ports import ChatResponse, ToolCall, Usage
from app.llm.prompts import PromptRegistry
from app.llm.router import ModelProfile, ModelRouter
from app.orchestrator.composer import ResponseComposer
from app.orchestrator.domain import AnyTurnEvent, TurnLimits, TurnRecord, TurnRequest
from app.orchestrator.intent import IntentResolver
from app.orchestrator.orchestrator import Orchestrator
from app.orchestrator.planner import QuestionPlanner
from app.orchestrator.tool_loop import ToolLoop
from app.planner.domain import Unavailability
from app.planner.facts import DataFacts
from app.tools.domain import ErrorInfo, Tool, ToolContext, ToolMetadata, ToolResult
from app.tools.executor import ToolExecutor
from app.tools.permissions import Role
from app.tools.registry import ToolRegistry
from app.validator.domain import ValidationRequest, ValidationResult, cited_ids
from app.validator.pipeline import build_pipeline
from tests.unit.analytics.conftest import semantic_model, small_orders

NOW = datetime(2026, 9, 28, tzinfo=UTC)
PROMPTS = PromptRegistry()
Step = ChatResponse | BaseException


class MemorySource(DatasetSource):
    def __init__(self, model: SemanticModel, table: pa.Table) -> None:
        self._loaded = LoadedDataset(model=model, table=table)

    def load(
        self, scope: WorkspaceScope, dataset_version_id: str, semantic_model_version: int
    ) -> LoadedDataset:
        return self._loaded


def _number(value: Any) -> Any:
    return value if isinstance(value, int | float) and not isinstance(value, bool) else None


class MemoryExecutor(ToolExecutor):
    def __init__(self, registry: ToolRegistry) -> None:
        super().__init__(
            registry, cast(UnitOfWork, None), EvidenceRecorder(FrozenClock(NOW)), SequentialIds()
        )
        self.evidence: dict[str, Evidence] = {}
        self.previews: dict[str, RowsPreview] = {}  # evidence.result_rows
        self.runs: list[tuple[str, dict[str, Any], ToolResult]] = []

    def _persist(
        self,
        name: str,
        meta: ToolMetadata | None,
        args: dict[str, Any],
        output: dict[str, Any] | None,
        unavailable: Unavailability | None,
        error: ErrorInfo | None,
        ctx: ToolContext,
        key: CallKey,
        elapsed_ms: int,
    ) -> ToolResult:
        evidence_id = None
        if meta is not None and output is not None and meta.produces_evidence:
            evidence_id = key.evidence_id
            unit = output.get("unit")
            self.evidence[evidence_id] = Evidence(
                evidence_id=evidence_id,
                workspace_id=ctx.workspace_id,
                kind="tool_result",
                dataset_id="ds_test",
                dataset_version_id=ctx.dataset_version_id,
                dataset_content_hash="sha256:" + "0" * 64,
                semantic_model_version=ctx.semantic_model_version,
                operation=meta.name,
                tool_version=meta.version,
                arguments=args,
                result=EvidenceResult(
                    value=_number(output.get("value")),
                    unit=unit if isinstance(unit, str) else None,
                    rows=_number(output.get("rows")),
                    result_hash=result_hash(output),
                ),
                created_at=NOW,
                software_version="test",
            )
            preview = rows_preview(output)
            if preview is not None:
                self.previews[evidence_id] = preview
        status = "error" if error else "unavailable" if unavailable else "ok"
        result = ToolResult(
            tool=meta.name if meta else "unknown.tool",
            tool_version=meta.version if meta else "0.0.0",
            status=status,
            output=output if status == "ok" else None,
            unavailable=unavailable if status == "unavailable" else None,
            error=error,
            evidence_id=evidence_id,
            duration_ms=elapsed_ms,
        )
        self.runs.append((name, args, result))
        return result


class MemoryValidation:
    def __init__(self, executor: MemoryExecutor, model: SemanticModel) -> None:
        self._executor = executor
        self._models = {model.dataset_version_id: model}
        self._ids = SequentialIds()
        self.results: list[ValidationResult] = []

    async def validate(
        self, scope: WorkspaceScope, req: ValidationRequest, *, repair_exhausted: bool = False
    ) -> ValidationResult:
        store = self._executor.evidence
        bundle = {
            i: store[i]
            for i in cited_ids(req)
            if i in store and store[i].workspace_id == scope.workspace_id
        }
        previews = {i: p for i, p in self._executor.previews.items() if i in bundle}
        pipeline = build_pipeline(scope.workspace_id, self._models, self._ids, previews)
        result = await pipeline.run(req, bundle, repair_exhausted=repair_exhausted)
        self.results.append(result)
        return result


@dataclass
class MemoryRecorder:
    records: list[TurnRecord] = field(default_factory=list)

    async def record(self, scope: WorkspaceScope, record: TurnRecord) -> None:
        self.records.append(record)


async def _no_sleep(_: float) -> None:
    return None


def gateway(script: Iterable[Step], **kw: Any) -> tuple[LLMGateway, FakeLLMProvider]:
    provider = FakeLLMProvider(script)
    profile = ModelProfile(
        name="default",
        provider="fake",
        model_version="fake-1",
        max_context=32_768,
        capabilities=provider.capabilities,
    )
    router = ModelRouter({"default": profile})
    return LLMGateway(router, {"default": provider}, PROMPTS, sleep=_no_sleep, **kw), provider


Calls = Sequence[tuple[str, dict[str, Any]]]


def reply(text: str | None = None, calls: Calls = ()) -> ChatResponse:
    tool_calls = [ToolCall(id=f"c{i}", name=n, arguments=a) for i, (n, a) in enumerate(calls)]
    return ChatResponse(
        text=text,
        tool_calls=tool_calls,
        model_version="fake-1",
        usage=Usage(prompt_tokens=10, completion_tokens=5),
        finish_reason="tool_calls" if tool_calls else "stop",
    )


def as_json(value: Any) -> ChatResponse:
    return reply(json.dumps(value))


def intent(**fields: Any) -> ChatResponse:
    return as_json({"route": "structured", "objective": "metric_value", **fields})


def compose(*segments: dict[str, Any]) -> ChatResponse:
    return as_json({"segments": list(segments)})


def text(s: str) -> dict[str, Any]:
    return {"type": "text", "text": s}


def num(ref: str) -> dict[str, Any]:
    return {"type": "number", "ref": ref}


NORTH = [{"dimension": "Region", "op": "eq", "value": "North"}]
AUG = {"start": "2026-08-01", "end": "2026-08-31"}
DONE = reply("I have what I need.")  # ToolLoop round without further calls


@dataclass
class Harness:
    orchestrator: Orchestrator
    gateway: LLMGateway
    provider: FakeLLMProvider
    executor: MemoryExecutor
    validation: MemoryValidation
    recorder: MemoryRecorder
    request: TurnRequest

    def run(self) -> list[AnyTurnEvent]:
        async def collect() -> list[AnyTurnEvent]:
            return [e async for e in self.orchestrator.handle_turn(self.request)]

        return asyncio.run(asyncio.wait_for(collect(), timeout=30))


def harness(
    script: Iterable[Step],
    question: str = "What was revenue in North in August 2026?",
    *,
    limits: TurnLimits | None = None,
    role: Role = Role.ANALYST,
    table: pa.Table | None = None,
    extra_tools: Sequence[Tool] = (),
    gateway_kw: dict[str, Any] | None = None,
    llm: LLMGateway | None = None,
    model: SemanticModel | None = None,
    facts: DataFacts | None = None,
) -> Harness:
    model = model or semantic_model()
    source = MemorySource(model, table if table is not None else small_orders())
    registry = ToolRegistry([*analytics_tools(source, AnalyticalEngine()), *extra_tools])
    executor = MemoryExecutor(registry)
    validation = MemoryValidation(executor, model)
    recorder = MemoryRecorder()
    gw, provider = gateway(script, **(gateway_kw or {}))
    gw = llm or gw  # a live gateway for tests/llm; the fake provider then stays unused
    context = ContextBuilder()
    orchestrator = Orchestrator(
        intents=IntentResolver(gw, PROMPTS, context),
        planner=QuestionPlanner(registry, SequentialIds()),
        tool_loop=ToolLoop(gw, PROMPTS, context),
        composer=ResponseComposer(gw, PROMPTS, context),
        validation=validation,
        executor=executor,
        registry=registry,
        recorder=recorder,
        clock=FrozenClock(NOW),
        limits=limits,
    )
    request = TurnRequest(
        scope=WorkspaceScope(workspace_id="ws_abcdef", principal_id="usr_1"),
        role=role,
        question=question,
        conversation_id="cv_1",
        message_id=f"msg_{uuid.uuid4().hex}",
        dataset_version_id=model.dataset_version_id,
        semantic_model=model,
        trace_id="trace-test",
        facts=facts,
    )
    return Harness(orchestrator, gw, provider, executor, validation, recorder, request)
