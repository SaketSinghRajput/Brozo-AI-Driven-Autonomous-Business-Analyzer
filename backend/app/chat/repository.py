"""Conversation and message store (TASK-CHAT-001, TASK-DB-008).

Access rule: a conversation is visible only inside its workspace (WHERE + RLS) and only to the
user who created it; ``owner_id=None`` (workspace Admins) lifts the owner filter, never the
workspace one. Messages are append-only (the app role holds INSERT/SELECT only).
"""

import json
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from sqlalchemy import text

from app.core.scope import WorkspaceScope
from app.infra.repository import Repository
from app.orchestrator.domain import DisplaySegment, TurnRecord, TurnState
from app.validator.domain import ConfidenceSummary, ValidationStatus


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Conversation(Contract):
    id: str
    owner_id: str
    dataset_version_id: str
    semantic_model_version: Annotated[int, Field(ge=1)]
    title: str
    created_at: datetime


class ChatMessage(Contract):
    id: str
    conversation_id: str
    role: Literal["user", "assistant"]
    content: str | None = None  # the question (user messages)
    # The stored answer (assistant messages); numbers carry the evidence value + unit.
    segments: list[DisplaySegment] | None = None
    state: TurnState | None = None
    validation_id: str | None = None
    validation_status: ValidationStatus | None = None  # final status of the answer
    confidence: ConfidenceSummary | None = None
    # "template": written by code from the analysis results (FinalEvent.answer_source)
    answer_source: Literal["template"] | None = None
    created_at: datetime


_CONVERSATION = (
    "SELECT id, owner_id, dataset_version_id, semantic_model_version, title, created_at "
    "FROM conversations WHERE workspace_id = :ws "
    "AND (CAST(:owner AS text) IS NULL OR owner_id = :owner)"
)


class ConversationRepository(Repository):
    def create(self, scope: WorkspaceScope, conversation: Conversation) -> None:
        self._session.execute(
            text(
                "INSERT INTO conversations (id, workspace_id, owner_id, dataset_version_id, "
                "semantic_model_version, title, created_at) VALUES (:id, :ws, :owner, :dsv, "
                ":smv, :title, :at)"
            ),
            {
                "id": conversation.id,
                "ws": scope.workspace_id,
                "owner": conversation.owner_id,
                "dsv": conversation.dataset_version_id,
                "smv": conversation.semantic_model_version,
                "title": conversation.title,
                "at": conversation.created_at,
            },
        )

    def get(
        self, scope: WorkspaceScope, conversation_id: str, owner_id: str | None
    ) -> Conversation | None:
        row = self._session.execute(
            text(f"{_CONVERSATION} AND id = :id"),
            {"ws": scope.workspace_id, "owner": owner_id, "id": conversation_id},
        ).one_or_none()
        return None if row is None else Conversation.model_validate(row._asdict())

    def conversations(
        self, scope: WorkspaceScope, owner_id: str | None, limit: int = 100
    ) -> list[Conversation]:
        rows = self._session.execute(
            text(f"{_CONVERSATION} ORDER BY created_at DESC, id DESC LIMIT :limit"),
            {"ws": scope.workspace_id, "owner": owner_id, "limit": limit},
        )
        return [Conversation.model_validate(r._asdict()) for r in rows]

    def add_question(
        self, scope: WorkspaceScope, message_id: str, conversation_id: str, content: str
    ) -> None:
        self._session.execute(
            text(
                "INSERT INTO messages (id, workspace_id, conversation_id, role, content) "
                "VALUES (:id, :ws, :cv, 'user', :content)"
            ),
            {"id": message_id, "ws": scope.workspace_id, "cv": conversation_id, "content": content},
        )

    def add_answer(self, scope: WorkspaceScope, record: TurnRecord) -> None:
        segments: list[JsonValue] = [s.model_dump(mode="json") for s in record.segments]
        verdict: JsonValue = None
        if record.validation_status is not None:
            verdict = {
                "status": record.validation_status.value,
                "confidence": None
                if record.confidence is None
                else record.confidence.model_dump(mode="json"),
            }
        turn = {**record.trace, "validation": verdict}
        self._session.execute(
            text(
                "INSERT INTO messages (id, workspace_id, conversation_id, role, segments, state, "
                "validation_id, turn) VALUES (:id, :ws, :cv, 'assistant', "
                "CAST(:segments AS jsonb), :state, :va, CAST(:turn AS jsonb))"
            ),
            {
                "id": record.message_id,
                "ws": scope.workspace_id,
                "cv": record.conversation_id,
                "segments": json.dumps(segments),
                "state": record.state.value,
                "va": record.validation_id,
                "turn": json.dumps(turn),
            },
        )

    def messages(self, scope: WorkspaceScope, conversation_id: str) -> list[ChatMessage]:
        rows = self._session.execute(
            text(
                "SELECT id, conversation_id, role, content, segments, state, validation_id, "
                "turn->'validation' AS verdict, turn->>'answer_source' AS answer_source, "
                "created_at FROM messages "
                "WHERE workspace_id = :ws AND conversation_id = :cv ORDER BY created_at, id"
            ),
            {"ws": scope.workspace_id, "cv": conversation_id},
        )
        return [_message(r._asdict()) for r in rows]


def _message(row: dict[str, Any]) -> ChatMessage:
    """Rows written before value/unit/verdict were stored read back with them empty."""
    verdict = row.pop("verdict") or {}
    if row["segments"] is not None:
        row["segments"] = [
            {"value": None, "unit": None, **s} if s.get("type") == "number" else s
            for s in row["segments"]
        ]
    return ChatMessage.model_validate(
        {**row, "validation_status": verdict.get("status"), "confidence": verdict.get("confidence")}
    )
