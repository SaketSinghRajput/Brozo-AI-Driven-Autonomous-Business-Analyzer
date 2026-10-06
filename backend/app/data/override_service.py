"""User overrides of domain and semantic model (TASK-DOMAIN-007, TASK-SEM-009; design 9, 14-15).

Both produce a NEW semantic model version (versions are immutable), re-mapped with the effective
overrides applied last, in one transaction with: the `SemanticModelChanged` outbox event (-> the
re-plan job), a READY -> SEMANTICALLY_MAPPED transition when the version was READY, and an audit
entry. Allowed only once a version has a semantic model (SEMANTICALLY_MAPPED or READY); a version
that is still being understood or is ANALYZING answers 409. A domain override on a version that is
DOMAIN_IDENTIFIED is stored only (the running pipeline maps with the current domain row).
Concurrent PATCHes are serialised by ``base_version`` (409 when it is not the current version).
Review flags on a version are cumulative (set union), so a confirmed MAPPING_AMBIGUOUS flag stays
in its history; the current model is the source of truth for what is mapped.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.core.errors import AppValidationError, NotFoundError, StateConflictError
from app.core.ids import IdGenerator
from app.core.scope import WorkspaceScope
from app.data.domain import DatasetState, DatasetVersion
from app.data.repository import DatasetRepository
from app.data.semantic.domain import Override, SemanticModel
from app.data.semantic.mapper import SemanticMapper
from app.data.semantic.overrides import remap
from app.data.understanding.domain import DomainClassification
from app.data.understanding.domain_detector import DomainDetector
from app.data.understanding_pipeline import domain_flags, table_name
from app.data.understanding_store import UnderstandingStore
from app.infra.db import UnitOfWork
from app.security.audit import AuditKeys, AuditLog

S = DatasetState
_MAPPED = frozenset({S.SEMANTICALLY_MAPPED, S.READY})


class OverrideInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str = Field(min_length=1, max_length=300)
    value: str | None = Field(max_length=2000)


@dataclass(frozen=True)
class DomainOverrideResult:
    version_id: str
    domain: DomainClassification
    semantic_model_version: int | None  # None: the version is not mapped yet


class OverrideService:
    def __init__(
        self,
        uow: UnitOfWork,
        ids: IdGenerator,
        clock: Clock,
        audit_keys: AuditKeys,
        *,
        detector: DomainDetector | None = None,
        mapper: SemanticMapper | None = None,
    ) -> None:
        self._uow = uow
        self._ids = ids
        self._clock = clock
        self._keys = audit_keys
        self._detector = detector or DomainDetector()
        self._mapper = mapper or SemanticMapper()

    def set_domain(
        self, scope: WorkspaceScope, dataset_id: str, domain: str
    ) -> DomainOverrideResult:
        try:
            classification = self._detector.override(domain, by=scope.principal_id)
        except ValueError:
            raise AppValidationError(f"unknown domain {domain!r}") from None
        with self._uow(scope) as s:
            repo = DatasetRepository(s)
            if repo.get_dataset(scope, dataset_id) is None:
                raise NotFoundError("dataset not found")
            versions = repo.list_versions(scope, dataset_id)
            version = versions[-1] if versions else None
            if version is None or version.state not in _MAPPED | {S.DOMAIN_IDENTIFIED}:
                raise StateConflictError("the latest version has no identified domain yet")
            store = UnderstandingStore(s)
            record = store.put_domain(
                scope,
                version.id,
                classification,
                "user_override",
                created_by=scope.principal_id,
                append=True,
            )
            model = None
            if version.state in _MAPPED:
                current = store.semantic_model(scope, version.id)
                if current is None:
                    raise StateConflictError("the version has no semantic model")
                model = self._remap(s, scope, version, current, current.overrides, strict=False)
            AuditLog(s, self._keys, self._clock, self._ids).append(
                scope,
                action="dataset.domain_override",
                resource=f"dataset_version:{version.id}",
                result="success",
                metadata={"domain": domain, "seq": record.seq},
            )
        return DomainOverrideResult(version.id, classification, model.version if model else None)

    def patch_semantic_model(
        self,
        scope: WorkspaceScope,
        dataset_id: str,
        version_id: str,
        base_version: int,
        changes: Sequence[OverrideInput],
    ) -> SemanticModel:
        if not changes:
            raise AppValidationError("at least one override is required")
        now = self._clock.now()
        new = [
            Override(field=c.field, value=c.value, by=scope.principal_id, at=now) for c in changes
        ]
        with self._uow(scope) as s:
            repo = DatasetRepository(s)
            version = repo.get_version(scope, version_id)
            if version is None or version.dataset_id != dataset_id:
                raise NotFoundError("dataset version not found")
            if version.state not in _MAPPED:
                raise StateConflictError(f"the semantic model cannot change while {version.state}")
            current = UnderstandingStore(s).semantic_model(scope, version_id)
            if current is None:
                raise StateConflictError("the version has no semantic model")
            if current.version != base_version:
                raise StateConflictError(
                    f"base_version {base_version} is not the current version {current.version}"
                )
            fields = {o.field for o in new}
            effective = [o for o in current.overrides if o.field not in fields] + new
            model = self._remap(s, scope, version, current, effective, strict=True)
            AuditLog(s, self._keys, self._clock, self._ids).append(
                scope,
                action="semantic_model.override",
                resource=f"dataset_version:{version_id}",
                result="success",
                metadata={"semantic_model_version": model.version, "overrides": len(new)},
            )
        return model

    def _remap(
        self,
        s: Session,
        scope: WorkspaceScope,
        version: DatasetVersion,
        current: SemanticModel,
        overrides: Sequence[Override],
        *,
        strict: bool,
    ) -> SemanticModel:
        store, repo = UnderstandingStore(s), DatasetRepository(s)
        understanding = store.latest(scope, version.id)
        domain = store.domain(scope, version.id)
        dataset = repo.get_dataset(scope, version.dataset_id)
        if understanding is None or domain is None or dataset is None:
            raise StateConflictError("the version has no understanding or domain")
        remapped = remap(
            self._mapper,
            understanding,
            domain.classification,
            table=table_name(dataset.name),
            overrides=overrides,
            previous=current,
            strict=strict,
        )
        if not store.put_semantic_model(scope, remapped.model, "overridden"):
            raise StateConflictError("the semantic model changed concurrently; retry")
        if version.state is S.READY:
            flags = remapped.review_flags + domain_flags(domain.classification)
            repo.transition(scope, version.id, S.READY, S.SEMANTICALLY_MAPPED, add_flags=flags)
        event = {
            "dataset_id": version.dataset_id,
            "version_id": version.id,
            "semantic_model_version": remapped.model.version,
        }
        repo.add_outbox(scope, self._ids.new("evt"), "SemanticModelChanged", event)
        return remapped.model
