"""Deterministic answers for wording-only failures (RCA-FIX; CLAUDE.md §13-14).

When the composed answer of an explain_change, forecast or anomaly turn is still rejected after
the one recompose and every validation issue is a wording issue (``WORDING``), the orchestrator
replaces it with an answer written by code from the turn's tool results, then validates that
answer again like any other; it is shown only if it passes on its own merits. Numeric, evidence
or schema issues never fall back: they mean the facts, not the wording, are in doubt.

- explain_change: "<metric> (<filters>) fell by X (Y) from the period a to b to the period c to
  d." then the top factors with their contribution (``rows.<i>.magnitude``) and share of the
  change (``rows.<i>.share_pct``, a fraction) from the evidence's stored rows, and the caveat
  that contributions are not causes. X is ``result.value`` of the explain_change evidence, Y
  ``result.value`` of the growth evidence.
- forecast / anomalies: the code-built ML blocks of ``ml_answers`` alone.
Text holds no digits except ISO dates (masked by the validator); member names come from the
data, so a name with digits makes the template fail validation, and the answer stays held.
"""

from pydantic import JsonValue

from app.analytics.tools.analysis import ExplainChangeOutput
from app.orchestrator.composer import display_format
from app.orchestrator.ml_answers import ml_segments
from app.orchestrator.tool_loop import ExecutedCall
from app.validator.domain import (
    AnswerSegment,
    CaveatSegment,
    NumberSegment,
    TextSegment,
    ValidationResult,
)

WORDING = frozenset({"CAUSAL_LANGUAGE", "GUARANTEE_LANGUAGE"})
MAX_FACTORS = 3
PLAIN = "number:2"  # preview columns other than ``value`` carry no unit
NOT_CAUSES = "These are contributions, not proven causes."
SHARES = "A share is the fraction of the total change a factor carries (one = all of it)."


def wording_only(result: ValidationResult) -> bool:
    return bool(result.issues) and all(i.code in WORDING for i in result.issues)


def template_answer(objective: str, calls: list[ExecutedCall]) -> list[AnswerSegment]:
    """The code-written answer for ``objective``; empty when there is none."""
    if objective == "explain_change":
        return _explain_change(calls)
    if objective in ("forecast", "anomalies"):
        return ml_segments(calls)
    return []


def _value(calls: list[ExecutedCall], name: str) -> tuple[ExecutedCall, str, float] | None:
    """The first ok call of ``name`` with evidence and a numeric ``value``."""
    for c in calls:
        out, ev = c.result.output, c.result.evidence_id
        value = out.get("value") if out else None
        if c.name == name and c.result.status == "ok" and ev and isinstance(value, int | float):
            return c, ev, float(value)
    return None


def _scope(filters: JsonValue) -> str:
    parts = []
    for f in filters if isinstance(filters, list) else []:
        if isinstance(f, dict):
            shown = f.get("value") if f.get("op") == "eq" else "filtered"
            parts.append(f"{f.get('dimension')} {shown}")
    return f" ({', '.join(parts)})" if parts else ""


def _unit(call: ExecutedCall) -> str | None:
    unit = call.result.output.get("unit") if call.result.output else None
    return unit if isinstance(unit, str) else None


def _explain_change(calls: list[ExecutedCall]) -> list[AnswerSegment]:
    found = _value(calls, "analytics.explain_change")
    if found is None:
        return []
    rca, ev, delta = found
    result = ExplainChangeOutput.model_validate(rca.result.output).result
    verb = "fell by " if delta < 0 else "rose by " if delta > 0 else "changed by "
    head = f"{rca.args.get('metric')}{_scope(rca.args.get('filters'))} {verb}"
    segments: list[AnswerSegment] = [
        TextSegment(text=head),
        NumberSegment(evidence_id=ev, field="result.value", format=display_format(_unit(rca))),
    ]
    growth = _value(calls, "analytics.growth")
    if growth is not None:
        call, growth_ev, _ = growth
        fmt = display_format(_unit(call))
        segments += [
            TextSegment(text=" ("),
            NumberSegment(evidence_id=growth_ev, field="result.value", format=fmt),
            TextSegment(text=")"),
        ]
    (a, b), (c, d) = result.compare_range, result.current_range
    segments.append(TextSegment(text=f" from the period {a} to {b} to the period {c} to {d}.\n"))
    findings = result.findings[:MAX_FACTORS]
    if not findings:
        segments.append(TextSegment(text="No contributing factor was ranked for this change."))
        return [*segments, CaveatSegment(text=NOT_CAUSES)]
    segments.append(TextSegment(text="The largest contributions to the change: "))
    for i, f in enumerate(findings):  # preview rows follow the findings' order
        segments += [
            TextSegment(text=f"{', ' if i else ''}{f.factor} ("),
            NumberSegment(evidence_id=ev, field=f"rows.{i}.magnitude", format=PLAIN),
        ]
        if f.share_pct is not None:
            segments += [
                TextSegment(text="; share "),
                NumberSegment(evidence_id=ev, field=f"rows.{i}.share_pct", format=PLAIN),
            ]
        segments.append(TextSegment(text=")"))
    segments.append(TextSegment(text="."))
    return [*segments, CaveatSegment(text=NOT_CAUSES), CaveatSegment(text=SHARES)]
