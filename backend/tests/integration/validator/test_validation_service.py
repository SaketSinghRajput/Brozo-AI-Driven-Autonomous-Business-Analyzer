"""TASK-VAL-007 / TASK-DB-006: the pipeline against real evidence, models, persistence, event."""

import asyncio
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError

from app.core.ids import UuidV7Ids
from app.evidence.domain import Evidence, EvidenceResult
from app.infra.db import UnitOfWork
from app.validator.domain import ValidationRequest, ValidationResult, ValidationStatus
from app.validator.repository import EVENT_TYPE, ValidationRepository
from app.validator.service import ValidationService
from tests.unit.validator.factories import AT, HASH, evidence, number, request, semantic_model
from tests.unit.validator.factories import text as seg

from .seed import Seeded, store_evidence, store_model, workspace

S = ValidationStatus
EVENT = text(
    "SELECT payload FROM outbox_events WHERE event_type = :t AND payload->>'validation_id' = :id"
)


@pytest.fixture
def uow(app_engine: Engine) -> UnitOfWork:
    return UnitOfWork(app_engine)


@pytest.fixture
def seeded(uow: UnitOfWork) -> Seeded:
    return workspace(uow)


def _evidence(seeded: Seeded, **kw: Any) -> Evidence:
    return evidence(
        f"ev_{seeded.dataset_version_id}",
        workspace_id=seeded.scope.workspace_id,
        dataset_id=seeded.dataset_id,
        dataset_version_id=seeded.dataset_version_id,
        **kw,
    )


def _validate(uow: UnitOfWork, seeded: Seeded, req: ValidationRequest) -> ValidationResult:
    return asyncio.run(ValidationService(uow, UuidV7Ids()).validate(seeded.scope, req))


def _answer(seeded: Seeded, **kw: Any) -> ValidationRequest:
    ev_id = f"ev_{seeded.dataset_version_id}"
    return request(seg("Revenue fell "), number(ev_id), evidence_ids=[ev_id], **kw)


def test_valid_answer_is_persisted_with_its_event(uow: UnitOfWork, seeded: Seeded) -> None:
    store_evidence(uow, seeded, _evidence(seeded))
    store_model(uow, seeded, semantic_model(1, seeded.dataset_version_id))
    result = _validate(uow, seeded, _answer(seeded))
    assert result.status is S.VALID, result.issues
    with uow(seeded.scope) as s:
        assert ValidationRepository(s).get(seeded.scope, result.id) == result
        payload = s.execute(EVENT, {"t": EVENT_TYPE, "id": result.id}).scalar_one()
    assert payload["status"] == "VALID"
    assert payload["workspace_id"] == seeded.scope.workspace_id


def test_evidence_of_another_workspace_is_missing(uow: UnitOfWork, seeded: Seeded) -> None:
    other = workspace(uow)
    store_evidence(uow, other, _evidence(other))
    result = _validate(uow, seeded, _answer(other))
    assert result.status is S.INVALID
    assert [i.code for i in result.issues] == ["MISSING_EVIDENCE"]


def test_latest_semantic_model_catches_unknown_metric(uow: UnitOfWork, seeded: Seeded) -> None:
    unversioned = _evidence(seeded, arguments={"metric": "Profit"}, semantic_model_version=None)
    store_evidence(uow, seeded, unversioned)
    store_model(uow, seeded, semantic_model(1, seeded.dataset_version_id))
    store_model(uow, seeded, semantic_model(2, seeded.dataset_version_id))
    result = _validate(uow, seeded, _answer(seeded))
    assert [i.code for i in result.issues] == ["UNKNOWN_METRIC"]
    assert result.validator_versions["numerical"] == "skipped"


def test_pinned_semantic_version_that_does_not_exist(uow: UnitOfWork, seeded: Seeded) -> None:
    store_evidence(uow, seeded, _evidence(seeded, semantic_model_version=None))
    store_model(uow, seeded, semantic_model(1, seeded.dataset_version_id))
    result = _validate(uow, seeded, _answer(seeded, semantic_model_version=7))
    assert [i.code for i in result.issues] == ["UNKNOWN_SEMANTIC_MODEL"]


def test_rows_field_without_a_stored_preview_is_not_a_number(
    uow: UnitOfWork, seeded: Seeded
) -> None:
    """A ``rows.<i>.<col>`` number whose evidence has no stored result rows cannot be verified."""
    store_evidence(uow, seeded, _evidence(seeded))
    store_model(uow, seeded, semantic_model(1, seeded.dataset_version_id))
    ev_id = f"ev_{seeded.dataset_version_id}"
    req = request(seg("Estimate "), number(ev_id, "number:2", "rows.0.value"), evidence_ids=[ev_id])
    result = _validate(uow, seeded, req)
    assert result.status is S.INVALID
    assert [i.code for i in result.issues] == ["FIELD_NOT_NUMERIC"]


def test_citation_evidence_needs_no_semantic_model(uow: UnitOfWork, seeded: Seeded) -> None:
    citation = Evidence(
        evidence_id=f"ev_doc_{seeded.dataset_version_id}",
        workspace_id=seeded.scope.workspace_id,
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
    store_evidence(uow, seeded, citation)
    req = request(seg("The policy covers returns."), evidence_ids=[citation.evidence_id])
    assert _validate(uow, seeded, req).status is S.VALID


def test_results_are_immutable_for_the_app_role(uow: UnitOfWork, seeded: Seeded) -> None:
    result = _validate(uow, seeded, request(seg("Nothing to report."), evidence_ids=[]))
    assert result.status is S.UNCERTAIN
    for sql in (
        "UPDATE validation_results SET status = 'VALID' WHERE id = :id",
        "DELETE FROM validation_results WHERE id = :id",
    ):
        with pytest.raises(ProgrammingError, match="permission denied"), uow(seeded.scope) as s:
            s.execute(text(sql), {"id": result.id})


def test_repository_get_is_workspace_scoped(uow: UnitOfWork, seeded: Seeded) -> None:
    result = _validate(uow, seeded, request(seg("Fine."), evidence_ids=[]))
    other = workspace(uow)
    with uow(other.scope) as s:
        assert ValidationRepository(s).get(other.scope, result.id) is None
