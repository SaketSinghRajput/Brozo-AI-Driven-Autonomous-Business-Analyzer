"""Append-only store for validation results + the ValidationCompleted outbox event (TASK-VAL-007).

There is no update or delete method; the app role holds only INSERT/SELECT on
``validation_results`` (migration 0008). The result row and its event commit in the caller's
transaction (design 51 transactional outbox).

``semantic_model`` reads ``semantic_models`` directly because no semantic-model repository exists
yet; switch to it when the data stream adds one.
"""

import json

from sqlalchemy import text

from app.core.scope import WorkspaceScope
from app.data.semantic.domain import SemanticModel
from app.infra.repository import Repository
from app.validator.domain import ValidationResult

EVENT_TYPE = "ValidationCompleted"


class ValidationRepository(Repository):
    def insert(self, scope: WorkspaceScope, result: ValidationResult, event_id: str) -> None:
        self._session.execute(
            text(
                "INSERT INTO validation_results (id, workspace_id, subject_type, subject_id, "
                "status, payload) VALUES (:id, :ws, :st, :sid, :status, CAST(:payload AS jsonb))"
            ),
            {
                "id": result.id,
                "ws": scope.workspace_id,
                "st": result.subject_type,
                "sid": result.subject_id,
                "status": result.status.value,
                "payload": result.model_dump_json(),
            },
        )
        event = {
            "validation_id": result.id,
            "workspace_id": scope.workspace_id,
            "subject_type": result.subject_type,
            "subject_id": result.subject_id,
            "status": result.status.value,
        }
        self._session.execute(
            text(
                "INSERT INTO outbox_events (id, event_type, payload) "
                "VALUES (:id, :type, CAST(:payload AS jsonb))"
            ),
            {"id": event_id, "type": EVENT_TYPE, "payload": json.dumps(event)},
        )

    def get(self, scope: WorkspaceScope, validation_id: str) -> ValidationResult | None:
        payload = self._session.execute(
            text("SELECT payload FROM validation_results WHERE workspace_id = :ws AND id = :id"),
            {"ws": scope.workspace_id, "id": validation_id},
        ).scalar_one_or_none()
        return None if payload is None else ValidationResult.model_validate(payload)

    def semantic_model(
        self, scope: WorkspaceScope, dataset_version_id: str, version: int | None
    ) -> SemanticModel | None:
        """The pinned version, or the latest one when ``version`` is None."""
        model = self._session.execute(
            text(
                "SELECT model FROM semantic_models WHERE workspace_id = :ws "
                "AND dataset_version_id = :dsv AND (CAST(:v AS integer) IS NULL OR version = :v) "
                "ORDER BY version DESC LIMIT 1"
            ),
            {"ws": scope.workspace_id, "dsv": dataset_version_id, "v": version},
        ).scalar_one_or_none()
        return None if model is None else SemanticModel.model_validate(model)
