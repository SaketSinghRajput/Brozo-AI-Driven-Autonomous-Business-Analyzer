"""Chat API (TASK-CHAT-001/002/003; TRD §42, ADR-0020).

- ``POST /chat/conversations`` (RUN_ANALYSIS): bind a conversation to one dataset version +
  semantic model version of the caller's workspace.
- ``GET /chat/conversations``, ``GET /chat/conversations/{id}``, ``GET .../{id}/messages``
  (VIEW_DATASET): the caller's own conversations; workspace Admins see all of the workspace.
  Someone else's conversation answers 404 (existence is not revealed).
- ``POST /chat/conversations/{id}/messages`` (RUN_ANALYSIS): stores the question, then streams
  the turn as Server-Sent Events. Each SSE event is ``event: <type>``, ``id: <seq>`` and
  ``data: <TurnEvent JSON>`` (shared/schemas/turn_event.schema.json); the order is documented
  in app/orchestrator/domain.py. A client disconnect cancels the turn.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI
from pydantic import BaseModel, ConfigDict, Field
from sse_starlette import EventSourceResponse, ServerSentEvent

from app.api.deps import ContainerDep, PrincipalDep, require
from app.chat.repository import ChatMessage, Conversation
from app.chat.service import ChatService, build_orchestrator
from app.core.logging import trace_id_var
from app.core.scope import WorkspaceScope
from app.orchestrator.domain import TurnEvent
from app.security.permissions import Permission

router = APIRouter(prefix="/chat", tags=["chat"])
SSE_PING_S = 15
COMPONENTS = "#/components/schemas/"


def install_turn_event_schema(app: FastAPI) -> None:
    """Publish ``TurnEvent`` (the SSE payload, no response model FastAPI could see) and its
    nested models under components/schemas, so the stream's ``$ref`` resolves for client
    generators. Components FastAPI already generated are kept as they are."""
    generate = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        doc = generate()
        schema = TurnEvent.model_json_schema(ref_template=COMPONENTS + "{model}")
        nested = schema.pop("$defs", {})
        components = doc.setdefault("components", {}).setdefault("schemas", {})
        for name, body in {**nested, "TurnEvent": schema}.items():
            components.setdefault(name, body)
        return doc

    app.openapi = openapi  # type: ignore[method-assign]


Viewer = Annotated[WorkspaceScope, Depends(require(Permission.VIEW_DATASET))]
Analyst = Annotated[WorkspaceScope, Depends(require(Permission.RUN_ANALYSIS))]


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_version_id: Annotated[str, Field(pattern=r"^dsv_\w+$")]
    semantic_model_version: Annotated[int, Field(ge=1)]
    title: Annotated[str, Field(min_length=1, max_length=200)] = "New conversation"


class MessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: Annotated[str, Field(min_length=1, max_length=2000)]


def _service(container: ContainerDep) -> ChatService:
    return ChatService(container.uow, container.ids, container.clock)


Service = Annotated[ChatService, Depends(_service)]


@router.post("/conversations", status_code=201)
def create_conversation(body: ConversationCreate, scope: Analyst, service: Service) -> Conversation:
    return service.create(scope, body.dataset_version_id, body.semantic_model_version, body.title)


@router.get("/conversations")
def list_conversations(
    scope: Viewer, principal: PrincipalDep, service: Service
) -> list[Conversation]:
    return service.conversations(scope, principal)


@router.get("/conversations/{conversation_id}")
def get_conversation(
    conversation_id: str, scope: Viewer, principal: PrincipalDep, service: Service
) -> Conversation:
    return service.get(scope, principal, conversation_id)


@router.get("/conversations/{conversation_id}/messages")
def list_messages(
    conversation_id: str, scope: Viewer, principal: PrincipalDep, service: Service
) -> list[ChatMessage]:
    return service.messages(scope, service.get(scope, principal, conversation_id))


@router.post(
    "/conversations/{conversation_id}/messages",
    response_class=EventSourceResponse,
    responses={
        200: {
            "description": "TurnEvent stream (Server-Sent Events)",
            "content": {"text/event-stream": {"schema": {"$ref": f"{COMPONENTS}TurnEvent"}}},
        }
    },
)
async def post_message(
    conversation_id: str,
    body: MessageCreate,
    scope: Analyst,
    principal: PrincipalDep,
    service: Service,
    container: ContainerDep,
) -> EventSourceResponse:
    trace_id = trace_id_var.get() or container.ids.new("trace")
    conversation = await asyncio.to_thread(service.get, scope, principal, conversation_id)
    request = await asyncio.to_thread(
        service.start_turn, scope, principal, conversation, body.content, trace_id
    )
    orchestrator = build_orchestrator(container)

    async def events() -> AsyncIterator[ServerSentEvent]:
        seq = 0
        async for event in orchestrator.handle_turn(request):
            data = TurnEvent(seq=seq, event=event).model_dump_json()
            yield ServerSentEvent(data=data, event=event.type, id=str(seq))
            seq += 1

    return EventSourceResponse(events(), ping=SSE_PING_S)
