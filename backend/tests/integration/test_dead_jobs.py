"""GAPS-5a: a version whose job exhausts its retries on a non-application error (a crash) must not
stay in an in-progress state forever; it moves to the matching *_FAILED state with JOB_FAILED."""

import pytest

from app.jobs.models import Job, JobType
from app.jobs.worker import JobContext

from .dataset_env import FIXTURES, Env


def _crash(job: Job, ctx: JobContext) -> str | None:
    raise RuntimeError("boom")  # not an ApplicationError: retried until max_attempts


@pytest.mark.parametrize(
    ("job_type", "expected"),
    [
        (JobType.INGEST, {"VALIDATION_FAILED", "INGESTION_FAILED"}),  # VALIDATING / INGESTING
        (JobType.PROCESS_DATASET, {"ANALYSIS_FAILED"}),  # INGESTED / understanding states
        (JobType.RUN_PLAN, {"ANALYSIS_FAILED"}),  # SEMANTICALLY_MAPPED / ANALYZING
    ],
)
def test_exhausted_retries_fail_the_version(
    env: Env, job_type: JobType, expected: set[str]
) -> None:
    env.worker._handlers[job_type] = _crash
    r = env.upload(FIXTURES / "retail" / "orders.csv")
    assert r.status_code == 201, r.text
    ids = r.json()
    env.settle()
    version = env.version(ids)
    assert version["state"] in expected, version
    assert version["failure"]["code"] == "JOB_FAILED"
    assert "boom" not in version["failure"]["message"]  # internals are not shown
