"""Composition root: builds adapters from config; services receive them via constructors.

No service locator: code outside `create_app` and tests must not read `app.state.container`.
Building the container opens no connections (the engine connects lazily; the LLM gateway's
HTTP client connects on its first request).
"""

from dataclasses import dataclass

from sqlalchemy import Engine

from app.analytics.source import DatasetSource
from app.analytics.sql.engine import AnalyticalEngine, QueryLimits
from app.analytics.tools import analytics_tools
from app.core.clock import Clock, SystemClock
from app.core.config import AppConfig
from app.core.ids import IdGenerator, UuidV7Ids
from app.evidence.recorder import EvidenceRecorder
from app.infra.db import UnitOfWork, make_engine
from app.infra.storage import LocalStorage, Storage
from app.llm.gateway import LLMGateway, gateway_from_config
from app.llm.prompts import PromptRegistry
from app.security.auth import AuthService
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry, default_tools


@dataclass(frozen=True)
class Container:
    config: AppConfig
    clock: Clock
    ids: IdGenerator
    engine: Engine
    uow: UnitOfWork
    storage: Storage
    auth: AuthService
    tools: ToolRegistry
    tool_executor: ToolExecutor
    prompts: PromptRegistry
    llm: LLMGateway


def build_container(
    config: AppConfig,
    *,
    clock: Clock | None = None,
    ids: IdGenerator | None = None,
    storage: Storage | None = None,
) -> Container:
    engine = make_engine(config.database)
    clock = clock or SystemClock()
    ids = ids or UuidV7Ids()
    uow = UnitOfWork(engine)
    storage = storage or LocalStorage(config.storage.datastore_path)
    limits = QueryLimits(**config.sql.model_dump())  # design §65 sql.* (TASK-MLOPS-008)
    prompts = PromptRegistry()
    registry = ToolRegistry(
        [
            *default_tools(uow, storage, ids),
            *analytics_tools(DatasetSource(uow, storage), AnalyticalEngine(limits)),
        ]
    )
    return Container(
        config=config,
        clock=clock,
        ids=ids,
        engine=engine,
        uow=uow,
        storage=storage,
        auth=AuthService(uow, clock, ids, config.security),
        tools=registry,
        tool_executor=ToolExecutor(registry, uow, EvidenceRecorder(clock), ids),
        prompts=prompts,
        llm=gateway_from_config(config.llm, prompts),
    )
