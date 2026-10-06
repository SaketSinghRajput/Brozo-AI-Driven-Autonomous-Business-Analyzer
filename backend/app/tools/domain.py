"""Tool registry contracts (TASK-TOOL-001; design 19-20).

Deviations:
- ``ToolResult.output`` is the tool output model dumped to a JSON object (``dict``) rather than a
  live ``BaseModel``, so the result is serialisable and exportable. ``ToolExecutor`` validates it
  against ``Tool.output_model`` before building the result.
- ``ToolContext`` holds the fields design 19 dereferences (principal, workspace, effective
  permissions, trace). The turn budget object is added with ``BudgetGuard`` (not specified yet).
"""

from enum import StrEnum
from typing import Annotated, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from app.planner.domain import Unavailability

ToolName = Annotated[str, Field(pattern=r"^[a-z]+(\.[a-z_]+)+$")]  # "analytics.metric_query"
SemVer = Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolPermission(StrEnum):
    SQL_READ = "SQL_READ"
    PYTHON_ANALYTICS = "PYTHON_ANALYTICS"
    MODEL_RUN = "MODEL_RUN"
    MODEL_TRAIN = "MODEL_TRAIN"
    DOCUMENT_SEARCH = "DOCUMENT_SEARCH"
    MEMORY_READ = "MEMORY_READ"
    MEMORY_WRITE = "MEMORY_WRITE"
    SIMULATION = "SIMULATION"
    CODE_EXECUTION = "CODE_EXECUTION"
    PATCH_PROPOSE = "PATCH_PROPOSE"


class ToolMetadata(Contract):
    schema_version: Literal["1.0"] = "1.0"
    name: ToolName
    version: SemVer
    description: str
    permission: ToolPermission
    side_effects: Literal["none", "writes_memory", "creates_artifact", "proposes_patch"]
    timeout_s: Annotated[float, Field(gt=0)]
    max_calls_per_turn: Annotated[int, Field(ge=1)]
    parallel_safe: bool
    produces_evidence: bool = True
    runs_in: Literal["api", "worker"]


class ErrorInfo(Contract):
    code: str  # TRD 35 error code, e.g. TOOL_ERROR
    message: str  # user-safe; no stack traces, SQL or paths
    retryable: bool


class ToolResult(Contract):
    schema_version: Literal["1.0"] = "1.0"
    tool: ToolName
    tool_version: SemVer
    status: Literal["ok", "unavailable", "error"]
    output: dict[str, JsonValue] | None = None
    unavailable: Unavailability | None = None
    error: ErrorInfo | None = None
    evidence_id: Annotated[str, Field(pattern=r"^ev_\w+$")] | None = None
    duration_ms: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def _one_payload(self) -> Self:
        payload = {"ok": self.output, "unavailable": self.unavailable, "error": self.error}
        present = [k for k, v in payload.items() if v is not None]
        if present != [self.status]:
            raise ValueError(f"status {self.status!r} must carry exactly its own payload")
        if self.status != "ok" and self.evidence_id is not None:
            raise ValueError("only ok results carry an evidence_id")
        return self


class ToolSpecForLLM(Contract):
    """What the LLM sees: name, description and the JSON Schema of the input model."""

    schema_version: Literal["1.0"] = "1.0"
    name: ToolName
    description: str
    input_schema: dict[str, JsonValue]


class ToolContext(Contract):
    workspace_id: Annotated[str, Field(pattern=r"^ws_\w+$")]
    principal_id: str
    trace_id: str
    permissions: frozenset[ToolPermission]  # effective = role & turn profile (design 20)
    dataset_version_id: Annotated[str, Field(pattern=r"^dsv_\w+$")] | None = None
    semantic_model_version: Annotated[int, Field(ge=1)] | None = None


class Tool(Protocol):
    meta: ToolMetadata
    input_model: type[BaseModel]
    output_model: type[BaseModel]

    async def __call__(self, args: BaseModel, ctx: ToolContext) -> BaseModel | Unavailability: ...
