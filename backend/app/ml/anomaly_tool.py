"""``ml.anomalies`` tool (TASK-ML-019, design 26): flagged points, method, threshold, caveat.

``method="auto"`` picks by data shape: ≥ 2 ``features`` → Isolation Forest; a ``dimension`` →
contextual; a ``time_column`` → STL residual (robust z when no season is found); otherwise
robust z. Every result carries the method, threshold and the caveat that a statistical anomaly
is not a business problem; wording is "anomaly detected", never "problem found".
"""

from typing import Annotated, Literal, Self

import numpy as np
import polars as pl
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.scope import WorkspaceScope
from app.data.profiling.temporal import temporal_stats
from app.ml.anomaly import SKLEARN, display_scores
from app.ml.anomaly_points import (
    Points,
    SeriesSpec,
    multivariate_points,
    row_points,
    series_points,
)
from app.ml.config import FORECAST_GRAINS, ForecastGrain
from app.ml.domain import (
    ANOMALY_CAVEAT,
    AnomalyExplanation,
    AnomalyResult,
    AnomalyScore,
    BaselineScore,
    Metrics,
    MLTask,
    ModelInfo,
    ModelInputs,
    ModelResult,
    TaskSpec,
    TrainingInfo,
)
from app.ml.forecasters import IN_HOUSE, STATSMODELS
from app.ml.timeseries import Agg, as_dates
from app.ml.tooling import MLTool, calculation
from app.planner.domain import Unavailability
from app.tools.domain import ToolMetadata, ToolPermission

Method = Literal["auto", "robust_z", "iqr", "stl_residual", "contextual", "isolation_forest"]


class AnomalyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric: str | None = None  # numeric column; None = row count per period (series only)
    multiply_by: list[str] = []
    method: Method = "auto"
    time_column: str | None = None
    grain: ForecastGrain | None = None
    agg: Agg = "sum"
    dimension: str | None = None
    features: list[str] = []  # >= 2 numeric columns for Isolation Forest
    id_column: str | None = None  # included in each anomaly's subject (rows only)
    threshold: Annotated[float, Field(gt=0)] | None = None
    contamination: Annotated[float, Field(gt=0, le=0.5)] | None = None
    seed: int | None = None

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.method == "isolation_forest" and len(self.features) < 2:
            raise ValueError("isolation_forest needs at least 2 features")
        if self.method == "stl_residual" and self.time_column is None:
            raise ValueError("stl_residual needs time_column")
        if self.method == "contextual" and self.dimension is None:
            raise ValueError("contextual needs a dimension")
        if not self.features and (self.agg == "count") != (self.metric is None):
            raise ValueError("metric is required unless agg is 'count' (then it must be empty)")
        if self.agg == "count" and self.time_column is None:
            raise ValueError("counting rows needs a time_column")
        return self

    def resolved_method(self) -> str:
        if self.method != "auto":
            return self.method
        if len(self.features) >= 2:
            return "isolation_forest"
        if self.dimension is not None:
            return "contextual"
        return "stl_residual" if self.time_column else "robust_z"

    def label(self) -> str:
        if len(self.features) >= 2:
            return ", ".join(self.features)
        if self.metric is None:
            return "count(*)"
        return " * ".join([self.metric, *self.multiply_by])


class AnomalyOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_run_id: str
    rows: int  # points evaluated
    flagged: int
    calculation: str  # recorded on the evidence: method, library version, run id
    result: AnomalyResult
    model_result: ModelResult


def _scores(points: Points, metric: str, features: list[str]) -> list[AnomalyScore]:
    d = points.detection
    shown = np.flatnonzero(d.flagged)
    norm = display_scores(d.statistic[shown])
    medians = np.median(points.x, axis=0)
    out = []
    for k, i in enumerate(shown):
        if d.expected is not None:  # univariate: the detector's own expected value
            j, name, expected = 0, metric, float(d.expected[i])
            direction = "high" if d.statistic[i] > 0 else "low"
        else:  # multivariate: the feature that deviates most from its median
            j = int(np.argmax(np.abs(points.x[i] - medians)))
            name, expected = features[j], float(medians[j])
            direction = "high" if points.x[i, j] > medians[j] else "low"
        out.append(
            AnomalyScore(
                subject=points.subjects[i],
                metric=name,
                observed=float(points.x[i, j]),
                expected=expected,
                score=float(norm[k]),
                direction=direction,
            )
        )
    return sorted(out, key=lambda s: -s.score)


class AnomalyTool(MLTool):
    meta = ToolMetadata(
        name="ml.anomalies",
        version="1.0.0",
        description="Detect statistical anomalies in the current dataset version: in one "
        "measure (robust z / IQR), a time series (STL residual), per member of a dimension "
        "(contextual) or across several numeric columns (Isolation Forest). Returns flagged "
        "points with observed vs expected values, the method and threshold. An anomaly is not "
        "a business problem.",
        permission=ToolPermission.MODEL_RUN,
        side_effects="none",
        timeout_s=120,
        max_calls_per_turn=3,
        parallel_safe=True,
        runs_in="worker",
    )
    input_model: type[BaseModel] = AnomalyArgs
    output_model: type[BaseModel] = AnomalyOutput

    def run(
        self, args: AnomalyArgs, frame: pl.DataFrame, scope: WorkspaceScope, version_id: str
    ) -> AnomalyOutput | Unavailability:
        seed = self.seed(args.seed)
        spec = TaskSpec(
            task=MLTask.ANOMALY,
            dataset_version_id=version_id,
            metric=args.label(),
            time_column=args.time_column,
            seed=seed,
        )
        points = self._points(args, frame, spec, seed)
        if isinstance(points, Unavailability):
            return points
        return self._finish(args, points, scope, version_id, seed)

    def _points(
        self, args: AnomalyArgs, frame: pl.DataFrame, spec: TaskSpec, seed: int
    ) -> Points | Unavailability:
        method = args.resolved_method()
        if method == "isolation_forest":
            return multivariate_points(
                self._cfg, spec, frame, args.features, args.id_column, args.contamination, seed
            )
        if args.time_column is not None:
            stats = temporal_stats(as_dates(frame, args.time_column))
            native = stats.granularity if stats else None
            native_grain = next((g for g in FORECAST_GRAINS if g == native), None)
            grain: ForecastGrain = args.grain or native_grain or "day"
            series = SeriesSpec(args.time_column, args.metric, args.multiply_by, args.agg, grain)
            return series_points(
                self._cfg, spec, frame, series, method, args.dimension, args.threshold
            )
        measure = (args.metric or "", args.multiply_by)
        return row_points(
            self._cfg, spec, frame, measure, method, args.dimension, args.id_column, args.threshold
        )

    def _finish(
        self, args: AnomalyArgs, points: Points, scope: WorkspaceScope, version_id: str, seed: int
    ) -> AnomalyOutput:
        cfg, d = self._cfg.anomaly, points.detection
        total, n = int(d.flagged.sum()), len(points.x)
        caveats = [ANOMALY_CAVEAT, *points.caveats]
        if total > cfg.max_reported:
            caveats.append(f"Showing the {cfg.max_reported} strongest of {total} anomalies.")
        explanation = AnomalyExplanation(
            method=points.method,  # one of the five detector names (validated by the contract)
            threshold=d.threshold,
            contributing_features=points.contributing,
            caveats=caveats,
        )
        scores = _scores(points, args.label(), args.features)[: cfg.max_reported]
        result = AnomalyResult(metric=args.label(), anomalies=scores, explanation=explanation)
        metrics = Metrics(
            primary="flagged_ratio",
            values={"flagged": total, "evaluated": n, "flagged_ratio": total / n if n else 0.0},
            baseline=BaselineScore(
                name="robust_z",
                values={
                    "flagged": int(points.baseline.sum()),
                    "flagged_ratio": float(points.baseline.mean()) if n else 0.0,
                },
            ),
        )
        library = {"isolation_forest": SKLEARN, "stl_residual": STATSMODELS}.get(
            points.method, IN_HOUSE
        )
        model = ModelInfo(
            name=points.method,
            library=library,
            params=points.params | {"threshold": d.threshold, "seed": seed},
        )
        run = self.persist(scope, version_id, MLTask.ANOMALY, model, args, metrics, seed)
        model_result = ModelResult(
            model_run_id=run.id,
            task=MLTask.ANOMALY,
            model=model,
            inputs=ModelInputs(dataset_version_id=version_id, metric=args.label(), rows=n),
            training=TrainingInfo(split="none", seed=seed),
            metrics=metrics,
            limitations=caveats,
        )
        return AnomalyOutput(
            model_run_id=run.id,
            rows=n,
            flagged=total,
            calculation=calculation(
                run, model, f"anomaly detection on {args.label()} (threshold {d.threshold:.4g})"
            ),
            result=result,
            model_result=model_result,
        )
