"""TASK-INTEGRATION-004/005, DASH-002..006, TOOL-007 end to end on PostgreSQL (RLS on).

upload -> ingest -> profile -> understand -> run_plan (plan -> executor -> tools -> evidence ->
dashboard spec) -> READY, then GET the dashboard, its rows, a filtered re-query and viz.spec.
"""

import asyncio
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.dashboard.domain import ChartWidget, DashboardSpec, KpiCardWidget, UnavailableWidget
from app.dashboard.filters import FilterBinder
from app.dashboard.viz_tool import VizSpecOutput
from app.evidence.recorder import CallKey
from app.jobs.models import Job, JobStatus
from app.jobs.worker import JobContext
from app.planner.run_plan import run_plan_handler
from app.tools.domain import ToolContext, ToolPermission
from app.tools.executor import TurnBudget
from app.tools.permissions import Role

from ..dataset_env import FIXTURES, Env


def _url(ids: dict[str, str]) -> str:
    return f"/{ids['dataset_id']}/versions/{ids['version_id']}/dashboard"


def _ready(env: Env, path: Path) -> tuple[dict[str, str], DashboardSpec]:
    r = env.upload(path)
    assert r.status_code == 201, r.text
    ids: dict[str, str] = r.json()
    env.settle()
    assert env.version(ids)["state"] == "READY", env.version(ids)
    body = env.get(_url(ids))
    assert body.status_code == 200, body.text
    return ids, DashboardSpec.model_validate(body.json())


def _evidence_ids(spec: DashboardSpec) -> list[str]:
    found = []
    for w in spec.widgets:
        if isinstance(w, KpiCardWidget):
            found.append(w.value_ref.evidence_id)
        elif isinstance(w, ChartWidget):
            found += [w.chart.evidence_id, *(o.evidence_id for o in w.chart.overlays)]
    return found


def test_retail_upload_builds_an_evidence_bound_dashboard(env: Env) -> None:
    ids, spec = _ready(env, FIXTURES / "retail" / "orders.csv")
    kpis = [w for w in spec.widgets if isinstance(w, KpiCardWidget)]
    charts = [w for w in spec.widgets if isinstance(w, ChartWidget)]
    assert kpis
    assert charts
    assert kpis[0].metric == "Revenue"  # retail pack KPI order
    assert any(w.widget_type == "line_chart" for w in charts)
    cited = _evidence_ids(spec)
    with env.container.uow(env.scope) as s:  # RLS + explicit workspace filter
        stored = set(
            s.execute(
                text("SELECT id FROM evidence WHERE workspace_id = :ws AND id = ANY(:ids)"),
                {"ws": env.scope.workspace_id, "ids": cited},
            ).scalars()
        )
        states = (
            s.execute(
                text("SELECT state FROM analysis_runs WHERE dataset_version_id = :v"),
                {"v": ids["version_id"]},
            )
            .scalars()
            .all()
        )
    assert stored == set(cited)
    assert states
    assert set(states) <= {"COMPLETED", "PARTIAL"}
    assert all(w.validation_status in {"VALID", "UNCERTAIN"} for w in kpis)
    assert {i.id for i in spec.layout.items} == {w.id for w in spec.widgets}
    assert spec.schema_version == "1.1"
    assert {f.type for f in spec.filters} >= {"date_range", "multi_select"}
    print(  # E2E evidence for the report
        f"retail widgets={len(spec.widgets)} kpis={len(kpis)} charts={len(charts)} "
        f"evidence={len(cited)} insights={len(spec.insights)} summary={len(spec.summary)} "
        f"kpi_status={[w.validation_status.value for w in kpis]} "
        f"chart_types={[w.widget_type for w in charts]} runs={states}"
    )

    trend = next(w for w in charts if w.widget_type == "line_chart")
    data = env.client.get(f"/api/v1/evidence/{trend.chart.evidence_id}/data").json()
    assert data["rows"]
    assert trend.chart.x is not None
    assert trend.chart.x.field in data["rows"][0]

    env.role[0] = Role.VIEWER  # dashboards are readable by viewers
    assert env.get(_url(ids)).status_code == 200
    env.role[0] = Role.ANALYST


def test_dashboard_filter_requery_creates_new_evidence(env: Env) -> None:
    ids, spec = _ready(env, FIXTURES / "retail" / "orders.csv")
    kpi = next(w for w in spec.widgets if isinstance(w, KpiCardWidget) and w.query is not None)
    assert kpi.query is not None
    region = next(f for f in spec.filters if f.dimension == "Region")
    base = f"/{ids['dataset_id']}/versions/{ids['version_id']}/dimensions"
    picked = env.get(f"{base}/{region.dimension}/values?limit=2").json()
    assert (picked["values"], picked["truncated"]) == (["East", "North"], True)  # sorted, bounded
    assert env.get(f"{base}/Order Date/values").status_code == 422  # time: a date range
    assert env.get(f"{base}/Nope/values").status_code in (404, 422)
    bound = FilterBinder().bind(kpi.query, spec.filters, {region.id: ["East"]})
    assert bound.filters[-1].model_dump() == {"dimension": "Region", "op": "in", "value": ["East"]}
    body = {
        "dataset_version_id": ids["version_id"],
        "semantic_model_version": 1,
        "request": bound.model_dump(mode="json"),
    }
    r = env.client.post("/api/v1/analytics/query", json=body)
    assert r.status_code == 200, r.text
    filtered = r.json()
    assert filtered["evidence_id"] != kpi.value_ref.evidence_id
    original = env.client.get(f"/api/v1/evidence/{kpi.value_ref.evidence_id}").json()
    assert filtered["value"] < original["result"]["value"]  # one region of several


def test_insufficient_data_shows_unavailable_cards_with_reasons(env: Env) -> None:
    ids, spec = _ready(env, FIXTURES / "insufficient" / "sales.csv")
    caps_url = f"/{ids['dataset_id']}/versions/{ids['version_id']}/capabilities"
    caps = env.get(caps_url).json()
    reasons = {(u["analysis"], u["reason_code"]) for u in caps["unavailable"]}
    cards = [w for w in spec.widgets if isinstance(w, UnavailableWidget)]
    assert cards
    from_report = {w.id.removeprefix("u_"): w for w in cards}
    assert "forecast" in from_report
    for analysis, card in from_report.items():
        if (analysis, card.reason_code.value) in reasons:
            assert card.message.startswith("Insufficient data")
    assert ("forecast", from_report["forecast"].reason_code.value) in reasons
    print(
        "insufficient unavailable=",
        sorted((k, w.reason_code.value) for k, w in from_report.items()),
    )


def test_capabilities_report_what_the_run_could_not_use(env: Env) -> None:
    """U-4 (live demo): Segmentation was "available" while its widget said "Not available"
    (quality gate failed). Every available analysis whose dashboard card is unavailable is
    listed in ``attempted`` with the reason its card shows."""
    ids, spec = _ready(env, FIXTURES / "retail" / "orders.csv")
    caps = env.get(f"/{ids['dataset_id']}/versions/{ids['version_id']}/capabilities").json()
    cards = {
        (w.reason_code.value, w.message) for w in spec.widgets if isinstance(w, UnavailableWidget)
    }
    attempted = {a["analysis"]: (a["reason_code"], a["message"]) for a in caps["attempted"]}
    print("retail attempted=", attempted)
    # synthetic customers buy at random: no segmentation passes the quality gate
    assert attempted["customer_segmentation"][0] == "LOW_MODEL_QUALITY"
    assert set(attempted) <= set(caps["available"])
    assert set(attempted.values()) <= cards


def test_unknown_domain_gets_a_generic_dashboard(env: Env) -> None:
    ids, spec = _ready(env, FIXTURES / "ambiguous" / "data.csv")
    model_url = f"/{ids['dataset_id']}/versions/{ids['version_id']}/semantic-model"
    model = env.get(model_url).json()
    sources = {m["name"]: m.get("source") for m in model["metrics"]}
    shown = [w.metric for w in spec.widgets if isinstance(w, KpiCardWidget)]
    assert shown
    for metric in shown:
        source = sources.get(metric)
        assert source is None or source.startswith("pack:generic@"), (metric, source)


def test_stale_run_plan_job_is_skipped(env: Env) -> None:
    ids, _ = _ready(env, FIXTURES / "retail" / "orders.csv")
    with env.container.uow(env.scope) as s:
        raw = dict(
            s.execute(
                text("SELECT * FROM jobs WHERE type = 'run_plan' AND payload->>'version_id' = :v"),
                {"v": ids["version_id"]},
            )
            .one()
            ._mapping
        )
    payload: dict[str, Any] = {**raw["payload"], "semantic_model_version": 7}
    job = Job.model_validate({**raw, "payload": payload, "status": JobStatus.RUNNING})
    assert run_plan_handler(env.container)(job, JobContext(job)) is None
    assert env.version(ids)["state"] == "READY"


def test_viz_spec_tool_charts_an_evidence_record(env: Env) -> None:
    ids, spec = _ready(env, FIXTURES / "retail" / "orders.csv")
    chart = next(w for w in spec.widgets if isinstance(w, ChartWidget))
    ctx = ToolContext(
        workspace_id=env.scope.workspace_id,
        principal_id="usr_test",
        trace_id="viz",
        permissions=frozenset({ToolPermission.MEMORY_READ}),
    )
    key = CallKey(f"turn_{ids['version_id']}", "viz")
    args = {"evidence_id": chart.chart.evidence_id}
    executor = env.container.tool_executor
    result = asyncio.run(executor.run("viz.spec", args, ctx, key, TurnBudget(2)))
    assert result.status == "ok", result.error
    assert result.output is not None
    out = VizSpecOutput.model_validate(result.output)
    assert out.chart.evidence_id == chart.chart.evidence_id
    assert result.evidence_id is None  # presentation only: no new evidence


def test_analysis_runs_are_listed_per_version(env: Env) -> None:
    """GAPS-4: GET .../analysis-runs (state, timestamps, step counts), newest first."""
    ids, _ = _ready(env, FIXTURES / "retail" / "orders.csv")
    url = f"/{ids['dataset_id']}/versions/{ids['version_id']}/analysis-runs"
    env.role[0] = Role.VIEWER  # VIEW_DATASET is enough
    r = env.get(url)
    env.role[0] = Role.ANALYST
    assert r.status_code == 200, r.text
    (run,) = r.json()
    assert run["id"].startswith("ar_")
    assert run["state"] in {"COMPLETED", "PARTIAL"}
    assert run["started_at"]
    assert run["finished_at"]
    counts = run["step_counts"]
    assert sum(counts.values()) == run["steps_total"] > 0
    assert counts.get("SUCCEEDED", 0) > 0
    missing = env.get(f"/{ids['dataset_id']}/versions/dsv_missing/analysis-runs")
    assert missing.status_code == 404


def test_replanning_serves_the_previous_dashboard_marked_stale(env: Env) -> None:
    """GAPS-5b: after a semantic patch and before run_plan finishes, the last READY dashboard
    is served with stale=true instead of 404."""
    ids, before = _ready(env, FIXTURES / "retail" / "orders.csv")
    assert before.stale is False
    base = f"/{ids['dataset_id']}/versions/{ids['version_id']}"
    patch = {
        "base_version": 1,
        "overrides": [{"field": "measures.Quantity.default_agg", "value": "avg"}],
    }
    assert env.client.patch(f"/api/v1/datasets{base}/semantic-model", json=patch).status_code == 200
    during = env.get(_url(ids))
    assert during.status_code == 200, during.text
    stale = DashboardSpec.model_validate(during.json())
    assert stale.stale is True
    assert stale.dashboard_id == before.dashboard_id
    env.settle()
    after = DashboardSpec.model_validate(env.get(_url(ids)).json())
    assert after.stale is False
    assert after.dashboard_id != before.dashboard_id
