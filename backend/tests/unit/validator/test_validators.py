"""Unit tests per validator, the confidence formula and the pipeline decision table."""

import asyncio
from collections.abc import Mapping

import pytest
from pydantic import ValidationError

from app.core.ids import SequentialIds
from app.data.semantic.domain import SemanticModel
from app.evidence.domain import Evidence, EvidenceResult
from app.evidence.preview import PreviewColumn, RowsPreview
from app.validator.confidence import ConfidenceCalculator
from app.validator.domain import (
    ConfidenceSummary,
    EvidenceBundle,
    ValidationIssue,
    ValidationRequest,
    ValidationResult,
    ValidationStatus,
    Validator,
)
from app.validator.evidence import EvidenceValidator
from app.validator.logic import LogicValidator
from app.validator.numerical import NumericalValidator, resolve
from app.validator.pipeline import SKIPPED, ValidationPipeline, decide
from app.validator.schema import SchemaValidator, references

from .factories import AT, DSV, HASH, WS, bundle, evidence, number, request, semantic_model, text

S = ValidationStatus


def _codes(validator: Validator, req: ValidationRequest, ev: EvidenceBundle) -> list[str]:
    return [i.code for i in asyncio.run(validator.check(req, ev))]


def _issue(severity: str, code: str = "X") -> ValidationIssue:
    return ValidationIssue.model_validate(
        {"code": code, "severity": severity, "target": "t", "detail": "d"}
    )


# --- NumericalValidator -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fmt", "field", "code"),
    [
        ("pct:x", "result.value", "INVALID_FORMAT"),
        ("pct:1", "result.nothing", "FIELD_NOT_NUMERIC"),
        ("pct:1", "created_at", "FIELD_NOT_NUMERIC"),
        ("currency:INR:0", "result.value", "UNIT_MISMATCH"),
        ("count", "result.rows", None),
        ("number:0", "arguments.limits.0", None),
        ("number:0", "arguments.limits.9", "FIELD_NOT_NUMERIC"),
    ],
)
def test_number_segment_checks(fmt: str, field: str, code: str | None) -> None:
    ev = bundle(evidence(arguments={"metric": "Revenue", "limits": [5]}))
    codes = _codes(NumericalValidator(), request(number(fmt=fmt, field=field)), ev)
    assert codes == ([code] if code else [])


def test_number_segment_with_unknown_evidence_is_left_to_evidence_validator() -> None:
    assert _codes(NumericalValidator(), request(number("ev_none")), {}) == []


def test_free_numbers_match_arguments_and_skip_empty_result_fields() -> None:
    record = evidence(value=None, rows=None, arguments={"metric": "Revenue", "top": 5})
    req = request(text("The top 5 regions."), text(" Also 6 others."))
    codes = _codes(NumericalValidator(), req, bundle(record))
    assert codes == ["NUMBER_WITHOUT_EVIDENCE"]


def test_direction_after_the_number() -> None:
    ev = bundle(evidence())
    assert _codes(NumericalValidator(), request(text("A 14.2% decline.")), ev) == []
    assert _codes(NumericalValidator(), request(text("A 14.2% increase.")), ev) == [
        "DIRECTION_MISMATCH"
    ]


def test_resolve_walks_dicts_and_lists() -> None:
    record = evidence(arguments={"metric": "Revenue", "k": [1, {"v": 2}], "flag": True})
    assert resolve(record, "arguments.k.1.v") == 2
    assert resolve(record, "arguments.flag") is None
    assert resolve(record, "arguments.k.x") is None


FORECAST_ROWS = RowsPreview(
    columns=[
        PreviewColumn(name="period", type="date"),
        PreviewColumn(name="value", type="float"),
        PreviewColumn(name="lower", type="float"),
        PreviewColumn(name="upper", type="float", unit="currency:INR"),
    ],
    rows=[["2026-10-01", 120.5, 100.0, 141.0]],
    truncated=False,
    row_count=1,
)


@pytest.mark.parametrize(
    ("fmt", "field", "code"),
    [
        ("number:1", "rows.0.value", None),
        ("number:0", "rows.0.lower", None),
        ("currency:INR:0", "rows.0.upper", None),  # the preview column's unit
        ("number:0", "rows.0.upper", "UNIT_MISMATCH"),
        ("number:0", "rows.1.value", "FIELD_NOT_NUMERIC"),
        ("number:0", "rows.0.period", "FIELD_NOT_NUMERIC"),
        ("number:0", "rows.0.nothing", "FIELD_NOT_NUMERIC"),
    ],
)
def test_number_segments_can_cite_stored_result_rows(
    fmt: str, field: str, code: str | None
) -> None:
    ev = bundle(evidence(value=None, unit=None))
    validator = NumericalValidator({"ev_rev": FORECAST_ROWS})
    codes = _codes(validator, request(number(fmt=fmt, field=field)), ev)
    assert codes == ([code] if code else [])


def test_result_rows_without_a_stored_preview_are_not_numbers() -> None:
    ev = bundle(evidence(value=None, unit=None))
    req = request(number(fmt="number:0", field="rows.0.value"))
    assert _codes(NumericalValidator(), req, ev) == ["FIELD_NOT_NUMERIC"]


# --- SchemaValidator -------------------------------------------------------------------------


def test_references_from_all_argument_shapes() -> None:
    refs = references(
        {
            "metric": "Revenue",
            "dimensions": ["Region", 3],
            "limit": 5,
            "table": None,
            "filters": [{"column": "region"}, {"dimension": ["order_date"]}, "junk"],
        }
    )
    assert refs == [
        ("metric", "Revenue"),
        ("dimension", "Region"),
        ("column", "region"),
        ("column", "order_date"),
    ]
    assert references({"filters": {"region": "West"}}) == [("column", "region")]
    assert references({"filters": "region = 'West'"}) == []


def _schema(models: Mapping[str, SemanticModel] | None = None) -> SchemaValidator:
    return SchemaValidator({DSV: semantic_model()} if models is None else models)


def test_schema_accepts_known_names_case_insensitively() -> None:
    args = {"metric": "sales", "dimensions": ["REGION"], "table": "orders", "column": "amount"}
    ev = bundle(evidence(arguments=args))  # type: ignore[arg-type]
    assert _codes(_schema(), request(text("x")), ev) == []


def test_schema_unknown_table_and_skipped_evidence() -> None:
    ev = bundle(evidence(arguments={"table": "payments"}), evidence("ev_none", arguments={}))
    req = request(text("x"), evidence_ids=["ev_rev", "ev_none", "ev_missing"])
    assert _codes(_schema(), req, ev) == ["UNKNOWN_TABLE"]


def test_schema_without_models() -> None:
    ev = bundle(evidence())
    quoted = request(text("By `region`."))
    assert _codes(_schema({}), quoted, ev) == ["SCHEMA_UNVERIFIED", "SCHEMA_UNVERIFIED"]
    pinned = request(text("By `region`."), semantic_model_version=3)
    assert _codes(_schema({}), pinned, ev) == ["UNKNOWN_SEMANTIC_MODEL"] * 2


def test_schema_ignores_quotes_in_number_segments() -> None:
    assert _codes(_schema(), request(number(), text("`region`")), bundle(evidence())) == []


# --- EvidenceValidator -------------------------------------------------------------------


def test_evidence_without_dataset_and_mismatched_bundle_key() -> None:
    citation = Evidence(
        evidence_id="ev_doc",
        workspace_id=WS,
        kind="citation",
        dataset_id=None,
        dataset_version_id=None,
        dataset_content_hash=None,
        semantic_model_version=None,
        operation="rag.search",
        tool_version="1.0.0",
        arguments={},
        result=EvidenceResult(result_hash=HASH),
        created_at=AT,
        software_version="0.1.0+test",
    )
    validator = EvidenceValidator(WS)
    req = request(text("x"), evidence_ids=["ev_doc"], semantic_model_version=1)
    assert _codes(validator, req, {"ev_doc": citation}) == []
    assert _codes(validator, req, {"ev_doc": evidence()}) == ["FOREIGN_EVIDENCE"]


def test_same_dataset_version_twice_is_consistent() -> None:
    req = request(text("x"), evidence_ids=["ev_rev", "ev_b"])
    assert _codes(EvidenceValidator(WS), req, bundle(evidence(), evidence("ev_b"))) == []


# --- LogicValidator --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sentence", "codes"),
    [
        ("Sales rose because of the campaign.", ["CAUSAL_LANGUAGE"]),
        ("The drop is due to missing data for June.", []),
        ("The drop is due to the new competitor.", ["CAUSAL_LANGUAGE"]),
        ("There is no guarantee of recovery.", []),
        ("Recovery is guaranteed.", ["GUARANTEE_LANGUAGE"]),
        ("This outlier could be a data error; please verify.", []),
        ("The outlier is an error.", ["ANOMALY_AS_PROBLEM"]),
        ("A statistical anomaly ≠ business problem.", []),  # the ML caveat itself
        ("This anomaly is not a problem.", []),
    ],
)
def test_logic_lexicon(sentence: str, codes: list[str]) -> None:
    assert _codes(LogicValidator(), request(text(sentence)), bundle(evidence())) == codes


def test_causal_wording_allowed_with_causal_evidence() -> None:
    ev = bundle(evidence(operation="causal.experiment"))
    assert _codes(LogicValidator(), request(text("The campaign caused the lift.")), ev) == []


def test_contradiction_needs_direction_words() -> None:
    req = request(text("Revenue was "), number(), text(" and fell "), number())
    assert _codes(LogicValidator(), req, bundle(evidence())) == []


# --- ConfidenceCalculator ----------------------------------------------------------------


def test_confidence_formula() -> None:
    calc = ConfidenceCalculator()
    ev = bundle(evidence())
    hard = calc.compute(request(text("x")), ev, [_issue("hard")])
    assert (hard.score, hard.level) == (0.0, "low")
    full = calc.compute(request(number()), ev, [])
    assert (full.score, full.level, full.basis) == (1.0, "high", ["numbers_verified:1/1"])
    flagged = calc.compute(request(number(), number()), ev, [_issue("soft", "SIGN_OMITTED")])
    assert flagged.basis[0] == "numbers_verified:1/2"
    assert flagged.score == round(0.5 * 0.85, 4)
    ml = bundle(evidence(model=True, confidence=0.7))
    estimate = calc.compute(request(number()), ml, [_issue("soft", "ANOMALY_AS_PROBLEM")])
    assert estimate.score == round(0.85 * 0.9 * 0.7, 4)
    assert estimate.level == "medium"
    assert estimate.basis[1:] == [
        "soft_issues:1",
        "model_estimate",
        "evidence_confidence_min:0.70",
    ]
    prose = calc.compute(request(text("Fine."), evidence_ids=[]), {}, [_issue("soft")])
    assert (prose.score, prose.level, prose.basis[0]) == (0.425, "low", "ungrounded")
    narrative = calc.compute(request(text("Fine.")), ev, [_issue("soft")])
    assert (narrative.score, narrative.level, narrative.basis[0]) == (0.85, "high", "no_numbers")


# --- Pipeline ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("severities", "impact", "exhausted", "status"),
    [
        ([], "normal", False, S.VALID),
        ([], "high", False, S.VALID),
        (["soft"], "normal", False, S.UNCERTAIN),
        (["soft"], "high", False, S.REQUIRES_REVIEW),
        (["hard"], "normal", False, S.INVALID),
        (["hard", "soft"], "high", False, S.INVALID),
        (["hard"], "normal", True, S.REQUIRES_REVIEW),
        (["soft"], "normal", True, S.UNCERTAIN),
    ],
)
def test_decision_table(
    severities: list[str], impact: str, exhausted: bool, status: ValidationStatus
) -> None:
    issues = [_issue(s) for s in severities]
    assert decide(issues, impact, repair_exhausted=exhausted) is status


class _Fixed:
    def __init__(self, name: str, severity: str | None) -> None:
        self.name, self.version, self._severity, self.calls = name, "9.9.9", severity, 0

    async def check(self, req: ValidationRequest, ev: EvidenceBundle) -> list[ValidationIssue]:
        self.calls += 1
        return [_issue(self._severity)] if self._severity else []


def test_schema_hard_issue_short_circuits_numerical_only() -> None:
    schema, numerical = _Fixed("schema", "hard"), _Fixed("numerical", None)
    logic = _Fixed("logic", None)
    pipeline = ValidationPipeline([schema, numerical, logic], SequentialIds())
    result = asyncio.run(pipeline.run(request(text("x")), {}))
    assert (schema.calls, numerical.calls, logic.calls) == (1, 0, 1)
    assert result.validator_versions["numerical"] == SKIPPED
    assert result.validator_versions["logic"] == "9.9.9"
    assert result.status is S.INVALID
    assert result.id == "va_000001"


def test_result_contract_rejects_invalid_without_hard_issue() -> None:
    with pytest.raises(ValidationError, match="INVALID requires at least one hard issue"):
        ValidationResult(
            id="va_1",
            subject_type="answer",
            subject_id="a",
            status=S.INVALID,
            issues=[_issue("soft")],
            confidence=ConfidenceSummary(level="low", score=0.1, basis=[]),
            validator_versions={},
        )


def test_soft_schema_issue_does_not_short_circuit() -> None:
    schema, numerical = _Fixed("schema", "soft"), _Fixed("numerical", None)
    asyncio.run(ValidationPipeline([schema, numerical], SequentialIds()).run(request(), {}))
    assert numerical.calls == 1
