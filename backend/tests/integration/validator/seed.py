"""Seed a workspace with a dataset version, evidence and an optional semantic model (synthetic)."""

import uuid
from dataclasses import dataclass

from sqlalchemy import text

from app.core.scope import SystemScope, WorkspaceScope
from app.data.semantic.domain import SemanticModel
from app.evidence.domain import Evidence
from app.evidence.repository import EvidenceRepository
from app.infra.db import UnitOfWork

HASH = "sha256:" + "ab" * 32


@dataclass(frozen=True)
class Seeded:
    scope: WorkspaceScope
    dataset_id: str
    dataset_version_id: str


def workspace(uow: UnitOfWork) -> Seeded:
    ws, ds, dsv = (f"{p}_{uuid.uuid4().hex[:12]}" for p in ("ws", "ds", "dsv"))
    with uow(SystemScope("system:test")) as s:
        s.execute(text("INSERT INTO workspaces (id, name) VALUES (:id, 'val')"), {"id": ws})
        s.execute(
            text(
                "INSERT INTO datasets (id, workspace_id, name, source_type) "
                "VALUES (:id, :ws, 'orders', 'csv')"
            ),
            {"id": ds, "ws": ws},
        )
        s.execute(
            text(
                "INSERT INTO dataset_versions (id, workspace_id, dataset_id, version_no, "
                "source_sha256, content_hash, state) VALUES (:id, :ws, :ds, 1, :h, :h, 'INGESTED')"
            ),
            {"id": dsv, "ws": ws, "ds": ds, "h": HASH},
        )
    return Seeded(WorkspaceScope(workspace_id=ws, principal_id="usr_test"), ds, dsv)


def store_evidence(uow: UnitOfWork, seeded: Seeded, *records: Evidence) -> None:
    with uow(seeded.scope) as s:
        for record in records:
            EvidenceRepository(s).insert(seeded.scope, record)


def store_model(uow: UnitOfWork, seeded: Seeded, model: SemanticModel) -> None:
    with uow(seeded.scope) as s:
        s.execute(
            text(
                "INSERT INTO semantic_models (id, workspace_id, dataset_version_id, version, "
                "model, status) VALUES (:id, :ws, :dsv, :v, CAST(:m AS jsonb), 'active')"
            ),
            {
                "id": f"sm_{uuid.uuid4().hex[:12]}",
                "ws": seeded.scope.workspace_id,
                "dsv": seeded.dataset_version_id,
                "v": model.version,
                "m": model.model_dump_json(by_alias=True),
            },
        )
