"""Evidence v0 and Claim contracts (TASK-EVID-001; TRD 25.1, design 39).

Evidence v0 is the TRD 25.1 object minus two fields:
- ``claim_ids``: evidence is written before any claim exists and is immutable, so the link is
  kept on ``Claim.evidence_ids`` only (design 39: Claim and Evidence are separate).
- ``validation``: validation happens after evidence is written; ``ValidationResult`` references
  the subject instead. Both links are available as read models.
``confidence`` is optional in v0 because it depends on the validator outcome (TRD 25.1).
Evidence and Claim are frozen (append-only, never updated).
"""

from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

from app.data.domain import Ratio, Sha256

EvidenceId = Annotated[str, Field(pattern=r"^ev_\w+$")]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class QueryInfo(Contract):
    engine: str  # "<engine>@<version>"
    sql: str  # redacted for viewers without VIEW_QUERY (design 39)
    params: dict[str, JsonValue] = {}


class EvidenceResult(Contract):
    value: float | int | None = None
    unit: str | None = None  # "pct", "pp", "currency:INR", "count"
    rows: Annotated[int, Field(ge=0)] | None = None
    result_hash: Sha256


class ModelRef(Contract):
    model_run_id: str | None = None
    model_id: str | None = None
    model_version: str | None = None


class LlmRef(Contract):
    model_version: str
    prompt_id: str
    prompt_version: str


class Confidence(Contract):
    level: Literal["low", "medium", "high"]
    score: Ratio  # ordinal indicator with a documented formula, not a probability
    basis: list[str]


class Evidence(Contract):
    schema_version: Literal["1.0"] = "1.0"
    evidence_id: EvidenceId
    workspace_id: Annotated[str, Field(pattern=r"^ws_\w+$")]
    kind: Literal["tool_result", "citation", "simulation"]  # the three EvidenceRecorder callers
    dataset_id: Annotated[str, Field(pattern=r"^ds_\w+$")] | None
    dataset_version_id: Annotated[str, Field(pattern=r"^dsv_\w+$")] | None
    dataset_content_hash: Sha256 | None
    semantic_model_version: Annotated[int, Field(ge=1)] | None
    operation: str  # tool name, e.g. "analytics.metric_query"
    tool_version: str
    arguments: dict[str, JsonValue]
    query: QueryInfo | None = None
    calculation: str | None = None
    result: EvidenceResult
    model: ModelRef | None = None
    llm: LlmRef | None = None
    confidence: Confidence | None = None
    created_at: AwareDatetime
    software_version: str  # "0.1.0+<git-sha>"

    @model_validator(mode="after")
    def _dataset_refs(self) -> Self:
        refs = (self.dataset_id, self.dataset_version_id, self.dataset_content_hash)
        if any(r is None for r in refs) and any(r is not None for r in refs):
            raise ValueError("dataset_id, dataset_version_id, dataset_content_hash go together")
        if self.kind == "tool_result" and self.dataset_version_id is None:
            raise ValueError("tool_result evidence must reference a dataset version")
        return self


class Claim(Contract):
    schema_version: Literal["1.0"] = "1.0"
    claim_id: Annotated[str, Field(pattern=r"^cl_\w+$")]
    workspace_id: Annotated[str, Field(pattern=r"^ws_\w+$")]
    kind: Literal["answer_sentence", "widget"]
    text: str | None = None  # the sentence, for answer_sentence
    widget_id: str | None = None  # for widget
    evidence_ids: Annotated[list[EvidenceId], Field(min_length=1)]
    validation_id: Annotated[str, Field(pattern=r"^va_\w+$")] | None = None
    created_at: AwareDatetime

    @model_validator(mode="after")
    def _subject(self) -> Self:
        if self.kind == "answer_sentence" and not (self.text and self.widget_id is None):
            raise ValueError("answer_sentence claims need text and no widget_id")
        if self.kind == "widget" and not (self.widget_id and self.text is None):
            raise ValueError("widget claims need widget_id and no text")
        return self
