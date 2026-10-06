"""Grounding eval (TASK-EVAL-004), computed from what a turn stored — never from an LLM judge.

Inputs per turn: the ``TurnRecord`` (stored segments, including held ones; claims; trace), the
turn's ``ValidationResult`` list and the evidence store of the in-memory executor, plus the golden
item. Method per ADR-0025: deterministic evidence/label matching.

Per turn:
- bound numbers: number segments; each must name evidence that exists in this turn's store;
- unbound numbers: quantities written into text/caveat segments (validator ``extract_numbers``,
  which skips years and dates) — these are numbers not bound to evidence;
- claims: every stored claim must cite >= 1 existing evidence id;
- correct: every bound number's evidence value equals a golden expected number (1e-6 rel).
"""

from collections import Counter
from dataclasses import dataclass
from typing import Any

from app.orchestrator.domain import NumberView, TurnRecord, TurnState
from app.validator.domain import CaveatSegment, NumberSegment, TextSegment, ValidationResult
from app.validator.numbers import extract_numbers

SHOWN = frozenset({TurnState.COMPLETED})


def close(a: float, b: float) -> bool:
    return abs(a - b) <= max(1e-6 * max(abs(a), abs(b)), 5e-6)


@dataclass(frozen=True)
class Grounding:
    bound: int
    unbound: int
    dangling: int  # number segments whose evidence id is not in the store
    claims: int
    claims_without_evidence: int
    numbers_correct: int  # bound numbers whose evidence value matches a golden number
    status: str | None
    shown: bool

    @property
    def grounded(self) -> bool:
        """Shown answer whose every number is bound to existing, correct evidence."""
        return (
            self.shown
            and self.unbound == 0
            and self.dangling == 0
            and self.claims_without_evidence == 0
            and self.numbers_correct == self.bound
        )


def turn_grounding(
    record: TurnRecord,
    results: list[ValidationResult],
    evidence: dict[str, Any],
    expected_numbers: list[float],
) -> Grounding:
    bound = unbound = dangling = correct = 0
    for s in record.segments:
        # Stored answers hold number views (NumberSegment + value/unit) since GAPS-3a.
        if isinstance(s, NumberSegment | NumberView):
            bound += 1
            ev = evidence.get(s.evidence_id)
            if ev is None:
                dangling += 1
                continue
            value = ev.result.value
            if value is not None and any(close(value, e) for e in expected_numbers):
                correct += 1
        elif isinstance(s, TextSegment | CaveatSegment):
            unbound += len(extract_numbers(s.text))
    missing = sum(1 for c in record.claims if not all(i in evidence for i in c.evidence_ids))
    final = next((r for r in reversed(results) if r.id == record.validation_id), None)
    return Grounding(
        bound=bound,
        unbound=unbound,
        dangling=dangling,
        claims=len(record.claims),
        claims_without_evidence=missing,
        numbers_correct=correct,
        status=final.status.value if final else None,
        shown=record.state in SHOWN,
    )


def _rate(hits: int, total: int) -> float | None:
    return round(hits / total, 4) if total else None


def summarize(rows: list[tuple[dict[str, Any], Grounding, bool]]) -> dict[str, Any]:
    """rows: (golden item, grounding, outcome_correct)."""
    grounds = [g for _, g, _ in rows]
    unbound = sum(g.unbound for g in grounds)
    numbers = unbound + sum(g.bound for g in grounds)
    shown = [g for g in grounds if g.shown]
    refusals = [ok for i, _, ok in rows if i["expected"]["outcome"] != "answered"]
    answerable = [g for i, g, _ in rows if i["expected"]["outcome"] == "answered"]
    return {
        "turns": len(rows),
        "numbers_total": numbers,
        "numbers_bound": numbers - unbound,
        "numbers_unbound": unbound,
        "unsupported_claim_rate": _rate(unbound, numbers),
        "dangling_evidence_refs": sum(g.dangling for g in grounds),
        "claims": sum(g.claims for g in grounds),
        "claims_without_evidence": sum(g.claims_without_evidence for g in grounds),
        "validation_status": dict(sorted(Counter(g.status or "none" for g in grounds).items())),
        "shown_answers": len(shown),
        "groundedness": _rate(sum(g.grounded for g in shown), len(shown)),
        "answerable_grounded_rate": _rate(sum(g.grounded for g in answerable), len(answerable)),
        "correct_refusal_rate": _rate(sum(refusals), len(refusals)),
    }
