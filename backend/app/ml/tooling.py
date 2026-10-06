"""Shared plumbing of the ML tools (``ml.forecast``, ``ml.anomalies``, ``ml.segments``).

A tool call: take the dataset version from the ToolContext (the same one the EvidenceRecorder
records), read its stored Parquet read-only, run the task in a worker thread, persist the
``ModelRun`` and return the typed output. Evidence is written afterwards by the ToolExecutor
(tools never write evidence, design 19); the output carries ``model_run_id``, the model name and
library version, so the evidence result hash covers them.
"""

import asyncio
from typing import Any

import polars as pl
from pydantic import BaseModel, JsonValue

from app.core.clock import Clock, SystemClock
from app.core.errors import ToolError
from app.core.ids import IdGenerator, UuidV7Ids
from app.core.scope import WorkspaceScope
from app.infra.db import UnitOfWork
from app.infra.storage import Storage
from app.ml.config import ML_CONFIG, MLConfig
from app.ml.domain import Metrics, MLTask, ModelInfo, ModelRun, Selection
from app.ml.repository import ModelRunRepository, VersionReader
from app.planner.domain import Unavailability
from app.tools.domain import ToolContext


def calculation(run: ModelRun, model: ModelInfo, what: str) -> str:
    """Evidence ``calculation`` text: the model, its library version and the run record."""
    return f"{what} with {model.name} ({model.library}), seed {run.seed}; model run {run.id}"


class MLTool:
    def __init__(
        self,
        uow: UnitOfWork,
        storage: Storage,
        ids: IdGenerator | None = None,
        clock: Clock | None = None,
        config: MLConfig = ML_CONFIG,
    ) -> None:
        self._uow = uow
        self._reader = VersionReader(uow, storage)
        self._ids = ids or UuidV7Ids()
        self._clock = clock or SystemClock()
        self._cfg = config

    async def __call__(self, args: BaseModel, ctx: ToolContext) -> BaseModel | Unavailability:
        if ctx.dataset_version_id is None:
            raise ToolError("ML tools need a dataset version in the tool context")
        scope = WorkspaceScope(workspace_id=ctx.workspace_id, principal_id=ctx.principal_id)
        return await asyncio.to_thread(self._sync, args, scope, ctx.dataset_version_id)

    def _sync(
        self, args: BaseModel, scope: WorkspaceScope, version_id: str
    ) -> BaseModel | Unavailability:
        return self.run(args, self._reader.read(scope, version_id), scope, version_id)

    def run(
        self, args: Any, frame: pl.DataFrame, scope: WorkspaceScope, version_id: str
    ) -> BaseModel | Unavailability:
        raise NotImplementedError

    def seed(self, requested: int | None) -> int:
        return self._cfg.seed if requested is None else requested

    def persist(
        self,
        scope: WorkspaceScope,
        version_id: str,
        task: MLTask,
        model: ModelInfo,
        args: BaseModel,
        metrics: Metrics,
        seed: int,
        selection: Selection | None = None,
        extra: dict[str, JsonValue] | None = None,
    ) -> ModelRun:
        params: dict[str, JsonValue] = {
            "args": args.model_dump(mode="json"),
            "model": model.model_dump(mode="json"),
            "dataset_version_id": version_id,
            "config_version": self._cfg.version,
            "catalog_version": self._cfg.catalog_version,
        }
        params.update(extra or {})
        run = ModelRun(
            id=self._ids.new("mr"),
            workspace_id=scope.workspace_id,
            task=task,
            params=params,
            selection=selection,
            metrics=metrics,
            seed=seed,
            created_at=self._clock.now(),
        )
        with self._uow(scope) as s:
            ModelRunRepository(s).insert(scope, run, version_id, model.name, model.library)
        return run
