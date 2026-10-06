"""ML contracts (TASK-ML-001; design 23-26, TRD 17.3, 18).

- ``ModelResult`` is the TRD 17.3 "model result contract". ``ModelRun`` is the design 8 per-analysis
  fit record (task, params, metrics, seed -> evidence); the selection rationale is stored in
  ``ModelRun.selection`` rather than inside ``params`` (design 24 says ``params.selection``) so
  that it is typed.
- ``Metrics`` puts the task metrics under ``values`` and the always-run baseline under
  ``baseline``. TRD 17.3 mixes both into one flat object.
- ``ForecastResult`` (design 25) always has ``label="estimate"``. ``AnomalyExplanation.caveats``
  must include ``ANOMALY_CAVEAT`` (design 26).
"""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

from app.data.domain import Ratio
from app.data.profiling.domain import TimeGrain

ANOMALY_CAVEAT = "statistical anomaly ≠ business problem"
DatasetVersionId = Annotated[str, Field(pattern=r"^dsv_\w+$")]
ModelRunId = Annotated[str, Field(pattern=r"^mr_\w+$")]
EvidenceId = Annotated[str, Field(pattern=r"^ev_\w+$")]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MLTask(StrEnum):
    FORECAST = "forecast"
    ANOMALY = "anomaly"
    SEGMENTATION = "segmentation"
    CLASSIFICATION = "classification"
    REGRESSION = "regression"


class TaskSpec(Contract):
    schema_version: Literal["1.0"] = "1.0"
    task: MLTask
    dataset_version_id: DatasetVersionId
    metric: str | None = None  # forecast / anomaly series metric
    target: str | None = None  # classification / regression
    features: list[str] = []
    time_column: str | None = None
    grain: TimeGrain | None = None
    horizon: Annotated[int, Field(ge=1)] | None = None
    seed: int

    @model_validator(mode="after")
    def _task_inputs(self) -> Self:
        if self.task is MLTask.FORECAST and None in (self.metric, self.time_column, self.horizon):
            raise ValueError("forecast needs metric, time_column and horizon")
        supervised = {MLTask.CLASSIFICATION, MLTask.REGRESSION}
        if self.task in supervised and (self.target is None or not self.features):
            raise ValueError(f"{self.task} needs a target and features")
        if self.target is not None and self.target in self.features:
            raise ValueError("target must not be a feature (leakage)")
        return self


class ModelInfo(Contract):
    name: str  # "ets", "seasonal_naive", "isolation_forest"
    library: str  # "<lib>@<version>"
    params: dict[str, JsonValue] = {}


class TrainingInfo(Contract):
    split: Literal["rolling_origin", "temporal", "stratified_kfold", "kfold", "holdout", "none"]
    folds: Annotated[int, Field(ge=1)] | None = None
    horizon: Annotated[int, Field(ge=1)] | None = None
    seed: int


class BaselineScore(Contract):
    name: str
    values: dict[str, float]


class Metrics(Contract):
    primary: str  # e.g. "mae"
    values: dict[str, float]
    baseline: BaselineScore | None = None  # required by policy for forecast/anomaly/supervised

    @model_validator(mode="after")
    def _primary_present(self) -> Self:
        if self.primary not in self.values:
            raise ValueError("primary metric must be in values")
        if self.baseline is not None and self.primary not in self.baseline.values:
            raise ValueError("baseline must report the primary metric")
        return self


class ExcludedCandidate(Contract):
    candidate: str
    reason: str


class Selection(Contract):
    best: str
    primary_metric: str
    scores: dict[str, float]  # candidate -> primary metric (same splits)
    excluded: list[ExcludedCandidate] = []
    margin_rule: str
    fell_back_to_baseline: bool
    data_characteristics: dict[str, JsonValue] = {}

    @model_validator(mode="after")
    def _best_scored(self) -> Self:
        if self.best not in self.scores:
            raise ValueError("best must be one of the scored candidates")
        return self


class ModelInputs(Contract):
    dataset_version_id: DatasetVersionId
    metric: str | None = None
    grain: TimeGrain | None = None
    rows: Annotated[int, Field(ge=0)]


class ModelResult(Contract):
    schema_version: Literal["1.0"] = "1.0"
    model_run_id: ModelRunId
    task: MLTask
    model: ModelInfo
    inputs: ModelInputs
    training: TrainingInfo
    metrics: Metrics
    result_ref: str | None = None  # "artifact://results/<mr>/forecast.parquet"
    limitations: list[str] = []
    evidence_id: EvidenceId | None = None


class ModelRun(Contract):
    schema_version: Literal["1.0"] = "1.0"
    id: ModelRunId
    workspace_id: Annotated[str, Field(pattern=r"^ws_\w+$")]
    task: MLTask
    params: dict[str, JsonValue]
    selection: Selection | None = None
    metrics: Metrics
    seed: int
    evidence_id: EvidenceId | None = None
    created_at: AwareDatetime


class ForecastPoint(Contract):
    period: str  # ISO date of the period start
    value: float
    lower: float | None = None
    upper: float | None = None

    @model_validator(mode="after")
    def _bounds(self) -> Self:
        if (self.lower is None) != (self.upper is None):
            raise ValueError("lower and upper come together")
        if (
            self.lower is not None
            and self.upper is not None
            and not (self.lower <= self.value <= self.upper)
        ):
            raise ValueError("value must lie within [lower, upper]")
        return self


class ForecastResult(Contract):
    schema_version: Literal["1.0"] = "1.0"
    metric: str
    grain: TimeGrain
    horizon: Annotated[int, Field(ge=1)]
    points: list[ForecastPoint]
    interval_level: Ratio | None = None  # None when the model gives no intervals (+ note)
    note: str | None = None
    model: ModelInfo
    backtest: Metrics
    assumptions: list[str]
    label: Literal["estimate"] = "estimate"

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if len(self.points) != self.horizon:
            raise ValueError("one point per horizon step")
        has_bounds = any(p.lower is not None for p in self.points)
        if has_bounds != (self.interval_level is not None):
            raise ValueError("interval_level is set exactly when points carry bounds")
        if self.interval_level is None and not self.note:
            raise ValueError("a forecast without intervals must say why in note")
        return self


class AnomalyScore(Contract):
    subject: dict[str, JsonValue]  # e.g. {"date": "2026-08-14", "region": "North"}
    metric: str
    observed: float
    expected: float | None
    score: Ratio  # normalised for display ordering only
    direction: Literal["high", "low"]


class AnomalyExplanation(Contract):
    method: Literal["robust_z", "iqr", "stl_residual", "isolation_forest", "contextual"]
    threshold: float
    contributing_features: list[str] = []
    caveats: list[str]

    @model_validator(mode="after")
    def _caveat(self) -> Self:
        if ANOMALY_CAVEAT not in self.caveats:
            raise ValueError(f"caveats must include {ANOMALY_CAVEAT!r}")
        return self


class AnomalyResult(Contract):
    schema_version: Literal["1.0"] = "1.0"
    metric: str
    anomalies: list[AnomalyScore]
    explanation: AnomalyExplanation
