"""TASK-AGENT-007/008/010 golden turns with a scripted FakeLLM over the hand-computed fixture
(tests/unit/analytics/conftest.py: North revenue Jul 245 -> Aug 200, total Aug 540)."""

from typing import Any

from app.llm.errors import LLMFailure, LLMFailureReason
from app.orchestrator.domain import (
    AnyTurnEvent,
    ErrorEvent,
    FinalEvent,
    NumberView,
    SegmentEvent,
    StatusEvent,
    ToolCallEvent,
    TurnState,
    ValidationEvent,
)
from app.orchestrator.orchestrator import HELD, UNAVAILABLE
from app.validator.domain import ValidationStatus
from tests.unit.analytics.conftest import small_orders
from tests.unit.orchestrator.fakes import (
    AUG,
    DONE,
    NORTH,
    compose,
    harness,
    intent,
    num,
    reply,
    text,
)

WHY = "Why did revenue drop in North in Aug 2026?"


def _kinds(events: list[AnyTurnEvent]) -> list[str]:
    return [e.type if not isinstance(e, StatusEvent) else f"status:{e.stage}" for e in events]


def _final(events: list[AnyTurnEvent]) -> FinalEvent:
    assert isinstance(events[-1], FinalEvent)
    assert sum(isinstance(e, FinalEvent) for e in events) == 1
    return events[-1]


def _shown(events: list[AnyTurnEvent]) -> list[Any]:
    return [e.segment for e in events if isinstance(e, SegmentEvent)]


def test_metric_question_every_number_is_a_tool_result_with_evidence() -> None:
    h = harness(
        [
            intent(metrics=["revenue"], filters=NORTH, **AUG),
            DONE,
            compose(text("Revenue in North in August 2026 was "), num("n1"), text(".")),
        ]
    )
    events = h.run()
    assert _kinds(events) == [
        "status:CLASSIFYING",
        "status:PLANNING",
        "status:EXECUTING",
        "tool_call",
        "status:COMPOSING",
        "status:VALIDATING",
        "validation",
        "segment",
        "segment",
        "segment",
        "final",
    ]
    final = _final(events)
    assert (final.state, final.validation_status) == (TurnState.COMPLETED, ValidationStatus.VALID)
    numbers = [s for s in _shown(events) if isinstance(s, NumberView)]
    assert len(numbers) == 1
    (name, args, result) = h.executor.runs[0]
    assert name == "analytics.metric_query"
    assert result.output is not None
    assert numbers[0].value == result.output["value"] == 200.0  # hand-computed
    assert numbers[0].evidence_id == result.evidence_id
    assert numbers[0].unit == "currency:INR"
    assert args["filters"][-1] == {
        "dimension": "Order Date",
        "op": "between",
        "value": ["2026-08-01", "2026-08-31"],
    }
    record = h.recorder.records[0]
    assert record.state is TurnState.COMPLETED
    assert record.validation_id == h.validation.results[-1].id
    assert [c.evidence_ids for c in record.claims] == [[result.evidence_id]]
    assert record.claims[0].text == "Revenue in North in August 2026 was INR 200.00."
    states = record.trace["states"]
    assert isinstance(states, list)
    assert states[-1] == "COMPLETED"


def test_why_question_routes_to_root_cause_with_contribution_wording() -> None:
    h = harness(
        [
            intent(metrics=["Revenue"], filters=NORTH, **AUG),  # LLM said metric_value
            DONE,
            compose(
                text("Revenue in North changed by "),
                num("n1"),
                text(" in August 2026. Apparel contributed most to the change."),
            ),
        ],
        WHY,
    )
    events = h.run()
    tools = [e.name for e in events if isinstance(e, ToolCallEvent)]
    assert tools == ["analytics.explain_change", "analytics.growth"]
    assert _final(events).state is TurnState.COMPLETED
    (number,) = [s for s in _shown(events) if isinstance(s, NumberView)]
    assert number.value == -45.0  # North: 245 -> 200
    rca_args = h.executor.runs[0][1]
    assert rca_args["dimensions"] == ["Category"]  # the filtered Region is not decomposed
    rca_event = next(e for e in events if isinstance(e, ToolCallEvent))
    assert rca_event.findings is not None  # streamed for the RCA panel
    changes = {f.factor: (f.change, f.relation_type) for f in rca_event.findings}
    # North by category (hand-computed): Electronics 200 -> 100, Apparel 45 -> 100
    assert changes["Category=Electronics"] == (-100.0, "contribution")
    assert changes["Category=Apparel"] == (55.0, "contribution")
    shares = {f.factor: f.share for f in rca_event.findings}
    assert shares["Category=Apparel"] == 55.0 / -45.0  # share of the total change (fraction)
    rca_result = h.executor.runs[0][2]
    assert rca_result.evidence_id is not None
    preview = h.executor.previews[rca_result.evidence_id]  # GET /evidence/{id}/data rows
    names = {c.name for c in preview.columns}
    assert {"factor", "magnitude", "share_pct", "relation_type"} <= names


def test_rank_answer_binds_numbers_to_ranked_rows() -> None:
    """B5: a ranking has no single value; its ranked rows are facts the composer may cite
    (``rows.<i>.value``), so the turn completes instead of failing the compose check."""
    h = harness(
        [
            intent(objective="rank", metrics=["Revenue"], dimensions=["Region"]),
            DONE,
            compose(text("North ranks first with "), num("n1"), text(".")),
        ],
        "Which region has the most revenue?",
    )
    events = h.run()
    final = _final(events)
    assert (final.state, final.validation_status) == (TurnState.COMPLETED, ValidationStatus.VALID)
    (number,) = [s for s in _shown(events) if isinstance(s, NumberView)]
    assert (number.field, number.value, number.unit) == ("rows.0.value", 445.0, "currency:INR")
    assert h.recorder.records[0].claims[0].text == "North ranks first with INR 445.00."


def test_causal_wording_is_recomposed_once_then_held_for_review() -> None:
    # An unbacked number too: wording-only failures are answered from the analysis template
    # instead (tests/unit/orchestrator/test_rca_answers.py).
    causal = compose(
        text("Revenue fell "), num("n1"), text(" due to weaker Apparel sales in 3 stores.")
    )
    h = harness([intent(metrics=["Revenue"], filters=NORTH, **AUG), DONE, causal, causal], WHY)
    events = h.run()
    kinds = _kinds(events)
    assert kinds.count("status:COMPOSING") == 2
    assert "status:RETRYING" in kinds
    statuses = [e.status for e in events if isinstance(e, ValidationEvent)]
    assert statuses == [ValidationStatus.INVALID, ValidationStatus.REQUIRES_REVIEW]
    first = next(e for e in events if isinstance(e, ValidationEvent))
    assert "CAUSAL_LANGUAGE" in {i.code for i in first.issues}
    final = _final(events)
    assert final.state is TurnState.REQUIRES_REVIEW
    assert [s.text for s in _shown(events)] == [HELD]  # never shown as fact
    recompose_prompt = h.provider.requests[-1].messages[-2].content
    assert "CAUSAL_LANGUAGE" in recompose_prompt
    assert h.recorder.records[0].segments[1].type == "number"  # held answer kept for review


def test_recompose_can_fix_the_answer() -> None:
    h = harness(
        [
            intent(metrics=["Revenue"], filters=NORTH, **AUG),
            DONE,
            compose(text("Revenue fell "), num("n1"), text(" because of Apparel.")),
            compose(text("Revenue fell "), num("n1"), text("; Apparel contributed most.")),
        ],
        WHY,
    )
    events = h.run()
    assert _final(events).validation_status is ValidationStatus.VALID
    assert _final(events).usage.retries == 1


def test_direction_word_shows_the_size_of_a_negative_change() -> None:
    """B-5 (live demo): "fell by -6,978.90" is a double negative. With a direction word the
    number is shown (and stored in the claim) as its absolute value; validation still sees the
    signed evidence value, so "fell by" a negative change is VALID."""
    h = harness(
        [
            intent(metrics=["Revenue"], filters=NORTH, **AUG),
            DONE,
            compose(text("Revenue fell by "), num("n1"), text(". It was "), num("n1"), text(".")),
        ],
        WHY,
    )
    events = h.run()
    assert _final(events).validation_status is ValidationStatus.VALID
    first, second = [s for s in _shown(events) if isinstance(s, NumberView)]
    assert first.value is not None
    assert first.value < 0
    assert (first.absolute, second.absolute) == (True, False)
    claims = h.recorder.records[0].claims
    assert claims[0].text.startswith("Revenue fell by INR ")
    assert "-" not in claims[0].text
    assert "-" in claims[1].text


def test_unknown_metric_is_refused_with_suggestions() -> None:
    h = harness([intent(metrics=["Revenu"], **AUG)])
    events = h.run()
    final = _final(events)
    assert final.state is TurnState.REFUSED
    (segment,) = _shown(events)
    assert "unknown metric" in segment.text
    assert "Revenue" in segment.text
    assert h.executor.runs == []
    assert final.refusal is not None  # the UI offers the suggestions as buttons
    assert (final.refusal.code, final.refusal.message) == ("UNKNOWN_NAME", segment.text)
    assert "Revenue" in final.refusal.suggestions


def test_insufficient_data_refusal_streams_its_reason_code() -> None:
    broken = small_orders().drop_columns(["unit_price"])
    final = _final(harness([intent(metrics=["Revenue"], filters=NORTH, **AUG)], table=broken).run())
    assert final.refusal is not None
    assert final.refusal.code == "INSUFFICIENT_DATA"
    ok = _final(
        harness([intent(metrics=["revenue"], filters=NORTH, **AUG), DONE, compose(text("x"))]).run()
    )
    assert ok.refusal is None


def test_unsupported_and_unavailable_questions_are_refused() -> None:
    for fields in ({"route": "unsupported"}, {"objective": "forecast", "metrics": ["Revenue"]}):
        h = harness([intent(**fields)], "What will revenue be next month?")
        events = h.run()
        assert _final(events).state is TurnState.REFUSED
        assert h.executor.runs == []


def test_missing_period_asks_for_clarification() -> None:
    h = harness([intent(objective="trend", metrics=["Revenue"])], "How is revenue trending?")
    events = h.run()
    assert _final(events).state is TurnState.NEEDS_CLARIFICATION


def test_all_plan_steps_failing_is_insufficient_data() -> None:
    broken = small_orders().drop_columns(["unit_price"])  # Revenue cannot be computed
    h = harness([intent(metrics=["Revenue"], filters=NORTH, **AUG)], table=broken)
    events = h.run()
    assert _final(events).state is TurnState.REFUSED
    assert _shown(events)[0].text == "Insufficient data: query failed."
    assert len(h.provider.requests) == 1  # no loop / compose call after nothing succeeded


def test_llm_down_gives_degraded_mode_and_no_fabricated_answer() -> None:
    down = LLMFailure(LLMFailureReason.UNAVAILABLE, "connection refused")
    h = harness([down, down, down])
    events = h.run()
    final = _final(events)
    assert (final.state, final.mode) == (TurnState.RECOVERED, "degraded")
    (error,) = [e for e in events if isinstance(e, ErrorEvent)]
    assert error.error.error_code == "LLM_ERROR"
    assert [s.text for s in _shown(events)] == [UNAVAILABLE]
    assert h.executor.runs == []


def test_open_circuit_is_degraded_without_calling_the_model() -> None:
    h = harness([])
    breaker = h.gateway.breaker("default")
    for _ in range(5):
        breaker.record_failure()
    events = h.run()
    assert _final(events).mode == "degraded"
    assert h.provider.requests == []


def test_llm_failure_after_tools_shows_only_verified_facts() -> None:
    down = LLMFailure(LLMFailureReason.TIMEOUT, "timed out")
    h = harness([intent(metrics=["Revenue"], filters=NORTH, **AUG), DONE, down, down, down])
    events = h.run()
    final = _final(events)
    assert (final.state, final.validation_status) == (TurnState.RECOVERED, ValidationStatus.VALID)
    numbers = [s for s in _shown(events) if isinstance(s, NumberView)]
    assert [n.value for n in numbers] == [200.0]


def test_simple_metric_question_makes_exactly_one_successful_tool_call() -> None:
    """B-4 (live demo): after the plan answered, qwen re-called the planned tool with partial
    arguments (``metric_query(dimension, filters, metric) -> TOOL_ERROR``). Such a repeat of an
    already-answered plan tool is not run."""
    repeat = reply(calls=[("analytics.metric_query", {"metric": "Revenue", "dimension": "Nope"})])
    h = harness(
        [
            intent(metrics=["revenue"], filters=NORTH, **AUG),
            repeat,
            compose(text("Revenue in North in August 2026 was "), num("n1"), text(".")),
        ]
    )
    events = h.run()
    calls = [e for e in events if isinstance(e, ToolCallEvent)]
    assert [(c.name, c.status) for c in calls] == [("analytics.metric_query", "ok")]
    assert len(h.executor.runs) == 1
    final = _final(events)
    assert (final.state, final.validation_status) == (TurnState.COMPLETED, ValidationStatus.VALID)
