"""Analysis run contracts + persistence (TASK-DB-007, TASK-ANALYSIS-007; design §18).

A run executes one validated plan. Step states are kept as a JSON map on the run row and
updated after every transition (one cheap UPDATE), so a restarted job resumes: SUCCEEDED and
UNAVAILABLE steps are not re-run, a RUNNING step is retried with the same attempt number (the
same deterministic evidence id, so its evidence insert is a no-op).
"""

import json
from collections import Counter
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from sqlalchemy import Row, text

from app.core.scope import WorkspaceScope
from app.infra.repository import Repository
from app.planner.domain import AnalysisPlan, Unavailability
from app.tools.domain import ErrorInfo


class StepState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"  # a dependency did not succeed
    UNAVAILABLE = "UNAVAILABLE"  # the tool returned a typed "not possible with this data"
    CANCELLED = "CANCELLED"


class RunState(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


FINISHED_STEPS = frozenset({StepState.SUCCEEDED, StepState.UNAVAILABLE})


class StepRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str
    tool: str
    state: StepState = StepState.PENDING
    attempts: Annotated[int, Field(ge=0)] = 0
    evidence_id: str | None = None
    error: ErrorInfo | None = None
    unavailable: Unavailability | None = None
    duration_ms: Annotated[int, Field(ge=0)] = 0


class AnalysisRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Annotated[str, Field(pattern=r"^ar_\w+$")]
    plan_id: str
    dataset_version_id: str
    semantic_model_version: int
    state: RunState
    steps: dict[str, StepRecord]
    dashboard: dict[str, JsonValue] | None = None


class AnalysisRunSummary(BaseModel):
    """``GET .../analysis-runs`` row: no plan, steps or dashboard bodies."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    plan_id: str
    semantic_model_version: int
    state: RunState
    started_at: datetime
    finished_at: datetime | None
    steps_total: int
    step_counts: dict[StepState, int]  # steps per state


_COLUMNS = "id, plan_id, dataset_version_id, semantic_model_version, state, steps, dashboard"
MAX_LISTED_RUNS = 50


def _run(row: Row[Any]) -> AnalysisRun:
    return AnalysisRun.model_validate(dict(row._mapping))


class AnalysisRunRepository(Repository):
    def save_plan(self, scope: WorkspaceScope, plan: AnalysisPlan) -> None:
        self._session.execute(
            text(
                "INSERT INTO analysis_plans (id, workspace_id, dataset_version_id, "
                "semantic_model_version, version, plan) VALUES (:id, :ws, :dsv, :smv, :v, "
                "CAST(:plan AS jsonb)) ON CONFLICT (id) DO NOTHING"
            ),
            {
                "id": plan.plan_id,
                "ws": scope.workspace_id,
                "dsv": plan.dataset_version_id,
                "smv": plan.semantic_model_version,
                "v": plan.version,
                "plan": plan.model_dump_json(),
            },
        )

    def start(
        self, scope: WorkspaceScope, run_id: str, plan: AnalysisPlan, job_id: str | None
    ) -> AnalysisRun:
        """Create the run with every step PENDING; an existing run (a job retry) is returned."""
        steps = {s.id: StepRecord(step_id=s.id, tool=s.tool) for s in plan.steps}
        self._session.execute(
            text(
                "INSERT INTO analysis_runs (id, workspace_id, plan_id, dataset_version_id, "
                "semantic_model_version, job_id, state, steps) VALUES (:id, :ws, :plan, :dsv, "
                ":smv, :job, 'RUNNING', CAST(:steps AS jsonb)) ON CONFLICT (id) DO NOTHING"
            ),
            {
                "id": run_id,
                "ws": scope.workspace_id,
                "plan": plan.plan_id,
                "dsv": plan.dataset_version_id,
                "smv": plan.semantic_model_version,
                "job": job_id,
                "steps": json.dumps({k: v.model_dump(mode="json") for k, v in steps.items()}),
            },
        )
        run = self.get(scope, run_id)
        if run is None:  # pragma: no cover - written in this transaction
            raise RuntimeError("analysis run missing after insert")
        return run

    def get(self, scope: WorkspaceScope, run_id: str) -> AnalysisRun | None:
        row = self._session.execute(
            text(
                f"SELECT {_COLUMNS} FROM analysis_runs "  # noqa: S608 (constant columns)
                "WHERE id = :id AND workspace_id = :ws"
            ),
            {"id": run_id, "ws": scope.workspace_id},
        ).one_or_none()
        return None if row is None else _run(row)

    def put_step(self, scope: WorkspaceScope, run_id: str, record: StepRecord) -> None:
        self._session.execute(
            text(
                "UPDATE analysis_runs SET steps = jsonb_set(steps, ARRAY[:sid], "
                "CAST(:rec AS jsonb)) WHERE id = :id AND workspace_id = :ws"
            ),
            {
                "id": run_id,
                "ws": scope.workspace_id,
                "sid": record.step_id,
                "rec": record.model_dump_json(),
            },
        )

    def finish(
        self,
        scope: WorkspaceScope,
        run_id: str,
        state: RunState,
        dashboard: dict[str, JsonValue] | None = None,
    ) -> None:
        self._session.execute(
            text(
                "UPDATE analysis_runs SET state = :state, finished_at = now(), "
                "dashboard = COALESCE(CAST(:dash AS jsonb), dashboard) "
                "WHERE id = :id AND workspace_id = :ws"
            ),
            {
                "id": run_id,
                "ws": scope.workspace_id,
                "state": state.value,
                "dash": None if dashboard is None else json.dumps(dashboard),
            },
        )

    def summaries(self, scope: WorkspaceScope, dataset_version_id: str) -> list[AnalysisRunSummary]:
        rows = self._session.execute(
            text(
                "SELECT id, plan_id, semantic_model_version, state, started_at, finished_at, "
                "steps FROM analysis_runs WHERE workspace_id = :ws AND dataset_version_id = :dsv "
                "ORDER BY started_at DESC, id DESC LIMIT :limit"
            ),
            {"ws": scope.workspace_id, "dsv": dataset_version_id, "limit": MAX_LISTED_RUNS},
        )
        out = []
        for r in rows:
            states = Counter(StepState(s["state"]) for s in dict(r.steps).values())
            out.append(
                AnalysisRunSummary(
                    id=r.id,
                    plan_id=r.plan_id,
                    semantic_model_version=r.semantic_model_version,
                    state=RunState(r.state),
                    started_at=r.started_at,
                    finished_at=r.finished_at,
                    steps_total=sum(states.values()),
                    step_counts=dict(states),
                )
            )
        return out

    def latest_finished(
        self, scope: WorkspaceScope, dataset_version_id: str, semantic_model_version: int
    ) -> AnalysisRun | None:
        """The newest run that built a dashboard for that semantic model version."""
        row = self._session.execute(
            text(
                f"SELECT {_COLUMNS} FROM analysis_runs "  # noqa: S608 (constant columns)
                "WHERE workspace_id = :ws AND dataset_version_id = :dsv "
                "AND semantic_model_version = :smv AND dashboard IS NOT NULL "
                "ORDER BY finished_at DESC, id DESC LIMIT 1"
            ),
            {"ws": scope.workspace_id, "dsv": dataset_version_id, "smv": semantic_model_version},
        ).one_or_none()
        return None if row is None else _run(row)

    def latest_dashboard(
        self, scope: WorkspaceScope, dataset_version_id: str, semantic_model_version: int | None
    ) -> dict[str, Any] | None:
        """The newest dashboard for that semantic model version (None: for any version)."""
        dashboard = self._session.execute(
            text(
                "SELECT dashboard FROM analysis_runs WHERE workspace_id = :ws "
                "AND dataset_version_id = :dsv "
                "AND (CAST(:smv AS integer) IS NULL OR semantic_model_version = :smv) "
                "AND dashboard IS NOT NULL ORDER BY finished_at DESC, id DESC LIMIT 1"
            ),
            {"ws": scope.workspace_id, "dsv": dataset_version_id, "smv": semantic_model_version},
        ).scalar_one_or_none()
        return None if dashboard is None else dict(dashboard)
