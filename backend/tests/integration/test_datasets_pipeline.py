"""TASK-DATA-009..012, PROFILE-009, INTEGRATION: upload → ingest job → profile job, on PostgreSQL.

The API and the worker both run as the least-privilege `analyzer_app` role, so missing grants
(e.g. UPDATE on dataset_versions, INSERT on dataset_profiles) fail here.
"""

from pathlib import Path

import pytest
from sqlalchemy import text

from app.api.deps import current_principal
from app.core.errors import StateConflictError
from app.data.domain import DatasetState
from app.data.jobs import data_job_handlers
from app.data.repository import DatasetRepository
from app.jobs.models import Job, JobStatus, JobType
from app.jobs.worker import JobContext
from tests.principals import principal_for
from tests.unit.data.conftest import generator_module

from .dataset_env import FIXTURES, Env
from .dataset_env import new_workspace as _new_workspace


def test_upload_ingest_profile_end_to_end(env: Env) -> None:
    r = env.upload(FIXTURES / "retail" / "orders.csv", name="Orders")
    assert r.status_code == 201, r.text
    ids = r.json()
    assert ids["status"] == "UPLOADED"
    assert env.drain() == 2  # ingest, then the chained process_dataset job
    assert env.job(ids["job_id"])["status"] == "COMPLETED"

    version = env.version(ids)
    assert version["state"] == "PROFILED"
    assert version["row_count"] == 10202
    assert version["content_hash"].startswith("sha256:")
    assert version["source_sha256"] != version["content_hash"]

    profile = env.get(f"/{ids['dataset_id']}/versions/{ids['version_id']}/profile").json()
    assert profile["row_count"] == 10202
    columns = {c["name"]: c for c in profile["columns"]}
    assert columns["order_date"]["temporal"]["granularity"] == "day"
    assert columns["customer_id"]["categorical"]["masked"] is True
    assert env.get("").json()[0]["name"] == "Orders"

    with env.container.uow(env.scope) as s:
        events = s.execute(
            text("SELECT event_type FROM outbox_events WHERE payload->>'version_id' = :v"),
            {"v": ids["version_id"]},
        ).scalars()
        assert set(events) == {"DatasetUploaded", "DatasetIngested", "DatasetProfiled"}


def test_identical_bytes_are_deduplicated(env: Env) -> None:
    path = FIXTURES / "retail" / "products.csv"
    first = env.upload(path).json()
    again = env.upload(path, key="another-key")
    assert again.status_code == 200
    assert again.json()["version_id"] == first["version_id"]
    assert again.json()["job_id"] == first["job_id"]
    assert len(env.get("").json()) == 1
    assert env.drain() == 2


def test_idempotency_key_replay_returns_original_ids(env: Env) -> None:
    first = env.upload(FIXTURES / "retail" / "customers.csv", key="k-1").json()
    replay = env.upload(FIXTURES / "saas" / "subscriptions.csv", key="k-1")
    assert replay.status_code == 200
    assert replay.json()["version_id"] == first["version_id"]
    assert len(env.get("").json()) == 1


def test_new_version_of_existing_dataset(env: Env) -> None:
    first = env.upload(FIXTURES / "insufficient" / "sales.csv").json()
    second = env.upload(FIXTURES / "ambiguous" / "data.csv", dataset_id=first["dataset_id"])
    assert second.status_code == 201
    versions = env.get(f"/{first['dataset_id']}/versions").json()
    assert [v["version_no"] for v in versions] == [1, 2]
    assert versions[1]["parent_id"] == first["version_id"]


def test_macro_upload_fails_validation_with_reason(env: Env) -> None:
    ids = env.upload(FIXTURES / "hostile" / "macro_enabled.xlsm").json()
    env.drain()
    job = env.job(ids["job_id"])
    assert job["status"] == "FAILED"
    assert job["error"]["code"] == "SECURITY_ERROR"
    assert job["error"]["message"].startswith("[MACRO_ENABLED]")
    version = env.version(ids)
    assert (version["state"], version["failure"]["code"]) == ("VALIDATION_FAILED", "MACRO_ENABLED")
    assert version["content_hash"] is None


def test_zip_bomb_upload_fails_validation(env: Env, tmp_path: Path) -> None:
    bomb = tmp_path / "bomb.xlsx"
    generator_module().make_zip_bomb(bomb, decompressed_mb=20)
    ids = env.upload(bomb).json()
    env.drain()
    version = env.version(ids)
    assert (version["state"], version["failure"]["code"]) == ("VALIDATION_FAILED", "ZIP_BOMB")


def test_messy_files_are_ingested_with_flags_and_quarantine(env: Env) -> None:
    ids = env.upload(FIXTURES / "messy" / "malformed_rows.csv").json()
    env.drain()
    version = env.version(ids)
    assert version["state"] == "PROFILED"
    assert "MALFORMED_ROW_FIELD_COUNT" in version["review_flags"]
    base = f"/{ids['dataset_id']}/versions/{ids['version_id']}"
    quarantine = env.get(f"{base}/quarantine").json()
    assert (quarantine["rows_seen"], quarantine["rows_rejected"]) == (30, 4)
    assert [r["row_number"] for r in quarantine["rejected_sample"]] == [6, 13, 21, 26]

    dup = env.upload(FIXTURES / "messy" / "duplicate_headers_mixed_types.csv").json()
    env.drain()
    flags = set(env.version(dup)["review_flags"])
    assert {"DUPLICATE_COLUMN_NAMES:amount", "DUPLICATE_ROWS", "MIXED_DATE_FORMATS:date"} <= flags


def test_other_workspaces_cannot_see_the_dataset(env: Env) -> None:
    ids = env.upload(FIXTURES / "agriculture" / "production.csv").json()
    env.drain()
    other = _new_workspace(env.container.uow)
    env.client.app.dependency_overrides[current_principal] = lambda: principal_for(other)  # type: ignore[attr-defined]
    base = f"/{ids['dataset_id']}"
    for path in ("", "/versions", f"/versions/{ids['version_id']}/profile"):
        assert env.get(base + path).status_code == 404
    assert env.get("").json() == []


def test_state_transitions_are_compare_and_set(env: Env) -> None:
    ids = env.upload(FIXTURES / "saas" / "marketing_spend.csv").json()
    scope, uow = env.scope, env.container.uow
    with uow(scope) as s:
        DatasetRepository(s).transition(
            scope, ids["version_id"], DatasetState.UPLOADED, DatasetState.VALIDATING
        )
    with pytest.raises(StateConflictError), uow(scope) as s:  # the racing duplicate loses
        DatasetRepository(s).transition(
            scope, ids["version_id"], DatasetState.UPLOADED, DatasetState.VALIDATING
        )
    with pytest.raises(StateConflictError, match="illegal"), uow(scope) as s:
        DatasetRepository(s).transition(
            scope, ids["version_id"], DatasetState.VALIDATING, DatasetState.READY
        )
    env.drain()  # the handler resumes from VALIDATING
    assert env.version(ids)["state"] == "PROFILED"


def test_handlers_are_idempotent_on_rerun(env: Env) -> None:
    ids = env.upload(FIXTURES / "retail" / "products.csv").json()
    env.settle()  # ingest -> profile -> understanding -> run_plan (READY)
    job = env.job(ids["job_id"])
    derived = (
        "SELECT (SELECT count(*) FROM dataset_profiles), (SELECT count(*) FROM understandings), "
    )
    derived += (
        "(SELECT count(*) FROM domain_classifications), (SELECT count(*) FROM semantic_models)"
    )
    with env.container.uow(env.scope) as s:
        before = DatasetRepository(s).get_version(env.scope, ids["version_id"])
        rows = tuple(s.execute(text(derived)).one())
    handlers = data_job_handlers(env.container)
    for job_type in (JobType.INGEST, JobType.PROCESS_DATASET):
        rerun = Job.model_validate({**job, "type": job_type, "status": JobStatus.RUNNING})
        handlers[job_type](rerun, JobContext(rerun))
    with env.container.uow(env.scope) as s:
        after = DatasetRepository(s).get_version(env.scope, ids["version_id"])
        assert tuple(s.execute(text(derived)).one()) == rows
    assert before is not None
    assert before.state is DatasetState.READY
    assert after == before
