"""Chat use cases (TASK-CHAT-001) and the orchestrator composition for one app.

``ChatRecorder`` implements the orchestrator's ``TurnRecorder`` port: the answer message and its
claims (via ``ClaimRepository``, with the answer's validation id) commit in one transaction.
"""

import asyncio

from app.chat.repository import ChatMessage, Conversation, ConversationRepository
from app.core.clock import Clock
from app.core.container import Container
from app.core.errors import NotFoundError
from app.core.ids import IdGenerator
from app.core.scope import WorkspaceScope
from app.data.profiling.domain import ProfileResult
from app.data.profiling.profiler import ENGINE_VERSION
from app.data.repository import DatasetRepository
from app.data.understanding_store import UnderstandingStore
from app.evidence.claims import ClaimRepository
from app.evidence.domain import Claim
from app.infra.db import UnitOfWork
from app.llm.context import ContextBuilder
from app.orchestrator.composer import ResponseComposer
from app.orchestrator.domain import TurnRecord, TurnRequest
from app.orchestrator.intent import IntentResolver
from app.orchestrator.orchestrator import Orchestrator
from app.orchestrator.planner import QuestionPlanner
from app.orchestrator.tool_loop import ToolLoop
from app.planner.facts import build_facts
from app.security.auth import Principal
from app.tools.permissions import Role
from app.validator.repository import ValidationRepository
from app.validator.service import ValidationService


def owner_filter(principal: Principal) -> str | None:
    """Admins see every conversation of their workspace; everyone else only their own."""
    return None if principal.role is Role.ADMIN else principal.user_id


class ChatRecorder:
    def __init__(self, uow: UnitOfWork, ids: IdGenerator, clock: Clock) -> None:
        self._uow = uow
        self._ids = ids
        self._clock = clock

    async def record(self, scope: WorkspaceScope, record: TurnRecord) -> None:
        await asyncio.to_thread(self._write, scope, record)

    def _write(self, scope: WorkspaceScope, record: TurnRecord) -> None:
        with self._uow(scope) as s:
            ConversationRepository(s).add_answer(scope, record)
            claims = ClaimRepository(s)
            for draft in record.claims:
                claims.insert(
                    scope,
                    Claim(
                        claim_id=self._ids.new("cl"),
                        workspace_id=scope.workspace_id,
                        kind="answer_sentence",
                        text=draft.text,
                        evidence_ids=draft.evidence_ids,
                        validation_id=record.validation_id,
                        created_at=self._clock.now(),
                    ),
                )


def build_orchestrator(container: Container) -> Orchestrator:
    context = ContextBuilder()
    gateway, prompts = container.llm, container.prompts
    return Orchestrator(
        intents=IntentResolver(gateway, prompts, context),
        planner=QuestionPlanner(container.tools, container.ids),
        tool_loop=ToolLoop(gateway, prompts, context),
        composer=ResponseComposer(gateway, prompts, context),
        validation=ValidationService(container.uow, container.ids),
        executor=container.tool_executor,
        registry=container.tools,
        recorder=ChatRecorder(container.uow, container.ids, container.clock),
        clock=container.clock,
    )


class ChatService:
    def __init__(self, uow: UnitOfWork, ids: IdGenerator, clock: Clock) -> None:
        self._uow = uow
        self._ids = ids
        self._clock = clock

    def create(
        self,
        scope: WorkspaceScope,
        dataset_version_id: str,
        semantic_model_version: int,
        title: str,
    ) -> Conversation:
        conversation = Conversation(
            id=self._ids.new("cv"),
            owner_id=scope.principal_id,
            dataset_version_id=dataset_version_id,
            semantic_model_version=semantic_model_version,
            title=title,
            created_at=self._clock.now(),
        )
        with self._uow(scope) as s:
            model = ValidationRepository(s).semantic_model(
                scope, dataset_version_id, semantic_model_version
            )
            if model is None:
                raise NotFoundError("dataset version or semantic model not found")
            ConversationRepository(s).create(scope, conversation)
        return conversation

    def conversations(self, scope: WorkspaceScope, principal: Principal) -> list[Conversation]:
        with self._uow(scope) as s:
            return ConversationRepository(s).conversations(scope, owner_filter(principal))

    def get(
        self, scope: WorkspaceScope, principal: Principal, conversation_id: str
    ) -> Conversation:
        with self._uow(scope) as s:
            found = ConversationRepository(s).get(scope, conversation_id, owner_filter(principal))
        if found is None:
            raise NotFoundError("conversation not found")
        return found

    def messages(self, scope: WorkspaceScope, conversation: Conversation) -> list[ChatMessage]:
        with self._uow(scope) as s:
            return ConversationRepository(s).messages(scope, conversation.id)

    def start_turn(
        self,
        scope: WorkspaceScope,
        principal: Principal,
        conversation: Conversation,
        question: str,
        trace_id: str,
    ) -> TurnRequest:
        """Stores the question and returns the turn request for the orchestrator."""
        with self._uow(scope) as s:
            model = ValidationRepository(s).semantic_model(
                scope, conversation.dataset_version_id, conversation.semantic_model_version
            )
            if model is None:
                raise NotFoundError("the conversation's semantic model no longer exists")
            version_id = conversation.dataset_version_id
            understanding = UnderstandingStore(s).latest(scope, version_id)
            profile = DatasetRepository(s).get_profile(scope, version_id, ENGINE_VERSION)
            ConversationRepository(s).add_question(
                scope, self._ids.new("msg"), conversation.id, question
            )
        return TurnRequest(
            scope=scope,
            role=principal.role,
            question=question,
            conversation_id=conversation.id,
            message_id=self._ids.new("msg"),
            dataset_version_id=conversation.dataset_version_id,
            semantic_model=model,
            trace_id=trace_id,
            facts=None
            if understanding is None or profile is None
            else build_facts(understanding, model, ProfileResult.model_validate(profile)),
        )
