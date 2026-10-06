"""ConfidenceCalculator (TASK-VAL-006; TRD 25.1: deterministic, never produced by the LLM).

Formula (an ordinal indicator, not a calibrated probability; calibration is TASK-VAL-012):

    any hard issue            -> score 0.0
    numbers   = number segments + quantities written in text/caveat segments
    flagged   = distinct targets of number issues (numerical.NUMBER_CODES)
    coverage  = (numbers - flagged) / numbers;  with no numbers: 1.0 if evidence is cited,
                else 0.5 (prose that nothing grounds)
    score     = coverage
                x 0.85 ** soft_issues
                x 0.9   if any cited evidence carries a model reference (an ML estimate)
                x min(score of cited evidence that already carries a confidence)
    level     = high if score >= 0.8, medium if score >= 0.5, else low

The score is rounded to 4 decimals; ``basis`` lists every factor that was applied.
"""

from typing import Literal

from app.validator.domain import (
    ConfidenceSummary,
    EvidenceBundle,
    NumberSegment,
    ValidationIssue,
    ValidationRequest,
    cited_ids,
)
from app.validator.numbers import extract_numbers
from app.validator.numerical import NUMBER_CODES

SOFT_PENALTY = 0.85
MODEL_PENALTY = 0.9
HIGH, MEDIUM = 0.8, 0.5


Level = Literal["high", "medium", "low"]


def confidence_level(score: float) -> Level:
    """The documented bands: high >= 0.8, medium >= 0.5, else low (also used for domain
    classification confidence in the understanding API)."""
    return "high" if score >= HIGH else "medium" if score >= MEDIUM else "low"


def _count_numbers(req: ValidationRequest) -> int:
    return sum(
        1 if isinstance(s, NumberSegment) else len(extract_numbers(s.text))
        for s in req.segments or []
    )


class ConfidenceCalculator:
    version = "1.0.0"

    def compute(
        self, req: ValidationRequest, ev: EvidenceBundle, issues: list[ValidationIssue]
    ) -> ConfidenceSummary:
        if any(i.severity == "hard" for i in issues):
            return ConfidenceSummary(level="low", score=0.0, basis=["hard_issues"])
        cited = [ev[i] for i in cited_ids(req) if i in ev]
        numbers = _count_numbers(req)
        basis: list[str] = []
        if numbers:
            flagged = len({i.target for i in issues if i.code in NUMBER_CODES})
            score = (numbers - flagged) / numbers
            basis.append(f"numbers_verified:{numbers - flagged}/{numbers}")
        else:
            score = 1.0 if cited else 0.5
            basis.append("no_numbers" if cited else "ungrounded")
        soft = sum(1 for i in issues if i.severity == "soft")
        if soft:
            score *= SOFT_PENALTY**soft
            basis.append(f"soft_issues:{soft}")
        if any(e.model is not None for e in cited):
            score *= MODEL_PENALTY
            basis.append("model_estimate")
        prior = [e.confidence.score for e in cited if e.confidence is not None]
        if prior:
            score *= min(prior)
            basis.append(f"evidence_confidence_min:{min(prior):.2f}")
        score = round(score, 4)
        return ConfidenceSummary(level=confidence_level(score), score=score, basis=basis)
