"""TASK-INTEGRATION-001/002/003, SCHEMA-010, DOMAIN-006/007, SEM-009/010, JOB-004 on PostgreSQL.

upload -> ingest -> profile -> (DatasetProfiled dispatched) -> schema -> domain -> semantic ->
(SemanticModelChanged dispatched) -> run_plan job; plus overrides, re-plan and semantic memory.
Everything runs as `analyzer_app` with RLS on.
"""

from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.jobs.models import JobType
from app.planner.service import PlanningService
from app.tools.permissions import Role
from app.validator.confidence import confidence_level

from .dataset_env import FIXTURES, Env


def _upload(env: Env, path: Path, **form: str) -> dict[str, str]:
    r = env.upload(path, **form)
    assert r.status_code == 201, r.text
    ids: dict[str, str] = r.json()
    env.settle()
    return ids


def _base(ids: dict[str, str]) -> str:
    return f"/{ids['dataset_id']}/versions/{ids['version_id']}"


def _jobs(env: Env, version_id: str, job_type: JobType) -> list[dict[str, Any]]:
    with env.container.uow(env.scope) as s:
        rows = s.execute(
            text("SELECT payload FROM jobs WHERE type = :t AND payload->>'version_id' = :v"),
            {"t": job_type.value, "v": version_id},
        ).scalars()
        return [dict(r) for r in rows]


def test_retail_upload_reaches_ready(env: Env) -> None:
    ids = _upload(env, FIXTURES / "retail" / "orders.csv", name="Orders")
    version = env.version(ids)
    assert version["state"] == "READY", version  # run_plan: SEMANTICALLY_MAPPED -> ... -> READY
    assert "DOMAIN_UNCERTAIN" not in version["review_flags"]

    view = env.get(f"{_base(ids)}/understanding").json()
    assert (view["domain"]["status"], view["domain"]["primary"]) == ("KNOWN", "retail")
    assert view["domain_source"] == "detector"
    assert view["domain_confidence_level"] == confidence_level(view["domain"]["confidence"])
    assert view["domain_confidence_level"] == "high"  # retail fixture: confidence 1.0
    assert {"Order", "Customer"} <= {e["name"] for e in view["understanding"]["entities"]}

    model = env.get(f"{_base(ids)}/semantic-model").json()
    assert model["version"] == 1
    assert "Revenue" in {m["name"] for m in model["metrics"]}
    assert model["entities"][0]["source_table"] == "orders"

    with env.container.uow(env.scope) as s:
        events = s.execute(
            text("SELECT event_type FROM outbox_events WHERE payload->>'version_id' = :v"),
            {"v": ids["version_id"]},
        ).scalars()
        assert set(events) == {
            "DatasetUploaded",
            "DatasetIngested",
            "DatasetProfiled",
            "DatasetSchemaAnalyzed",
            "DatasetDomainIdentified",
            "SemanticModelChanged",
        }
    replan = _jobs(env, ids["version_id"], JobType.RUN_PLAN)
    expected = {"dataset_id": ids["dataset_id"], "version_id": ids["version_id"]}
    assert replan == [{**expected, "semantic_model_version": 1}]

    caps = env.get(f"{_base(ids)}/capabilities").json()
    assert {"trend", "forecast", "customer_segmentation", "rca"} <= set(caps["available"])

    planned = PlanningService(env.container.uow, env.container.tools).default_plan(
        env.scope, ids["dataset_id"], ids["version_id"]
    )
    assert planned.validated.plan.semantic_model_version == 1
    assert planned.validated.plan.steps[0].tool == "analytics.metric_query"


def test_redispatching_an_event_creates_no_second_job(env: Env) -> None:
    ids = _upload(env, FIXTURES / "insufficient" / "sales.csv")
    with env.container.uow(env.scope) as s:
        s.execute(
            text("UPDATE outbox_events SET dispatched_at = NULL WHERE payload->>'version_id' = :v"),
            {"v": ids["version_id"]},
        )
    env.settle()
    assert len(_jobs(env, ids["version_id"], JobType.RUN_PLAN)) == 1
    assert len(_jobs(env, ids["version_id"], JobType.PROCESS_DATASET)) == 2  # profile, understand


def test_insufficient_capabilities_say_what_is_missing(env: Env) -> None:
    ids = _upload(env, FIXTURES / "insufficient" / "sales.csv")
    caps = env.get(f"{_base(ids)}/capabilities").json()
    forecast = [u for u in caps["unavailable"] if u["analysis"] == "forecast"]
    assert forecast[0]["reason_code"] == "INSUFFICIENT_HISTORY"
    assert forecast[0]["message"].startswith(
        "Insufficient data: forecasting needs at least 12 monthly periods"
    )
    codes = {(u["analysis"], u["reason_code"]) for u in caps["unavailable"]}
    assert ("churn_analysis", "NO_RETENTION_HISTORY") in codes
    assert ("customer_segmentation", "NO_CUSTOMER_IDENTIFIER") in codes


def test_ambiguous_data_gets_unknown_domain_and_generic_mapping(env: Env) -> None:
    ids = _upload(env, FIXTURES / "ambiguous" / "data.csv")
    version = env.version(ids)
    assert version["state"] == "READY"
    assert "DOMAIN_UNCERTAIN" in version["review_flags"]
    view = env.get(f"{_base(ids)}/understanding").json()
    assert view["domain"]["status"] in {"UNKNOWN", "AMBIGUOUS"}
    assert view["domain_confidence_level"] == "low"  # confidence 0.0
    model = env.get(f"{_base(ids)}/semantic-model").json()
    assert {m["name"] for m in model["measures"]} == {"Value", "Amount"}  # generic names


def test_semantic_patch_versions_audits_and_replans(env: Env) -> None:
    ids = _upload(env, FIXTURES / "retail" / "orders.csv", name="Orders")
    url = f"/api/v1/datasets{_base(ids)}/semantic-model"
    patch: dict[str, Any] = {
        "base_version": 1,
        "overrides": [
            {"field": "measures.Quantity.default_agg", "value": "avg"},
            {"field": "metrics.Units Total.expr", "value": "sum(measure('Quantity'))"},
        ],
    }
    r = env.client.patch(url, json=patch)
    assert r.status_code == 200, r.text
    model = r.json()
    assert model["version"] == 2
    assert next(m for m in model["measures"] if m["name"] == "Quantity")["default_agg"] == "avg"
    assert next(m for m in model["metrics"] if m["name"] == "Units Total")["source"] == "user"
    assert [o["field"] for o in model["overrides"]] == [o["field"] for o in patch["overrides"]]
    assert env.get(f"{_base(ids)}/semantic-model?version=1").json()["version"] == 1  # kept

    assert env.client.patch(url, json=patch).status_code == 409  # stale base_version
    bad = {"base_version": 2, "overrides": [{"field": "measures.Nope.unit", "value": "x"}]}
    r = env.client.patch(url, json=bad)
    assert r.status_code == 422
    assert "unknown measure" in r.json()["message"]

    env.settle()
    replans = _jobs(env, ids["version_id"], JobType.RUN_PLAN)
    assert sorted(j["semantic_model_version"] for j in replans) == [1, 2]
    with env.container.uow(env.scope) as s:
        actions = s.execute(text("SELECT action FROM audit_logs")).scalars().all()
    assert "semantic_model.override" in actions


def test_domain_override_remaps_and_is_kept_for_new_versions(env: Env, tmp_path: Path) -> None:
    source = FIXTURES / "ambiguous" / "data.csv"
    ids = _upload(env, source)
    domain_url = f"/api/v1/datasets/{ids['dataset_id']}/domain"
    r = env.client.put(domain_url, json={"domain": "retail"})
    assert r.status_code == 200, r.text
    assert r.json()["semantic_model_version"] == 2
    view = env.get(f"{_base(ids)}/understanding").json()
    assert (view["domain"]["primary"], view["domain_source"]) == ("retail", "user_override")
    assert env.client.put(domain_url, json={"domain": "nope"}).status_code == 422

    fix = {"base_version": 2, "overrides": [{"field": "measures.Amount.unit", "value": "pct"}]}
    r = env.client.patch(f"/api/v1/datasets{_base(ids)}/semantic-model", json=fix)
    assert r.status_code == 200, r.text

    # A new version of the same dataset (different bytes) keeps the domain and the correction.
    newer = tmp_path / "data.csv"
    newer.write_text(source.read_text(encoding="utf-8") + "ID9999,CAT_A,1.00,2.00,2026-08-28\n")
    second = _upload(env, newer, dataset_id=ids["dataset_id"])
    assert env.version(second)["state"] == "READY"
    view = env.get(f"{_base(second)}/understanding").json()
    assert (view["domain"]["primary"], view["domain_source"]) == ("retail", "user_override")
    model = env.get(f"{_base(second)}/semantic-model").json()
    assert model["version"] == 1
    assert next(m for m in model["measures"] if m["column"] == "amount")["unit"] == "pct"
    assert [o["field"] for o in model["overrides"]] == ["measures.Amount.unit"]


def test_viewer_cannot_change_semantics(env: Env) -> None:
    ids = _upload(env, FIXTURES / "insufficient" / "sales.csv")
    env.role[0] = Role.VIEWER
    assert env.get(f"{_base(ids)}/semantic-model").status_code == 200
    patch = {"base_version": 1, "overrides": [{"field": "measures.Quantity.unit", "value": "x"}]}
    r = env.client.patch(f"/api/v1/datasets{_base(ids)}/semantic-model", json=patch)
    assert r.status_code == 403
    r = env.client.put(f"/api/v1/datasets/{ids['dataset_id']}/domain", json={"domain": "saas"})
    assert r.status_code == 403
