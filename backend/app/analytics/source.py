"""DatasetSource: the one dataset version (Parquet) + semantic model a query may see.

Both are read inside the caller's workspace scope, so another workspace's version or model is
simply "not found". The Parquet bytes are hashed while loading and must equal the version's
``content_hash`` (ADR-0023), so evidence always refers to exactly the bytes the numbers came
from. ``storage_uri`` is a Storage key (set by ingestion, TASK-DATA-008).
"""

import hashlib
from dataclasses import dataclass

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import text

from app.core.errors import NotFoundError, SecurityError
from app.core.scope import WorkspaceScope
from app.data.semantic.domain import SemanticModel
from app.infra.db import UnitOfWork
from app.infra.storage import Storage


@dataclass(frozen=True, slots=True)
class LoadedDataset:
    model: SemanticModel
    table: pa.Table


class DatasetSource:
    def __init__(self, uow: UnitOfWork, storage: Storage) -> None:
        self._uow = uow
        self._storage = storage

    def load(
        self, scope: WorkspaceScope, dataset_version_id: str, semantic_model_version: int
    ) -> LoadedDataset:
        with self._uow(scope) as s:
            version = s.execute(
                text(
                    "SELECT storage_uri, content_hash FROM dataset_versions "
                    "WHERE id = :id AND workspace_id = :ws"
                ),
                {"id": dataset_version_id, "ws": scope.workspace_id},
            ).one_or_none()
            raw_model = s.execute(
                text(
                    "SELECT model FROM semantic_models WHERE workspace_id = :ws "
                    "AND dataset_version_id = :id AND version = :v"
                ),
                {"id": dataset_version_id, "ws": scope.workspace_id, "v": semantic_model_version},
            ).scalar_one_or_none()
        if version is None or version.storage_uri is None or version.content_hash is None:
            raise NotFoundError("dataset version not found or not ingested")
        if raw_model is None:
            raise NotFoundError("semantic model version not found")
        model = SemanticModel.model_validate(raw_model)
        if (
            model.dataset_version_id != dataset_version_id
            or model.version != semantic_model_version
        ):
            raise SecurityError("semantic model does not belong to this dataset version")
        with self._storage.open_read(version.storage_uri) as f:
            data = f.read()
        if f"sha256:{hashlib.sha256(data).hexdigest()}" != version.content_hash:
            raise SecurityError("stored dataset does not match its content hash")
        return LoadedDataset(model=model, table=pq.read_table(pa.BufferReader(data)))
