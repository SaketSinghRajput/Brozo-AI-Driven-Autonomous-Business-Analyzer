"""LogicValidator lexicon rules (TASK-VAL-005; TRD 26 logic check, TRD 28 wording policy).

- CAUSAL_LANGUAGE (hard): "caused", "due to", "led to", ... in answer text unless a cited
  evidence record comes from a causal method (operation namespace ``causal.``; none exists
  before P3). Caveat segments and "due to missing/insufficient data" statements are exempt,
  because they explain a limitation of the analysis, not a business cause.
- GUARANTEE_LANGUAGE (hard): "guaranteed", "will definitely", "risk-free", ... anywhere.
- ANOMALY_AS_PROBLEM (soft): a sentence calls an anomaly/outlier a problem, fraud or error
  without hedging ("may", "could", "investigate"). An anomaly is a statistical finding, not a
  diagnosis.
- CONTRADICTION (hard): the same evidence number is described as both rising and falling.
- CONTRADICTION_AVAILABILITY (hard, B-3): a text sentence says a result is unavailable,
  insufficient or cannot be computed, while the answer shows a number for the same subject.
  The subject is the analysis kind the sentence names (forecast, anomaly, segment, contribution)
  matched to the cited operation, else the metric argument of the cited evidence named in the
  sentence. Caveat segments are exempt ("forecasting needs 12 periods" next to a revenue total
  is a limitation of another analysis, not a contradiction).
Negated wording ("not caused by", "no guarantee", "anomaly ≠ business problem"; a negation within
the six preceding words)
does not trigger a rule. Contradictions with the dashboard need the dashboard in the request
and are not checked in v1.
"""

import re
from typing import Literal

from app.evidence.domain import Evidence
from app.validator.domain import (
    AnswerSegment,
    EvidenceBundle,
    NumberSegment,
    TextSegment,
    ValidationIssue,
    ValidationRequest,
    cited_ids,
)
from app.validator.text import Direction, direction_at, render, sentences

CAUSAL_PREFIX = "causal."
_CAUSAL = re.compile(
    r"\b(?:caused|causes|causing|cause of|because of|due to|led to|leads to|lead to|resulted in"
    r"|results in|as a result of|drove|driven by|attributable to|responsible for)\b",
    re.IGNORECASE,
)
_LIMITATION = re.compile(
    r"\s+(?:the\s+)?(?:missing|insufficient|limited|incomplete|unavailable|lack of)\b", re.I
)
_GUARANTEE = re.compile(
    r"\b(?:guarantee[sd]?|guaranteeing|will definitely|definitely will|will certainly"
    r"|certainly will|(?:is|are) certain to|will surely|surely will|assured|risk-free|no risk"
    r"|without (?:any )?doubt|cannot fail|can't fail)\b",
    re.IGNORECASE,
)
_NEGATION = re.compile(r"\b(?:not|no|never|cannot|without)\b|n't\b|≠", re.IGNORECASE)
_ANOMALY = re.compile(r"\b(?:anomal\w*|outliers?|spikes?)\b", re.IGNORECASE)
_PROBLEM = re.compile(
    r"\b(?:problems?|issues?|fraud\w*|errors?|failures?|broken|crisis|wrong|theft|bugs?)\b",
    re.IGNORECASE,
)
_HEDGE = re.compile(
    r"\b(?:may|might|could|possibl[ey]|potential(?:ly)?|investigat\w*|check|whether|verify)\b",
    re.IGNORECASE,
)


_UNAVAILABLE = re.compile(
    r"\b(?:unavailable|not available|insufficient|not possible|impossible|unable to"
    r"|(?:cannot|can't|could not|couldn't) be (?:computed|calculated|determined|estimated"
    r"|forecast|produced|provided|answered|shown)"
    r"|no\s+(?:\w+\s+){0,3}(?:is|are|was|were)\s+available)\b",
    re.IGNORECASE,
)
# analysis kind -> wording that names it; matched against the cited evidence operation
_KINDS = {
    "ml.forecast": re.compile(r"\b(?:forecast\w*|predict\w*|projection\w*)", re.I),
    "ml.anomalies": re.compile(r"\b(?:anomal\w*|unusual|outliers?)", re.I),
    "ml.segments": re.compile(r"\b(?:segment\w*|cluster\w*)", re.I),
    "analytics.explain_change": re.compile(r"\b(?:contribut\w*|factors?|drivers?)", re.I),
}


def _negated(text: str, start: int) -> bool:
    """A negation among the six words before ``start``."""
    window = " ".join(text[:start].split()[-6:])
    return bool(_NEGATION.search(window))


def _causal(text: str, m: re.Match[str]) -> bool:
    if _negated(text, m.start()):
        return False
    return not (m.group().lower() == "due to" and _LIMITATION.match(text, m.end()))


class LogicValidator:
    name = "logic"
    # 1.0.1: negated anomaly wording ("≠ business problem") is not flagged
    # 1.1.0: CONTRADICTION_AVAILABILITY
    version = "1.1.0"

    async def check(self, req: ValidationRequest, ev: EvidenceBundle) -> list[ValidationIssue]:
        segments = req.segments or []
        cited = [ev[i] for i in cited_ids(req) if i in ev]
        causal_ok = any(e.operation.startswith(CAUSAL_PREFIX) for e in cited)
        issues: list[ValidationIssue] = []
        for idx, seg in enumerate(segments):
            if isinstance(seg, NumberSegment):
                continue
            target = f"segments[{idx}]"
            if isinstance(seg, TextSegment) and not causal_ok:
                issues += [
                    _issue("CAUSAL_LANGUAGE", "hard", target, f"causal wording {m.group()!r}")
                    for m in _CAUSAL.finditer(seg.text)
                    if _causal(seg.text, m)
                ]
            issues += [
                _issue("GUARANTEE_LANGUAGE", "hard", target, f"guarantee wording {m.group()!r}")
                for m in _GUARANTEE.finditer(seg.text)
                if not _negated(seg.text, m.start())
            ]
        rendered = render(segments)
        for sentence in sentences(rendered.text):
            problem = _PROBLEM.search(sentence)
            flagged = _ANOMALY.search(sentence) and problem
            if flagged and problem and not _HEDGE.search(sentence):
                if _negated(sentence, problem.start()):  # "anomaly ≠ business problem"
                    continue
                detail = "an anomaly is presented as a confirmed business problem"
                issues.append(_issue("ANOMALY_AS_PROBLEM", "soft", "answer", detail))
        issues += _availability(segments, ev)
        return issues + _contradictions(req, rendered.text, rendered.offsets)


def _availability(segments: list[AnswerSegment], ev: EvidenceBundle) -> list[ValidationIssue]:
    """A "not available" sentence about a subject the answer then shows a number for."""
    shown = [
        ev[s.evidence_id]
        for s in segments
        if isinstance(s, NumberSegment) and s.evidence_id in ev  # missing: EvidenceValidator
    ]
    detail = "says a result is unavailable but shows a number for it"
    return [
        _issue("CONTRADICTION_AVAILABILITY", "hard", f"segments[{idx}]", detail)
        for idx, seg in enumerate(segments)
        if isinstance(seg, TextSegment)
        and any(
            _UNAVAILABLE.search(sentence) and any(_about(e, sentence) for e in shown)
            for sentence in sentences(seg.text)
        )
    ]


def _about(evidence: Evidence, sentence: str) -> bool:
    """The sentence names this evidence's analysis kind, or (naming none) its metric."""
    kinds = {op for op, words in _KINDS.items() if words.search(sentence)}
    if kinds:
        return evidence.operation in kinds
    metric = str(evidence.arguments.get("metric") or "").lower()
    return bool(metric) and metric in sentence.lower()


def _contradictions(
    req: ValidationRequest, text: str, offsets: tuple[int, ...]
) -> list[ValidationIssue]:
    seen: dict[str, set[Direction]] = {}
    for idx, seg in enumerate(req.segments or []):
        if isinstance(seg, NumberSegment):
            direction = direction_at(text, offsets[idx], offsets[idx] + 1)
            if direction is not None:
                seen.setdefault(seg.evidence_id, set()).add(direction)
    return [
        _issue("CONTRADICTION", "hard", evidence_id, "described as both rising and falling")
        for evidence_id, directions in seen.items()
        if len(directions) > 1
    ]


def _issue(
    code: str, severity: Literal["hard", "soft"], target: str, detail: str
) -> ValidationIssue:
    return ValidationIssue(code=code, severity=severity, target=target, detail=detail)
