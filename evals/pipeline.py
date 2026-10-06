"""Shared eval plumbing: a CSV understood through the production path, and a chat-turn rig.

``understand_csv``: FileInspector -> ingest_file (Parquet) -> profile_frame -> understand_frame ->
DomainDetector -> SemanticMapper, as the dataset pipeline runs them, without a database (same
steps as backend/tests/unit/planner/conftest.py). Stage wall times are returned for BENCH-008.

``rig``: the real Orchestrator over one understood table, with the in-memory executor, validation
and recorder from backend/tests/unit/orchestrator/fakes.py (real tools, real evidence recorder,
real v1 validation pipeline); only the LLM gateway is swapped (FakeLLM script or live Ollama).

Used by evals/golden/build.py, evals/llm/run.py, evals/regression/run.py and scripts/bench/.
Run from backend/ (``uv run python ../evals/...``) so ``app`` and ``tests`` import.
"""

import sys
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import polars as pl  # noqa: E402

from app.analytics.sql.engine import AnalyticalEngine  # noqa: E402
from app.analytics.tools import analytics_tools  # noqa: E402
from app.core.clock import FrozenClock  # noqa: E402
from app.core.ids import SequentialIds  # noqa: E402
from app.core.scope import WorkspaceScope  # noqa: E402
from app.data.domain import ParseLimits  # noqa: E402
from app.data.ingestion.inspector import FileInspector, InspectionLimits  # noqa: E402
from app.data.ingestion.pipeline import PipelineLimits, ingest_file, parser_for  # noqa: E402
from app.data.profiling.domain import ProfileResult  # noqa: E402
from app.data.profiling.profiler import profile_frame  # noqa: E402
from app.data.profiling.service import ingest_columns  # noqa: E402
from app.data.semantic.domain import SemanticModel  # noqa: E402
from app.data.semantic.mapper import SemanticMapper  # noqa: E402
from app.data.semantic.overrides import remap  # noqa: E402
from app.data.understanding.analyzer import SchemaAnalyzer  # noqa: E402
from app.data.understanding.domain import DomainClassification, Understanding  # noqa: E402
from app.data.understanding.domain_detector import DomainDetector  # noqa: E402
from app.data.understanding_pipeline import understand_frame  # noqa: E402
from app.infra.storage import LocalStorage  # noqa: E402
from app.llm.context import ContextBuilder  # noqa: E402
from app.llm.gateway import LLMGateway  # noqa: E402
from app.llm.prompts import PromptRegistry  # noqa: E402
from app.orchestrator.composer import ResponseComposer  # noqa: E402
from app.orchestrator.domain import TurnRequest  # noqa: E402
from app.orchestrator.intent import IntentResolver  # noqa: E402
from app.orchestrator.orchestrator import Orchestrator  # noqa: E402
from app.orchestrator.planner import QuestionPlanner  # noqa: E402
from app.orchestrator.tool_loop import ToolLoop  # noqa: E402
from app.tools.permissions import Role  # noqa: E402
from app.tools.registry import ToolRegistry  # noqa: E402
from tests.unit.orchestrator.fakes import (  # noqa: E402
    NOW,
    MemoryExecutor,
    MemoryRecorder,
    MemorySource,
    MemoryValidation,
    Step,
    gateway,
)

FIXTURES = ROOT / "evals" / "fixtures"
TABLES = {  # one table per dataset version (single-table scope of the dataset pipeline)
    "retail": "orders",
    "saas": "subscriptions",
    "agriculture": "production",
    "insufficient": "sales",
    "ambiguous": "data",
}
LIMITS = PipelineLimits(
    ParseLimits(max_rows=5_000_000, max_reject_ratio=0.25, max_field_chars=100_000),
    batch_rows=50_000,
)
SCOPE = WorkspaceScope(workspace_id="ws_e7a1000001", principal_id="usr_eval")
ENGINE = AnalyticalEngine()


@dataclass
class Understood:
    name: str
    frame: pl.DataFrame
    profile: ProfileResult
    understanding: Understanding
    domain: DomainClassification
    model: SemanticModel
    stages_s: dict[str, float] = field(default_factory=dict)


def understand_csv(path: Path, table: str, workdir: Path) -> Understood:
    """Production ingest -> profile -> understand -> domain -> semantic mapping, timed per stage."""
    stages: dict[str, float] = {}
    started = time.perf_counter()

    def lap(stage: str) -> None:
        nonlocal started
        now = time.perf_counter()
        stages[stage] = now - started
        started = now

    workdir.mkdir(parents=True, exist_ok=True)
    storage = LocalStorage(workdir / "store")
    facts = FileInspector(InspectionLimits(max_bytes=1 << 31)).inspect(path)
    report = ingest_file(
        facts,
        parser_for(facts, None, LIMITS),
        filename=path.name,
        limits=LIMITS,
        storage=storage,
        key="v.parquet",
        workdir=workdir,
    )
    with storage.open_read("v.parquet") as f:
        frame = pl.read_parquet(f)
    lap("ingest")
    profile = profile_frame(frame, f"dsv_{table}", ingest_columns(report.model_dump(mode="json")))
    lap("profile")
    understanding = understand_frame(SchemaAnalyzer(), table, profile, frame)
    domain = DomainDetector().detect([understanding])
    model = remap(SemanticMapper(), understanding, domain, table=table, overrides=[]).model
    lap("understand")
    return Understood(table, frame, profile, understanding, domain, model, stages)


def understand_fixture(name: str, workdir: Path) -> Understood:
    table = TABLES[name]
    return understand_csv(FIXTURES / name / f"{table}.csv", table, workdir / name)


@dataclass
class Rig:
    orchestrator: Orchestrator
    executor: MemoryExecutor
    validation: MemoryValidation
    recorder: MemoryRecorder
    request: TurnRequest


def rig(
    data: Understood,
    question: str,
    *,
    script: Iterable[Step] = (),
    llm: LLMGateway | None = None,
    prompts: PromptRegistry | None = None,
) -> Rig:
    """One chat turn over ``data``: FakeLLM ``script`` unless a live ``llm`` gateway is given."""
    prompts = prompts or PromptRegistry()
    model = data.model
    registry = ToolRegistry(analytics_tools(MemorySource(model, data.frame.to_arrow()), ENGINE))
    executor = MemoryExecutor(registry)
    validation = MemoryValidation(executor, model)
    recorder = MemoryRecorder()
    gw = llm or gateway(script)[0]
    context = ContextBuilder()
    orchestrator = Orchestrator(
        intents=IntentResolver(gw, prompts, context),
        planner=QuestionPlanner(registry, SequentialIds()),
        tool_loop=ToolLoop(gw, prompts, context),
        composer=ResponseComposer(gw, prompts, context),
        validation=validation,
        executor=executor,
        registry=registry,
        recorder=recorder,
        clock=FrozenClock(NOW),
    )
    request = TurnRequest(
        scope=SCOPE,
        role=Role.ANALYST,
        question=question,
        conversation_id="cv_eval",
        message_id=f"msg_{uuid.uuid4().hex}",
        dataset_version_id=model.dataset_version_id,
        semantic_model=model,
        trace_id="trace-eval",
    )
    return Rig(orchestrator, executor, validation, recorder, request)
