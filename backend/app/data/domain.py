"""Dataset and ingestion contracts (TASK-DATA-001; design.md sections 8-10, TRD 10.3, ADR-0023).

Deviations from the design sketches:
- Every hash is written as ``sha256:<64 lowercase hex>`` (the TRD 25.1 evidence form), including
  ``source_sha256`` and ``content_hash``, so one format is used everywhere.
- ``ParsedBatch.rows`` is a row-major list of JSON scalars. The design leaves the concrete
  columnar type to RES-014/ADR-0004 (still PROPOSED); switching to an Arrow batch is a contract
  change that needs ARCH review. ``ParsedBatch`` is in-process only and is not exported.
- ``DUPLICATE_OF`` (design 10 pseudocode) is not a state in the section 9 diagram; dedupe returns
  the existing version instead of adding a state.
"""

from collections.abc import Iterator, Mapping
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Protocol, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Sha256 = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Ratio = Annotated[float, Field(ge=0.0, le=1.0)]
Cell = str | int | float | bool | None


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DatasetState(StrEnum):
    UPLOADED = "UPLOADED"
    VALIDATING = "VALIDATING"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    INGESTING = "INGESTING"
    INGESTION_FAILED = "INGESTION_FAILED"
    INGESTED = "INGESTED"
    PROFILED = "PROFILED"
    SCHEMA_ANALYZED = "SCHEMA_ANALYZED"
    DOMAIN_IDENTIFIED = "DOMAIN_IDENTIFIED"
    SEMANTICALLY_MAPPED = "SEMANTICALLY_MAPPED"
    ANALYZING = "ANALYZING"
    READY = "READY"
    ANALYSIS_FAILED = "ANALYSIS_FAILED"


_S = DatasetState
# design.md section 9 state diagram, plus UPLOADED -> INGESTION_FAILED and INGESTED ->
# ANALYSIS_FAILED (GAPS-5a): a job that dies after its last retry must not leave the version in
# a state with no way out (app.data.jobs.fail_dead_job_version).
TRANSITIONS: Mapping[DatasetState, frozenset[DatasetState]] = {
    _S.UPLOADED: frozenset({_S.VALIDATING, _S.INGESTION_FAILED}),
    _S.VALIDATING: frozenset({_S.VALIDATION_FAILED, _S.INGESTING}),
    _S.INGESTING: frozenset({_S.INGESTION_FAILED, _S.INGESTED}),
    _S.INGESTED: frozenset({_S.PROFILED, _S.ANALYSIS_FAILED}),
    _S.PROFILED: frozenset({_S.SCHEMA_ANALYZED, _S.ANALYSIS_FAILED}),
    _S.SCHEMA_ANALYZED: frozenset({_S.DOMAIN_IDENTIFIED, _S.ANALYSIS_FAILED}),
    _S.DOMAIN_IDENTIFIED: frozenset({_S.SEMANTICALLY_MAPPED, _S.ANALYSIS_FAILED}),
    _S.SEMANTICALLY_MAPPED: frozenset({_S.ANALYZING, _S.ANALYSIS_FAILED}),
    _S.ANALYZING: frozenset({_S.READY, _S.ANALYSIS_FAILED}),
    _S.READY: frozenset({_S.SEMANTICALLY_MAPPED}),
    _S.ANALYSIS_FAILED: frozenset({_S.INGESTED}),
    _S.VALIDATION_FAILED: frozenset(),
    _S.INGESTION_FAILED: frozenset(),
}
FAILED_STATES = frozenset({_S.VALIDATION_FAILED, _S.INGESTION_FAILED, _S.ANALYSIS_FAILED})
# States in which the stored columnar file exists (content never changes after INGESTED).
_STORED_STATES = frozenset(TRANSITIONS) - {
    _S.UPLOADED,
    _S.VALIDATING,
    _S.VALIDATION_FAILED,
    _S.INGESTING,
    _S.INGESTION_FAILED,
}


def can_transition(current: DatasetState, target: DatasetState) -> bool:
    return target in TRANSITIONS[current]


class Dataset(Contract):
    schema_version: Literal["1.0"] = "1.0"
    id: Annotated[str, Field(pattern=r"^ds_\w+$")]
    workspace_id: Annotated[str, Field(pattern=r"^ws_\w+$")]
    name: Annotated[str, Field(min_length=1, max_length=255)]
    source_type: Literal["file_upload", "postgres_snapshot"]
    created_at: AwareDatetime


class DatasetFailure(Contract):
    code: str
    message: str


class DatasetVersion(Contract):
    """Immutable content snapshot; only ``state`` (and its bookkeeping) advances."""

    schema_version: Literal["1.0"] = "1.0"
    id: Annotated[str, Field(pattern=r"^dsv_\w+$")]
    dataset_id: Annotated[str, Field(pattern=r"^ds_\w+$")]
    workspace_id: Annotated[str, Field(pattern=r"^ws_\w+$")]
    version_no: Annotated[int, Field(ge=1)]
    parent_id: Annotated[str, Field(pattern=r"^dsv_\w+$")] | None = None
    source_sha256: Sha256  # raw uploaded bytes: idempotency / dedupe key (ADR-0023)
    content_hash: Sha256 | None = None  # stored Parquet bytes: integrity / evidence (ADR-0023)
    storage_uri: str | None = None
    row_count: Annotated[int, Field(ge=0)] | None = None
    state: DatasetState
    state_changed_at: AwareDatetime
    failure: DatasetFailure | None = None
    review_flags: list[str] = []  # e.g. DOMAIN_UNCERTAIN, MAPPING_AMBIGUOUS:net_amt
    created_at: AwareDatetime

    @model_validator(mode="after")
    def _state_consistency(self) -> Self:
        if (self.failure is not None) != (self.state in FAILED_STATES):
            raise ValueError("failure must be set exactly when state is a *_FAILED state")
        stored = (self.content_hash, self.storage_uri, self.row_count)
        if self.state in _STORED_STATES and None in stored:
            raise ValueError(f"state {self.state} requires content_hash, storage_uri, row_count")
        return self


class FileFacts(Contract):
    schema_version: Literal["1.0"] = "1.0"
    path: Path
    size_bytes: Annotated[int, Field(ge=0)]
    detected_type: Literal["csv", "xlsx", "json", "jsonl", "pdf", "docx", "txt", "unknown"]
    encoding: str | None
    bom: bool
    container: dict[str, int] | None  # e.g. {"entries": 12, "uncompressed_bytes": ...}


class ParseLimits(Contract):
    max_rows: Annotated[int, Field(ge=1)]
    max_reject_ratio: Ratio
    max_field_chars: Annotated[int, Field(ge=1)]


class RejectedRow(Contract):
    row_number: Annotated[int, Field(ge=1)]
    reason: str  # never the raw row content (may hold PII)


class ParsedBatch(Contract):
    columns: list[str]
    rows: list[list[Cell]]
    rejected_rows: list[RejectedRow]  # capped sample
    total_rows_seen: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def _shape(self) -> Self:
        width = len(self.columns)
        if any(len(row) != width for row in self.rows):
            raise ValueError("every row must have one value per column")
        if self.total_rows_seen < len(self.rows):
            raise ValueError("total_rows_seen cannot be below the number of parsed rows")
        return self


class DataParser(Protocol):
    name: str
    priority: int

    def can_parse(self, facts: FileFacts) -> bool: ...

    def iter_batches(self, facts: FileFacts, limits: ParseLimits) -> Iterator[ParsedBatch]: ...
