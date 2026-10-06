"""ValidationService: load evidence and semantic models, run the pipeline, persist (TASK-VAL-007).

Evidence is loaded through the workspace-scoped ``EvidenceRepository``, so records of another
workspace never enter the bundle and surface as MISSING_EVIDENCE. For each cited dataset
version the semantic model is the version the request pins, else the version the evidence was
built on, else the latest. Database work runs in a worker thread (sync SQLAlchemy, ADR-0022).
"""

import asyncio

from app.core.ids import IdGenerator
from app.core.scope import WorkspaceScope
from app.data.semantic.domain import SemanticModel
from app.evidence.domain import Evidence
from app.evidence.preview import RowsPreview
from app.evidence.repository import EvidenceRepository
from app.infra.db import UnitOfWork
from app.validator.domain import NumberSegment, ValidationRequest, ValidationResult, cited_ids
from app.validator.pipeline import build_pipeline
from app.validator.repository import ValidationRepository


class ValidationService:
    def __init__(self, uow: UnitOfWork, ids: IdGenerator) -> None:
        self._uow = uow
        self._ids = ids

    async def validate(
        self, scope: WorkspaceScope, req: ValidationRequest, *, repair_exhausted: bool = False
    ) -> ValidationResult:
        bundle, models, previews = await asyncio.to_thread(self._load, scope, req)
        pipeline = build_pipeline(scope.workspace_id, models, self._ids, previews)
        result = await pipeline.run(req, bundle, repair_exhausted=repair_exhausted)
        await asyncio.to_thread(self._persist, scope, result)
        return result

    def _load(
        self, scope: WorkspaceScope, req: ValidationRequest
    ) -> tuple[dict[str, Evidence], dict[str, SemanticModel], dict[str, RowsPreview]]:
        with self._uow(scope) as s:
            evidence = EvidenceRepository(s)
            bundle = {e.evidence_id: e for e in evidence.get_many(scope, cited_ids(req))}
            previews: dict[str, RowsPreview] = {}
            wanted_rows = {  # only numbers citing result rows need the stored preview
                seg.evidence_id
                for seg in req.segments or []
                if isinstance(seg, NumberSegment) and seg.field.startswith("rows.")
            }
            for evidence_id in sorted(wanted_rows & bundle.keys()):
                preview = evidence.rows(scope, evidence_id)[1]
                if preview is not None:
                    previews[evidence_id] = preview
            wanted: dict[str, int | None] = {}
            for e in bundle.values():
                if e.dataset_version_id is not None:
                    pinned = req.semantic_model_version or e.semantic_model_version
                    wanted.setdefault(e.dataset_version_id, pinned)
            repo = ValidationRepository(s)
            models = {
                dsv: model
                for dsv, version in wanted.items()
                if (model := repo.semantic_model(scope, dsv, version)) is not None
            }
        return bundle, models, previews

    def _persist(self, scope: WorkspaceScope, result: ValidationResult) -> None:
        with self._uow(scope) as s:
            ValidationRepository(s).insert(scope, result, self._ids.new("evt"))
