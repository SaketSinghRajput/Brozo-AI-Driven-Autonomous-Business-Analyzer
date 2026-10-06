"""One real chat turn against the local Ollama model (TASK-INTEGRATION-007 smoke; marked `llm`).

Run: `APP_LLM_TESTS=1 uv run pytest -m llm tests/llm/test_chat_turn_live.py`. The analytics,
evidence and validator are the real in-memory stack from tests/unit/orchestrator/fakes.py; only
the model is live. Whatever the model writes, every shown number must be a tool result.
"""

import asyncio
from pathlib import Path

import polars as pl
import pytest

from app.core.config import LLMConfig
from app.infra.storage import LocalStorage
from app.llm.gateway import gateway_from_config
from app.ml.anomaly_tool import AnomalyTool
from app.ml.forecast_tool import ForecastTool
from app.orchestrator.domain import AnyTurnEvent, FinalEvent, NumberView, SegmentEvent, TurnState
from app.tools.domain import Tool
from tests.unit.orchestrator.fakes import PROMPTS, Harness, harness
from tests.unit.orchestrator.test_ml_turns import in_memory
from tests.unit.planner.conftest import understand

pytestmark = pytest.mark.llm


async def _collect(h: Harness) -> list[AnyTurnEvent]:
    return [e async for e in h.orchestrator.handle_turn(h.request)]


def test_live_metric_turn_shows_only_tool_numbers() -> None:
    live = gateway_from_config(LLMConfig(), PROMPTS)
    h = harness([], "What was the total revenue in North in August 2026?", llm=live)
    events = asyncio.run(asyncio.wait_for(_collect(h), timeout=600))
    final = events[-1]
    assert isinstance(final, FinalEvent)
    print("live turn:", final.state, final.validation_status, final.usage)
    assert final.state in {TurnState.COMPLETED, TurnState.REQUIRES_REVIEW, TurnState.RECOVERED}
    produced = {r.evidence_id: (r.output or {}).get("value") for _, _, r in h.executor.runs}
    for e in events:
        if isinstance(e, SegmentEvent) and isinstance(e.segment, NumberView):
            assert produced[e.segment.evidence_id] == e.segment.value
    if final.state is TurnState.COMPLETED:
        assert 200.0 in produced.values()  # North, Aug 2026 (hand-computed fixture)


def test_live_forecast_turn_numbers_are_ml_rows(tmp_path: Path) -> None:
    """GAPS-2: the live model routes a forecast question; ML numbers come from code + rows."""
    live = gateway_from_config(LLMConfig(), PROMPTS)
    facts = understand("retail", tmp_path).facts
    with LocalStorage(tmp_path / "store").open_read("v.parquet") as f:
        frame = pl.read_parquet(f)
    tools: list[Tool] = [in_memory(ForecastTool, frame), in_memory(AnomalyTool, frame)]
    question = "Forecast revenue for the coming months."
    h = harness([], question, llm=live, extra_tools=tools, model=facts.model, facts=facts)
    events = asyncio.run(asyncio.wait_for(_collect(h), timeout=600))
    final = events[-1]
    assert isinstance(final, FinalEvent)
    ran = [name for name, _, _ in h.executor.runs]
    print("live forecast turn:", final.state, final.validation_status, ran)
    shown = [e.segment for e in events if isinstance(e, SegmentEvent)]
    for n in [s for s in shown if isinstance(s, NumberView)]:
        assert n.field.startswith("rows.") or n.field == "result.value"
    if final.state is TurnState.COMPLETED and "ml.forecast" in ran:
        assert any(isinstance(s, NumberView) and s.field == "rows.0.value" for s in shown)
