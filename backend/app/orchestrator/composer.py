"""ResponseComposer and ClaimExtractor (TASK-AGENT-005/006; design §32 and §42, CLAUDE.md §13).

Facts: every ok tool result with a numeric ``value`` and an evidence id becomes a fact ``nK``.
The LLM (COMPOSE, structured output) writes text/caveat segments and places ``{"type":
"number", "ref": "nK"}`` where a number goes; the composer turns each ref into a
``NumberSegment(evidence_id, "result.value", format)``. The format is derived from the evidence
unit in code, so the LLM chooses neither the value nor the unit. Accept check (design §32): every
ref must name a fact. Numbers the LLM types into text anyway are left for the validator's
backstop (NUMBER_WITHOUT_EVIDENCE etc.), which decides the answer's status.

ClaimExtractor: each sentence holding at least one number segment becomes a ``ClaimDraft``
(its rendered text + the evidence ids of its numbers); the recorder stores it via
``ClaimRepository`` with the answer's validation id.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field

from app.analytics.tools.analysis import ExplainChangeOutput
from app.analytics.tools.query import RankOutput
from app.llm.context import ContextBuilder, ContextPart
from app.llm.gateway import LLMGateway
from app.llm.ports import LLMTask
from app.llm.prompts import PromptRegistry
from app.ml.anomaly_tool import AnomalyOutput
from app.ml.forecast_tool import ForecastOutput
from app.orchestrator.domain import ClaimDraft, Contract, NumberView
from app.orchestrator.ml_answers import BUILDERS as ML_BUILDERS
from app.orchestrator.ml_answers import (
    MAX_ANOMALIES,
    MAX_POINTS,
    ml_segments,
    row_value,
    subject_label,
)
from app.orchestrator.state import TurnStateMachine
from app.orchestrator.tool_loop import ExecutedCall
from app.validator.domain import AnswerSegment, CaveatSegment, NumberSegment, TextSegment
from app.validator.text import direction_at, render

PROMPT = "compose.answer"
CONTEXT_BUDGET = 5000
FIELD = "result.value"
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)|\n")
Shown = TextSegment | NumberView | CaveatSegment


class ComposedSegment(Contract):
    type: Literal["text", "number", "caveat"]
    text: str | None = None
    ref: str | None = None


class ComposedAnswer(Contract):
    segments: Annotated[list[ComposedSegment], Field(min_length=1, max_length=40)]


@dataclass(frozen=True, slots=True)
class Fact:
    ref: str
    evidence_id: str
    value: float
    unit: str | None
    field: str = FIELD  # or "rows.<i>.value": a ranked row in the evidence's stored rows
    label: str | None = None  # the ranked member


MAX_RANK_FACTS = 10


def facts_from(calls: list[ExecutedCall]) -> list[Fact]:
    """``result.value`` of every numeric result, plus each top ranked row of a ranking (B5)."""
    facts: list[Fact] = []
    for c in calls:
        out, ev = c.result.output, c.result.evidence_id
        value = out.get("value") if out else None
        if out is None or ev is None or isinstance(value, bool):
            continue
        raw_unit = out.get("unit")
        unit = raw_unit if isinstance(raw_unit, str) else None
        if isinstance(value, int | float):
            facts.append(Fact(f"n{len(facts) + 1}", ev, float(value), unit))
        elif c.name == "analytics.rank" and c.result.status == "ok":
            items = RankOutput.model_validate(out).ranking.items[:MAX_RANK_FACTS]
            for i, item in enumerate(items):  # preview rows follow the ranking's item order
                ref = f"n{len(facts) + 1}"
                facts.append(Fact(ref, ev, item.value, unit, f"rows.{i}.value", item.key))
        elif c.result.status == "ok":
            facts += _ml_facts(c, ev, len(facts))
    return facts


def _ml_facts(call: ExecutedCall, evidence_id: str, before: int) -> list[Fact]:
    """B-2: forecast points and flagged anomalies as citable ``rows.<i>.<column>`` facts, in the
    order of the evidence's stored preview rows (as ``ml_answers`` cites them)."""
    out = call.result.output
    labelled: list[tuple[str, str]] = []
    if call.name == "ml.forecast" and out is not None:
        points = ForecastOutput.model_validate(out).forecast.points[:MAX_POINTS]
        labelled = [(f"forecast estimate {p.period}", "value") for p in points]
    elif call.name == "ml.anomalies" and out is not None:
        found = AnomalyOutput.model_validate(out).result.anomalies[:MAX_ANOMALIES]
        labelled = [(f"anomaly at {subject_label(a.subject)}", "observed") for a in found]
    facts: list[Fact] = []
    for i, (label, column) in enumerate(labelled):
        field = f"rows.{i}.{column}"
        value, unit = row_value(call, field)
        if value is not None:
            facts.append(
                Fact(f"n{before + len(facts) + 1}", evidence_id, value, unit, field, label)
            )
    return facts


def display_format(unit: str | None) -> str:
    """Validator display format for an evidence unit (app.validator.numbers.parse_format)."""
    if unit in ("pct", "pp"):
        return f"{unit}:1"
    if unit == "count":
        return "count"
    if unit is not None and re.fullmatch(r"currency:[A-Z]{3}", unit):
        return f"{unit}:2"
    return "number:2"


def format_value(value: float, unit: str | None) -> str:
    if unit == "pct":  # TRD 25.1: pct and pp are fractions, shown x100 (validator.numbers)
        return f"{value * 100:,.1f}%"
    if unit == "pp":
        return f"{value * 100:,.1f} pp"
    if unit == "count":
        return f"{value:,.0f}"
    if unit is not None and unit.startswith("currency:"):
        return f"{unit.removeprefix('currency:')} {value:,.2f}"
    return f"{value:,.2f}"


def _number(fact: Fact) -> NumberSegment:
    fmt = display_format(fact.unit)
    return NumberSegment(evidence_id=fact.evidence_id, field=fact.field, format=fmt)


def _fact_lines(calls: list[ExecutedCall], facts: list[Fact]) -> str:
    by_evidence: dict[str, list[Fact]] = {}
    for f in facts:
        by_evidence.setdefault(f.evidence_id, []).append(f)
    lines = []
    for c in calls:
        r = c.result
        keys = ("metric", "dimension", "filters", "start", "end", "current_start", "current_end")
        head = f"{r.tool} { ({k: c.args[k] for k in keys if k in c.args}) }"
        found = by_evidence.get(r.evidence_id or "", [])
        if found:
            for fact in found:
                what = "value"
                if fact.label:
                    rank = c.name == "analytics.rank"
                    what = f"rank member {fact.label}: value" if rank else f"{fact.label}: value"
                lines.append(f"{fact.ref}: {head} -> {what} {fact.value} ({fact.unit or 'plain'})")
        elif r.status != "ok":
            code = r.error.code if r.error else "UNAVAILABLE"
            lines.append(f"{head} -> not available ({code})")
        else:
            lines.append(f"{head} -> {_non_numeric(c)}")
    return "\n".join(lines)


def _non_numeric(call: ExecutedCall) -> str:
    """Rankings by member name only: ranked values have no evidence field of their own."""
    if call.name == "analytics.rank" and call.result.output is not None:
        items = RankOutput.model_validate(call.result.output).ranking.items
        return "ranked members (first = highest): " + ", ".join(i.key for i in items[:10])
    return "result without a single value"


def _rca_factors(calls: list[ExecutedCall]) -> str:
    lines = []
    for c in calls:
        if c.name != "analytics.explain_change" or c.result.output is None:
            continue
        for f in ExplainChangeOutput.model_validate(c.result.output).result.findings:
            sign = "down" if f.magnitude < 0 else "up"
            lines.append(f"{f.factor}: {f.relation_type}, moved {sign}")
    return "Ranked contributing factors:\n" + "\n".join(lines) if lines else ""


class ResponseComposer:
    def __init__(
        self, gateway: LLMGateway, prompts: PromptRegistry, context: ContextBuilder
    ) -> None:
        self._gateway = gateway
        self._prompts = prompts
        self._context = context

    async def compose(
        self,
        question: str,
        calls: list[ExecutedCall],
        feedback: list[str],
        machine: TurnStateMachine,
    ) -> list[AnswerSegment]:
        facts = facts_from(calls)
        refs = {f.ref: f for f in facts}
        content = "\n".join(p for p in (_fact_lines(calls, facts), _rca_factors(calls)) if p)
        part = ContextPart(kind="tool_results", content=content or "no results", trust="untrusted")
        variables = {"question": question, "feedback": ", ".join(feedback) or "none"}
        ctx = self._context.build(self._prompts.get(PROMPT), variables, [part], CONTEXT_BUDGET)

        def check(answer: ComposedAnswer) -> list[str]:
            problems = []
            for i, s in enumerate(answer.segments):
                if s.type == "number" and s.ref not in refs:
                    problems.append(f"segments.{i}.ref: must be one of {sorted(refs) or 'none'}")
                if s.type != "number" and not s.text:
                    problems.append(f"segments.{i}.text: required for {s.type} segments")
            return problems

        machine.charge_tokens(ctx.tokens)
        answer = await machine.bounded(
            self._gateway.structured(LLMTask.COMPOSE, ComposedAnswer, ctx, check=check)
        )
        machine.charge_tokens(len(answer.model_dump_json()) // 3)
        return [_segment(s, refs) for s in answer.segments]


def _segment(s: ComposedSegment, refs: dict[str, Fact]) -> AnswerSegment:
    if s.type == "number":
        return _number(refs[s.ref or ""])
    if s.type == "caveat":
        return CaveatSegment(text=s.text or "")
    return TextSegment(text=s.text or "")


def verified_facts(calls: list[ExecutedCall]) -> list[AnswerSegment]:
    """Degraded answer without the LLM: each verified number, labelled by its metric name; ML
    results as their code-built blocks with caveats (B-2)."""
    labels = {c.result.evidence_id: str(c.args.get("metric", c.name)) for c in calls}
    segments: list[AnswerSegment] = []
    for fact in facts_from([c for c in calls if c.name not in ML_BUILDERS]):
        name = labels.get(fact.evidence_id)
        label = TextSegment(text=f"{name} ({fact.label}): " if fact.label else f"{name}: ")
        segments += [label, _number(fact), TextSegment(text=".\n")]
    return segments + ml_segments(calls)


def number_values(
    calls: list[ExecutedCall],
) -> Callable[[NumberSegment], tuple[float | None, str | None]]:
    """(value, unit) behind a number segment: ``result.value`` facts, or a ``rows.<i>.<col>``
    cell of the call's result preview (ML answers, app.orchestrator.ml_answers)."""
    facts = {f.evidence_id: f for f in facts_from(calls)}
    by_evidence = {c.result.evidence_id: c for c in calls if c.result.evidence_id}

    def lookup(s: NumberSegment) -> tuple[float | None, str | None]:
        call = by_evidence.get(s.evidence_id)
        if s.field.startswith("rows.") and call is not None:
            return row_value(call, s.field)
        fact = facts.get(s.evidence_id)
        return (fact.value, fact.unit) if fact else (None, None)

    return lookup


def views(segments: list[AnswerSegment], calls: list[ExecutedCall]) -> list[Shown]:
    """Segments as streamed: number segments carry the evidence-bound value and unit."""
    lookup = number_values(calls)
    directed = _directed(segments)
    shown: list[Shown] = []
    for i, s in enumerate(segments):
        if isinstance(s, NumberSegment):
            value, unit = lookup(s)
            view = NumberView(
                evidence_id=s.evidence_id,
                field=s.field,
                format=s.format,
                value=value,
                unit=unit,
                absolute=i in directed,
            )
            shown.append(view)
        else:
            shown.append(s)
    return shown


def _directed(segments: list[AnswerSegment]) -> set[int]:
    """B-5: indexes of number segments governed by a direction word ("fell by", "a ...
    decrease"), whose sign the wording already states; the validator's own rule."""
    rendered = render(segments)
    return {
        i
        for i, s in enumerate(segments)
        if isinstance(s, NumberSegment)
        and direction_at(rendered.text, rendered.offsets[i], rendered.offsets[i] + 1) is not None
    }


def extract_claims(segments: list[AnswerSegment], calls: list[ExecutedCall]) -> list[ClaimDraft]:
    """ClaimExtractor: every sentence with >= 1 number segment -> one claim."""
    lookup = number_values(calls)
    claims: list[ClaimDraft] = []
    text, evidence = "", list[str]()

    def flush() -> None:
        nonlocal text, evidence
        if evidence and text.strip():
            ids = list(dict.fromkeys(evidence))
            claims.append(ClaimDraft(text=text.strip(), evidence_ids=ids))
        text, evidence = "", []

    directed = _directed(segments)
    for i, s in enumerate(segments):
        if isinstance(s, NumberSegment):
            value, unit = lookup(s)
            if value is not None and i in directed:
                value = abs(value)
            text += format_value(value, unit) if value is not None else "[number]"
            evidence.append(s.evidence_id)
        elif isinstance(s, CaveatSegment):
            flush()
        else:
            rest = s.text
            while (m := _SENTENCE_END.search(rest)) is not None:
                text += rest[: m.end()]
                rest = rest[m.end() :]
                flush()
            text += rest
    flush()
    return claims
