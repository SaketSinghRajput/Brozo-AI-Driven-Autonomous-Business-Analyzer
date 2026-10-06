"""Job contracts (design §53)."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    RETRYING = "RETRYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED})


class JobType(StrEnum):
    INGEST = "ingest"
    PROCESS_DATASET = "process_dataset"
    RUN_PLAN = "run_plan"
    ML_TRAIN = "ml_train"
    EMBED_DOCS = "embed_docs"
    EVALUATE = "evaluate"
    REPAIR_GATES = "repair_gates"
    FINE_TUNE = "fine_tune"
    AUDIT_VERIFY = "audit_verify"  # TASK-SEC-012: hash-chain verifier (app.security.audit)


class JobError(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    message: str
    retryable: bool


class Job(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    workspace_id: str
    type: JobType
    payload: dict[str, Any]
    status: JobStatus
    attempts: int
    max_attempts: int
    run_after: datetime
    idempotency_key: str | None
    cancel_requested: bool
    progress: float
    result_ref: str | None
    error: JobError | None
    trace_id: str | None
    created_at: datetime
    updated_at: datetime
