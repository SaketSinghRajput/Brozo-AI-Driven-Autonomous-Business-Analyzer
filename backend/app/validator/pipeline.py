"""ValidationPipeline (TASK-VAL-007; design 41, ADR-0021).

Order: Schema -> Numerical -> Evidence -> Logic (design 41). Short-circuit: a hard schema issue
skips the numerical check, whose field and unit lookups assume the schema is sound; the skip is
recorded in ``validator_versions``. Evidence and logic checks always run.

Status decision (design 41 table, TRD 26):
    any hard issue            -> INVALID, or REQUIRES_REVIEW once the repair budget is spent
    high impact and any soft  -> REQUIRES_REVIEW
    any soft                  -> UNCERTAIN
    otherwise                 -> VALID
So an unverified high-impact claim can never come out VALID.
"""

from collections.abc import Mapping, Sequence

from app.core.ids import IdGenerator
from app.data.semantic.domain import SemanticModel
from app.validator.confidence import ConfidenceCalculator
from app.validator.domain import (
    EvidenceBundle,
    ValidationIssue,
    ValidationRequest,
    ValidationResult,
    ValidationStatus,
    Validator,
)
from app.validator.evidence import EvidenceValidator
from app.validator.logic import LogicValidator
from app.validator.numerical import NumericalValidator, Previews
from app.validator.schema import SchemaValidator

PIPELINE_VERSION = "1.0.0"
SKIPPED = "skipped"
# validator name -> validator whose hard issues make it pointless to run
SHORT_CIRCUIT = {"numerical": "schema"}


def decide(
    issues: Sequence[ValidationIssue], impact: str, *, repair_exhausted: bool = False
) -> ValidationStatus:
    if any(i.severity == "hard" for i in issues):
        return ValidationStatus.REQUIRES_REVIEW if repair_exhausted else ValidationStatus.INVALID
    if issues:
        return ValidationStatus.REQUIRES_REVIEW if impact == "high" else ValidationStatus.UNCERTAIN
    return ValidationStatus.VALID


class ValidationPipeline:
    def __init__(
        self,
        validators: Sequence[Validator],
        ids: IdGenerator,
        confidence: ConfidenceCalculator | None = None,
    ) -> None:
        self._validators = validators
        self._ids = ids
        self._confidence = confidence or ConfidenceCalculator()

    async def run(
        self, req: ValidationRequest, ev: EvidenceBundle, *, repair_exhausted: bool = False
    ) -> ValidationResult:
        issues: list[ValidationIssue] = []
        versions = {"pipeline": PIPELINE_VERSION, "confidence": self._confidence.version}
        hard_by: set[str] = set()
        for validator in self._validators:
            if SHORT_CIRCUIT.get(validator.name) in hard_by:
                versions[validator.name] = SKIPPED
                continue
            found = await validator.check(req, ev)
            versions[validator.name] = validator.version
            if any(i.severity == "hard" for i in found):
                hard_by.add(validator.name)
            issues += found
        return ValidationResult(
            id=self._ids.new("va"),
            subject_type=req.subject_type,
            subject_id=req.subject_id,
            status=decide(issues, req.impact, repair_exhausted=repair_exhausted),
            issues=issues,
            confidence=self._confidence.compute(req, ev, issues),
            validator_versions=versions,
        )


def build_pipeline(
    workspace_id: str,
    models: Mapping[str, SemanticModel],
    ids: IdGenerator,
    previews: Previews | None = None,
) -> ValidationPipeline:
    """The v1 chain for one workspace; ``models`` maps dataset_version_id -> semantic model,
    ``previews`` evidence_id -> stored result rows (for ``rows.<i>.<column>`` number fields)."""
    validators: list[Validator] = [
        SchemaValidator(models),
        NumericalValidator(previews),
        EvidenceValidator(workspace_id),
        LogicValidator(),
    ]
    return ValidationPipeline(validators, ids)
