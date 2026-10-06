"""ToolRegistry (TASK-TOOL-003, design §19)."""

from collections.abc import Iterable

from app.core.errors import ToolError
from app.core.ids import IdGenerator
from app.dashboard.viz_tool import VizSpecTool
from app.evidence.tool import EvidenceGetTool
from app.infra.db import UnitOfWork
from app.infra.storage import Storage
from app.ml.anomaly_tool import AnomalyTool
from app.ml.forecast_tool import ForecastTool
from app.ml.segments_tool import SegmentsTool
from app.tools.domain import Tool, ToolPermission, ToolSpecForLLM


def default_tools(
    uow: UnitOfWork, storage: Storage | None = None, ids: IdGenerator | None = None
) -> list[Tool]:
    """Tools every deployment registers; feature streams append theirs here.

    The ML tools read stored dataset versions, so they are registered only when a storage is
    given (the composition root passes its storage and id generator).
    """
    tools: list[Tool] = [EvidenceGetTool(uow), VizSpecTool(uow)]
    if storage is not None:
        tools += [
            ForecastTool(uow, storage, ids),
            AnomalyTool(uow, storage, ids),
            SegmentsTool(uow, storage, ids),
        ]
    return tools


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.meta.name in self._tools:
            raise ValueError(f"tool {tool.meta.name!r} is already registered")
        self._tools[tool.meta.name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise ToolError(f"unknown tool {name!r}") from None

    def specs_for(self, permissions: Iterable[ToolPermission]) -> list[ToolSpecForLLM]:
        """What the LLM may see: only tools inside the effective permission set."""
        allowed = frozenset(permissions)
        return [
            ToolSpecForLLM(
                name=tool.meta.name,
                description=tool.meta.description,
                input_schema=tool.input_model.model_json_schema(),
            )
            for name, tool in sorted(self._tools.items())
            if tool.meta.permission in allowed
        ]
