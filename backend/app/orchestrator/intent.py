"""IntentResolver (TASK-AGENT-002, design §32 and §37).

The LLM proposes an ``IntentOutput`` through ``LLMGateway.structured`` (schema-constrained, one
repair round). Deterministic post-checks then decide what happens:
- every metric/dimension/filter name is resolved through the analytics ``SemanticResolver``;
  an unknown name refuses with suggestions taken from the semantic model (never from the LLM);
- routes: ``unsupported`` refuses; ``simulate`` and document-only questions are not available in
  P1 (no RAG / scenario engine yet); ``document``/``mixed`` with resolvable metrics are answered
  from the table only, with a caveat (design §37 promotion rule);
- a "why" question about a metric change is routed to ``explain_change`` whatever the LLM said;
  its dimensions are kept only when the question names them (name or column), so the planner,
  not the LLM, picks what a change is decomposed by (RCA-FIX);
- normalization (F1, B1, B4): a period the model put into ``filters`` on a time dimension
  (eq/between/gte/lte with a year, month or date) becomes start/end with its grain; a time
  dimension in ``dimensions`` is dropped (never a grouping) and names the period's date
  (``time_dimension``); ``compare_to: previous_year`` maps to ``same_period_last_year``;
- missing metrics or an unusable date range ask for clarification. Clarification and refusal
  texts are fixed strings plus semantic names: no LLM-written text reaches the user unvalidated.
"""

import re
from datetime import date
from typing import Literal

from pydantic import Field, ValidationError

from app.analytics.sql.resolver import SemanticResolver
from app.core.errors import SchemaError
from app.data.semantic.domain import SemanticDimension, SemanticModel
from app.llm.context import ContextBuilder, ContextPart
from app.llm.gateway import LLMGateway
from app.llm.ports import LLMTask
from app.llm.prompts import PromptRegistry
from app.orchestrator.domain import Contract, Refusal
from app.orchestrator.periods import lift
from app.orchestrator.state import TurnStateMachine
from app.planner.domain import AnalysisIntent, Filter, TimeSpec

Route = Literal["structured", "document", "mixed", "simulate", "unsupported", "clarify"]
Objective = Literal[
    "overview",
    "metric_value",
    "trend",
    "compare",
    "rank",
    "explain_change",
    "forecast",
    "anomalies",
    "segments",
    "predict",
    "simulate",
    "document_lookup",
]
PROMPT = "intent.resolve"
CONTEXT_BUDGET = 3000
_WHY = re.compile(r"\bwhy\b", re.IGNORECASE)
NO_DOCUMENTS = "Document search is not available yet; this answer uses the table only."
COMPARE_TO: dict[str, Literal["previous_period", "same_period_last_year"]] = {
    "previous_period": "previous_period",
    "previous_year": "same_period_last_year",
}


class IntentOutput(Contract):
    """What the LLM returns (design §32 ``Intent``)."""

    route: Route
    objective: Objective
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[Filter] = Field(default_factory=list)
    start: date | None = None
    end: date | None = None
    grain: Literal["day", "week", "month", "quarter", "year"] | None = None
    time_dimension: str | None = None  # B1: which date the period is about, when several exist
    compare_to: Literal["previous_period", "previous_year"] | None = None  # B4


class ResolvedIntent(Contract):
    route: Route
    intent: AnalysisIntent | None = None
    refusal: Refusal | None = None
    clarification: str | None = None
    caveats: list[str] = Field(default_factory=list)


def catalog(model: SemanticModel) -> str:
    metrics = ", ".join(m.name for m in model.metrics) or "none"
    measures = ", ".join(m.name for m in model.measures) or "none"
    dims = ", ".join(f"{d.name} ({d.type})" for d in model.dimensions) or "none"
    return f"Metrics: {metrics}\nMeasures: {measures}\nDimensions: {dims}"


def _clarify(model: SemanticModel) -> str:
    names = ", ".join([m.name for m in model.metrics][:8]) or "none"
    return f"Please name the metric and the period you mean. Available metrics: {names}."


class IntentResolver:
    def __init__(
        self, gateway: LLMGateway, prompts: PromptRegistry, context: ContextBuilder
    ) -> None:
        self._gateway = gateway
        self._prompts = prompts
        self._context = context

    async def resolve(
        self, question: str, model: SemanticModel, today: date, machine: TurnStateMachine
    ) -> ResolvedIntent:
        part = ContextPart(
            kind="semantic",
            content=catalog(model),
            trust="untrusted",  # names come from the user's data
            provenance=[model.semantic_model_id],
        )
        ctx = self._context.build(
            self._prompts.get(PROMPT),
            {"question": question, "today": today.isoformat()},
            [part],
            CONTEXT_BUDGET,
        )
        machine.charge_tokens(ctx.tokens)
        out = await machine.bounded(self._gateway.structured(LLMTask.INTENT, IntentOutput, ctx))
        machine.charge_tokens(len(out.model_dump_json()) // 3)
        return post_check(out, question, model)


def post_check(out: IntentOutput, question: str, model: SemanticModel) -> ResolvedIntent:
    route = out.route
    if route == "unsupported":
        names = ", ".join([m.name for m in model.metrics][:8]) or "none"
        return ResolvedIntent(
            route=route,
            refusal=Refusal(
                code="UNSUPPORTED",
                message=f"This question is not about this dataset. Ask about: {names}.",
            ),
        )
    if route == "simulate" or out.objective == "simulate":
        return ResolvedIntent(
            route=route,
            refusal=Refusal(code="NOT_AVAILABLE", message="Scenario simulation is not available."),
        )
    if route == "clarify":
        return ResolvedIntent(route=route, clarification=_clarify(model))
    caveats = []
    if route in ("document", "mixed") or out.objective == "document_lookup":
        if not out.metrics:
            message = "Document search is not available yet; ask about the table's metrics."
            refusal = Refusal(code="NOT_AVAILABLE", message=message)
            return ResolvedIntent(route=route, refusal=refusal)
        caveats.append(NO_DOCUMENTS)
    return _resolve_names(out, question, model, caveats)


def _resolve_names(
    out: IntentOutput, question: str, model: SemanticModel, caveats: list[str]
) -> ResolvedIntent:
    resolver = SemanticResolver(model)
    try:
        metrics = list(dict.fromkeys(resolver.metric(m).name for m in out.metrics))
        dimensions = list(dict.fromkeys(resolver.dimension(d).name for d in out.dimensions))
        filters = [
            f.model_copy(update={"dimension": resolver.dimension(f.dimension).name})
            for f in out.filters
        ]
        time_dimension = resolver.dimension(out.time_dimension).name if out.time_dimension else None
    except SchemaError as exc:
        suggestions = [str(s) for s in (exc.details or {}).get("suggestions", [])]
        return ResolvedIntent(
            route="structured",
            refusal=Refusal(code="UNKNOWN_NAME", message=exc.message, suggestions=suggestions),
        )
    if not metrics:
        return ResolvedIntent(route="structured", clarification=_clarify(model))
    objective: Objective = out.objective
    if objective in ("document_lookup", "metric_value", "compare", "trend") and _WHY.search(
        question
    ):
        objective = "explain_change"
    elif objective == "document_lookup":
        objective = "metric_value"
    if objective == "explain_change":
        dimensions = [d for d in dimensions if _named(question, resolver.dimension(d))]
    times = {d.name for d in model.dimensions if d.type == "time"}
    start, end, grain = out.start, out.end, out.grain
    kept = []
    for f in filters:  # F1: a period put into filters becomes start/end
        lifted = lift(f) if f.dimension in times else None
        if lifted is None:
            kept.append(f)
            continue
        time_dimension = time_dimension or f.dimension
        if out.start is None and out.end is None:
            start, end = lifted[0] or start, lifted[1] or end
            grain = grain or lifted[2]
    for d in dimensions:  # a time dimension is never a grouping; it names the period's date
        if d in times:
            time_dimension = time_dimension or d
    compare_to = COMPARE_TO.get(out.compare_to) if out.compare_to else None
    try:
        time = TimeSpec(grain=grain or "month", start=start, end=end) if start or end else None
        intent = AnalysisIntent(
            objective=objective,
            metrics=metrics,
            dimensions=[d for d in dimensions if d not in times],
            filters=kept,
            time=time,
            source="user_question",
            time_dimension=time_dimension,
            compare_to=compare_to,
        )
    except ValidationError:
        return ResolvedIntent(route="structured", clarification=_clarify(model))
    return ResolvedIntent(route="structured", intent=intent, caveats=caveats)


def _named(question: str, dimension: SemanticDimension) -> bool:
    """The question names the dimension: its name or its column, as whole words."""
    asked = question.lower()
    names = {dimension.name, dimension.column.replace("_", " ")}
    return any(re.search(rf"\b{re.escape(n.lower())}\b", asked) for n in names)
