"""MLTaskDetector (TASK-ML-002, design 23): may this ML task run on this data?

Two layers, in order:
1. The planner's ``CapabilityReport`` (dataset-level rules). If it marks the task's analysis
   unavailable, that ``Unavailability`` is returned unchanged.
2. Data checks on the characteristics of the concrete slice the tool will fit on (the series,
   rows or entities). Each failed check returns an ``Unavailability`` with a specific reason code
   and numbers (e.g. "3 month periods; forecasting needs at least 12").
"""

from dataclasses import dataclass

from app.ml.config import ML_CONFIG, MLConfig
from app.ml.domain import MLTask, TaskSpec
from app.planner.domain import CapabilityReport, ReasonCode, Unavailability

# Analysis names used by the capability rules / fixture labels for each ML task.
ANALYSIS_BY_TASK = {
    MLTask.FORECAST: "forecast",
    MLTask.ANOMALY: "anomaly_detection",
    MLTask.SEGMENTATION: "customer_segmentation",
    MLTask.CLASSIFICATION: "classification",
    MLTask.REGRESSION: "regression",
}


@dataclass(frozen=True)
class DataCharacteristics:
    rows: int
    numeric_features: int = 0
    multivariate: bool = False
    has_time: bool = False
    grain: str | None = None
    periods: int = 0
    regularity: float | None = None  # None when the series was re-aggregated to a coarser grain
    missing_ratio: float = 0.0
    horizon: int | None = None
    entities: int = 0
    has_target: bool = False


@dataclass(frozen=True)
class TaskDecision:
    task: MLTask
    chars: DataCharacteristics


def _no(task: MLTask, code: ReasonCode, message: str, enable: str | None = None) -> Unavailability:
    return Unavailability(
        analysis=ANALYSIS_BY_TASK[task], reason_code=code, message=message, would_enable=enable
    )


class MLTaskDetector:
    def __init__(self, config: MLConfig = ML_CONFIG) -> None:
        self._cfg = config

    def detect(
        self, caps: CapabilityReport | None, spec: TaskSpec, chars: DataCharacteristics
    ) -> TaskDecision | Unavailability:
        if caps is not None:
            name = ANALYSIS_BY_TASK[spec.task]
            refused = next((u for u in caps.unavailable if u.analysis == name), None)
            if refused is not None:
                return refused
        if spec.task is MLTask.FORECAST:
            problem = self._forecast(chars)
        elif spec.task is MLTask.ANOMALY:
            problem = self._anomaly(chars)
        elif spec.task is MLTask.SEGMENTATION:
            problem = self._segmentation(chars)
        else:
            problem = self._supervised(spec.task, chars)
        return problem if problem is not None else TaskDecision(task=spec.task, chars=chars)

    def _forecast(self, c: DataCharacteristics) -> Unavailability | None:
        cfg, task = self._cfg.forecast, MLTask.FORECAST
        if not c.has_time:
            return _no(
                task, ReasonCode.NO_TIME_DIMENSION, "No usable date column.", "a date column"
            )
        need = cfg.min_periods.get(c.grain)  # type: ignore[call-overload]  # str key lookup
        if need is None:
            return _no(
                task,
                ReasonCode.IRREGULAR_TIME,
                f"Grain {c.grain!r} is not supported for forecasting.",
                "daily, weekly, monthly, quarterly or yearly data",
            )
        if c.regularity is not None and c.regularity < cfg.min_regularity:
            return _no(
                task,
                ReasonCode.IRREGULAR_TIME,
                f"Only {c.regularity:.0%} of time steps follow the {c.grain} grain; "
                f"forecasting needs at least {cfg.min_regularity:.0%}.",
            )
        if c.periods < need:
            return _no(
                task,
                ReasonCode.INSUFFICIENT_HISTORY,
                f"Only {c.periods} {c.grain} periods of history; forecasting needs at least "
                f"{need}.",
                f"at least {need} {c.grain} periods of history",
            )
        if c.missing_ratio > cfg.max_missing_ratio:
            return _no(
                task,
                ReasonCode.TOO_MANY_MISSING_PERIODS,
                f"{c.missing_ratio:.0%} of {c.grain} periods have no data; the limit is "
                f"{cfg.max_missing_ratio:.0%}.",
            )
        if c.horizon is not None and c.horizon > cfg.max_horizon_ratio * c.periods:
            return _no(
                task,
                ReasonCode.INSUFFICIENT_HISTORY,
                f"A horizon of {c.horizon} periods needs at least "
                f"{int(c.horizon / cfg.max_horizon_ratio)} periods of history; found {c.periods}.",
            )
        return None

    def _anomaly(self, c: DataCharacteristics) -> Unavailability | None:
        cfg, task = self._cfg.anomaly, MLTask.ANOMALY
        if c.rows < cfg.min_rows:
            return _no(
                task,
                ReasonCode.INSUFFICIENT_ROWS,
                f"Only {c.rows} values; anomaly detection needs at least {cfg.min_rows}.",
            )
        if c.multivariate and c.numeric_features < 2:
            return _no(
                task, ReasonCode.NO_NUMERIC_FEATURES, "Isolation Forest needs 2+ numeric features."
            )
        return None

    def _segmentation(self, c: DataCharacteristics) -> Unavailability | None:
        need, task = self._cfg.segmentation.min_entities, MLTask.SEGMENTATION
        if c.entities < need:
            return _no(
                task,
                ReasonCode.INSUFFICIENT_ENTITIES,
                f"Only {c.entities} entities; segmentation needs at least {need}.",
            )
        if c.numeric_features < 1:
            return _no(task, ReasonCode.NO_NUMERIC_FEATURES, "No numeric features per entity.")
        return None

    def _supervised(self, task: MLTask, c: DataCharacteristics) -> Unavailability | None:
        if not c.has_target:
            return _no(task, ReasonCode.NO_TARGET, "No target column.", "a target column")
        need = self._cfg.anomaly.min_rows * 5
        if c.rows < need:
            return _no(task, ReasonCode.INSUFFICIENT_ROWS, f"Only {c.rows} rows; need {need}.")
        return None
