"""Validation contracts (TASK-VAL-001; design 40-41, TRD 26, ADR-0021).

Four states only: VALID, INVALID, UNCERTAIN, REQUIRES_REVIEW. ``ValidationResult`` enforces the
design 41 decision table: VALID means no issues; any hard issue means INVALID or REQUIRES_REVIEW
(REQUIRES_REVIEW also covers repeated INVALID after the repair budget, TRD 26); UNCERTAIN means
soft issues only.

``AnswerSegment`` covers the text/number/caveat segments that validators read (TRD 8.5/27).
Table and chart segments are left out until the ResponseComposer (TASK-AGENT-005) needs them.
"""

from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.data.domain import Ratio
from app.evidence.domain import Evidence, EvidenceId


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ValidationStatus(StrEnum):
    VALID = "VALID"
    INVALID = "INVALID"
    UNCERTAIN = "UNCERTAIN"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"


class ValidationIssue(Contract):
    code: str  # NUMBER_MISMATCH, MISSING_EVIDENCE, UNKNOWN_COLUMN, CAUSAL_LANGUAGE, ...
    severity: Literal["hard", "soft"]
    target: str  # segment / claim id
    detail: str


class TextSegment(Contract):
    type: Literal["text"] = "text"
    text: str


class NumberSegment(Contract):
    type: Literal["number"] = "number"
    evidence_id: EvidenceId
    field: str  # e.g. "result.value"
    format: str  # e.g. "pct:1"


class CaveatSegment(Contract):
    type: Literal["caveat"] = "caveat"
    text: str


AnswerSegment = Annotated[TextSegment | NumberSegment | CaveatSegment, Field(discriminator="type")]
SubjectType = Literal["answer", "widget", "tool_result", "insight"]


class ValidationRequest(Contract):
    schema_version: Literal["1.0"] = "1.0"
    subject_type: SubjectType
    subject_id: str
    segments: list[AnswerSegment] | None = None
    evidence_ids: list[EvidenceId]
    semantic_model_version: Annotated[int, Field(ge=1)] | None = None
    impact: Literal["normal", "high"] = "normal"


class ConfidenceSummary(Contract):
    level: Literal["low", "medium", "high"]
    score: Ratio
    basis: list[str]


class ValidationResult(Contract):
    schema_version: Literal["1.0"] = "1.0"
    id: Annotated[str, Field(pattern=r"^va_\w+$")]
    subject_type: SubjectType
    subject_id: str
    status: ValidationStatus
    issues: list[ValidationIssue]
    confidence: ConfidenceSummary
    validator_versions: dict[str, str]

    @model_validator(mode="after")
    def _status_matches_issues(self) -> Self:
        hard = any(i.severity == "hard" for i in self.issues)
        s = ValidationStatus
        if self.status is s.VALID and self.issues:
            raise ValueError("VALID results carry no issues")
        if hard and self.status not in {s.INVALID, s.REQUIRES_REVIEW}:
            raise ValueError("a hard issue requires INVALID or REQUIRES_REVIEW")
        if self.status is s.INVALID and not hard:
            raise ValueError("INVALID requires at least one hard issue")
        if self.status is s.UNCERTAIN and not self.issues:
            raise ValueError("UNCERTAIN requires at least one soft issue")
        return self


EvidenceBundle = Mapping[str, Evidence]  # evidence_id -> Evidence


def cited_ids(req: ValidationRequest) -> list[str]:
    """Declared evidence ids plus ids referenced by number segments, first occurrence order."""
    numbered = [s.evidence_id for s in req.segments or [] if isinstance(s, NumberSegment)]
    return list(dict.fromkeys([*req.evidence_ids, *numbered]))


class Validator(Protocol):
    name: str
    version: str

    async def check(self, req: ValidationRequest, ev: EvidenceBundle) -> list[ValidationIssue]: ...
