"""RCA-FIX: explain_change dimensions are chosen in code, not by the LLM; an answer whose only
failures are wording issues is rewritten from the analysis results by code (and revalidated).

The retail fixture's seeded driver (evals/fixtures/retail/labels.json: North, 2026-08,
expected_dominant_category Electronics) lives in products.csv, so the test joins orders with
products (one table, as a user would upload it) and runs a real turn over it.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import polars as pl
import pytest

from app.orchestrator import orchestrator as orchestrator_module
from app.orchestrator.domain import (
    AnyTurnEvent,
    FinalEvent,
    NumberView,
    SegmentEvent,
    ToolCallEvent,
    TurnState,
    ValidationEvent,
)
from app.orchestrator.fallback import NOT_CAUSES
from app.orchestrator.intent import IntentOutput, post_check
from app.orchestrator.orchestrator import HELD
from app.validator.domain import AnswerSegment, CaveatSegment, TextSegment, ValidationStatus
from tests.unit.analytics.conftest import semantic_model
from tests.unit.orchestrator.fakes import (
    AUG,
    DONE,
    NORTH,
    Harness,
    compose,
    harness,
    intent,
    num,
    text,
)

EVALS = Path(__file__).resolve().parents[4] / "evals"
RETAIL = EVALS / "fixtures" / "retail"
WHY = "Why did revenue drop in North in August 2026?"
SMALL_WHY = "Why did revenue drop in North in Aug 2026?"  # tests/unit/analytics/conftest
CAUSAL = compose(text("Revenue fell "), num("n1"), text(" due to weaker Apparel sales."))


def _pipeline() -> ModuleType:
    spec = importlib.util.spec_from_file_location("eval_pipeline_rca", EVALS / "pipeline.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["eval_pipeline_rca"] = module
    spec.loader.exec_module(module)
    return module


def _why(dimensions: list[str], question: str = WHY) -> list[str]:
    out = IntentOutput.model_validate(
        {
            "route": "structured",
            "objective": "explain_change",
            "metrics": ["Revenue"],
            "dimensions": dimensions,
            "filters": NORTH,
            **AUG,
        }
    )
    resolved = post_check(out, question, semantic_model())
    assert resolved.intent is not None
    return resolved.intent.dimensions


def test_a_dimension_the_user_did_not_name_is_not_used_for_explain_change() -> None:
    assert _why(["Category"]) == []
    named = "Why did revenue drop by category in North in August 2026?"
    assert _why(["Category"], named) == ["Category"]


@pytest.fixture(scope="module")
def retail_joined(tmp_path_factory: pytest.TempPathFactory) -> Any:
    work = tmp_path_factory.mktemp("retail_joined")
    orders = pl.read_csv(RETAIL / "orders.csv", infer_schema_length=0)
    products = pl.read_csv(RETAIL / "products.csv", infer_schema_length=0)
    joined = orders.join(products.select("product_id", "category"), on="product_id", how="left")
    path = work / "orders.csv"
    joined.write_csv(path)
    return _pipeline().understand_csv(path, "orders", work)


def run_turn(data: Any, question: str, script: list[Any]) -> tuple[Any, list[AnyTurnEvent]]:
    rig = sys.modules["eval_pipeline_rca"].rig(data, question, script=script)

    async def collect() -> list[AnyTurnEvent]:
        return [e async for e in rig.orchestrator.handle_turn(rig.request)]

    return rig, asyncio.run(asyncio.wait_for(collect(), timeout=120))


def test_north_august_why_puts_the_seeded_driver_first(retail_joined: Any) -> None:
    why = intent(
        objective="explain_change",
        metrics=["Revenue"],
        dimensions=["Sales Channel"],  # the LLM picked a dimension the user never named
        filters=NORTH,
        **AUG,
    )
    script = [why, DONE, compose(text("Revenue in North changed by "), num("n1"), text("."))]
    rig, events = run_turn(retail_joined, WHY, script)
    rca = next(e for e in events if isinstance(e, ToolCallEvent))
    assert rca.name == "analytics.explain_change"
    dims = rig.executor.runs[0][1]["dimensions"]
    assert "Product Category" in dims  # the mapped name of `category`
    assert "Sales Channel" in dims
    assert "Region" not in dims
    assert rca.findings is not None
    assert rca.findings[0].factor == "Product Category=Electronics"


# --- deterministic answer fallback (wording-only failures) ----------------------------------


def _small(*script: Any) -> tuple[Harness, list[AnyTurnEvent]]:
    h = harness([intent(metrics=["Revenue"], filters=NORTH, **AUG), DONE, *script], SMALL_WHY)
    return h, h.run()


def _statuses(events: list[AnyTurnEvent]) -> list[ValidationStatus]:
    return [e.status for e in events if isinstance(e, ValidationEvent)]


def _final(events: list[AnyTurnEvent]) -> FinalEvent:
    final = events[-1]
    assert isinstance(final, FinalEvent)
    return final


def test_causal_wording_twice_is_answered_from_the_analysis_template() -> None:
    h, events = _small(CAUSAL, CAUSAL)
    assert _statuses(events) == [
        ValidationStatus.INVALID,
        ValidationStatus.REQUIRES_REVIEW,
        ValidationStatus.VALID,  # the template, validated on its own merits
    ]
    final = _final(events)
    assert (final.state, final.validation_status) == (TurnState.COMPLETED, ValidationStatus.VALID)
    assert final.answer_source == "template"
    shown = [e.segment for e in events if isinstance(e, SegmentEvent)]
    numbers = [s for s in shown if isinstance(s, NumberView)]
    rca = h.executor.runs[0][2].evidence_id
    assert (numbers[0].evidence_id, numbers[0].field, numbers[0].value) == (
        rca,
        "result.value",
        -45.0,
    )
    assert numbers[0].absolute  # "fell by" states the sign
    factors = [(n.field, n.value) for n in numbers if n.evidence_id == rca][1:]
    assert factors[0] == ("rows.0.magnitude", -100.0)  # Electronics 200 -> 100
    record = h.recorder.records[0]
    assert record.trace["answer_source"] == "template"
    claims = [c.text for c in record.claims]
    assert claims[0] == (
        "Revenue (Region North) fell by INR 45.00 (-18.4%) from the period 2026-07-01 to "
        "2026-07-31 to the period 2026-08-01 to 2026-08-31."
    )
    assert "Category=Electronics" in claims[1]
    caveats = [s.text for s in shown if isinstance(s, CaveatSegment)]
    assert NOT_CAUSES in caveats
    assert "due to" not in "".join(s.text for s in shown if isinstance(s, TextSegment))


def test_a_numeric_issue_is_never_papered_over_by_the_template() -> None:
    wrong = compose(text("Revenue fell by 999 due to weaker Apparel sales."))
    _, events = _small(wrong, wrong)
    assert _statuses(events) == [ValidationStatus.INVALID, ValidationStatus.REQUIRES_REVIEW]
    final = _final(events)
    assert final.state is TurnState.REQUIRES_REVIEW
    assert final.answer_source is None
    shown = [e.segment for e in events if isinstance(e, SegmentEvent)]
    assert [s.text for s in shown if isinstance(s, CaveatSegment)] == [HELD]


def test_a_template_that_fails_validation_stays_held(monkeypatch: pytest.MonkeyPatch) -> None:
    def causal_template(objective: str, calls: Any) -> list[AnswerSegment]:
        return [TextSegment(text="Revenue fell due to Apparel.")]

    monkeypatch.setattr(orchestrator_module, "template_answer", causal_template)
    h, events = _small(CAUSAL, CAUSAL)
    assert _statuses(events)[-1] is ValidationStatus.REQUIRES_REVIEW
    final = _final(events)
    assert final.state is TurnState.REQUIRES_REVIEW
    assert final.answer_source is None
    assert h.recorder.records[0].trace["answer_source"] is None
