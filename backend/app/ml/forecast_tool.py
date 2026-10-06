"""``ml.forecast`` tool (TASK-ML-014, design 25, TRD 17): an estimate, never a fact.

Series → detector refusal rules (no date, unsupported/irregular grain, too little history, too
many missing periods, horizon too long) → explicit fill + outlier flags → seasonality →
rolling-origin backtest of baseline + catalog candidates → margin-rule selection → final fit on
the whole history → points with prediction intervals, labelled ``"estimate"``.
"""

from typing import Annotated, Literal, Self

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from app.core.scope import WorkspaceScope
from app.ml.config import ForecastGrain
from app.ml.detector import DataCharacteristics, MLTaskDetector
from app.ml.domain import (
    BaselineScore,
    ForecastResult,
    Metrics,
    MLTask,
    ModelInfo,
    ModelInputs,
    ModelResult,
    Selection,
    TaskSpec,
    TrainingInfo,
)
from app.ml.forecasters import make_forecaster
from app.ml.predictor import NO_INTERVALS_NOTE, forecast_points
from app.ml.selector import forecast_splits, select_forecaster
from app.ml.timeseries import Agg, build_series, detect_season, prepare
from app.ml.tooling import MLTool, calculation
from app.ml.trainer import Trainer
from app.planner.domain import ReasonCode, Unavailability
from app.tools.domain import ToolMetadata, ToolPermission


class ForecastArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    time_column: str
    metric: str | None = None  # numeric column; None only with agg="count"
    multiply_by: list[str] = []  # row value = metric x these columns (e.g. unit_price)
    agg: Agg = "sum"
    grain: ForecastGrain | None = None
    horizon: Annotated[int, Field(ge=1, le=366)]
    fill: Literal["interpolate", "zero"] = "interpolate"
    seed: int | None = None

    @model_validator(mode="after")
    def _metric(self) -> Self:
        if (self.agg == "count") != (self.metric is None):
            raise ValueError("metric is required unless agg is 'count' (then it must be empty)")
        if self.multiply_by and self.metric is None:
            raise ValueError("multiply_by needs a metric")
        return self

    def label(self) -> str:
        if self.metric is None:
            return "count(*)"
        return f"{self.agg}({' * '.join([self.metric, *self.multiply_by])})"


class ForecastOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_run_id: str
    rows: int
    calculation: str  # recorded on the evidence: model, library version, run id
    forecast: ForecastResult
    model_result: ModelResult
    selection: Selection


def _refuse(message: str) -> Unavailability:
    return Unavailability(
        analysis="forecast", reason_code=ReasonCode.INSUFFICIENT_HISTORY, message=message
    )


class ForecastTool(MLTool):
    meta = ToolMetadata(
        name="ml.forecast",
        version="1.0.0",
        description="Forecast a metric of the current dataset version over future periods. "
        "Returns point estimates with prediction intervals, backtest metrics against a naive "
        "baseline and the selected model. Refuses with a reason when history is too short, "
        "irregular or has too many gaps. Results are estimates, not facts.",
        permission=ToolPermission.MODEL_RUN,
        side_effects="none",
        timeout_s=120,
        max_calls_per_turn=3,
        parallel_safe=True,
        runs_in="worker",
    )
    input_model: type[BaseModel] = ForecastArgs
    output_model: type[BaseModel] = ForecastOutput

    def run(
        self, args: ForecastArgs, frame: pl.DataFrame, scope: WorkspaceScope, version_id: str
    ) -> ForecastOutput | Unavailability:
        cfg, seed = self._cfg.forecast, self.seed(args.seed)
        ts = build_series(
            frame, args.time_column, args.metric, args.multiply_by, args.agg, args.grain
        )
        spec = TaskSpec(
            task=MLTask.FORECAST,
            dataset_version_id=version_id,
            metric=args.label(),
            time_column=args.time_column,
            grain=ts.grain,
            horizon=args.horizon,
            seed=seed,
        )
        chars = DataCharacteristics(
            rows=ts.rows,
            has_time=True,
            grain=ts.grain,
            periods=len(ts.values),
            regularity=ts.regularity,
            missing_ratio=ts.missing_ratio,
            horizon=args.horizon,
        )
        decision = MLTaskDetector(self._cfg).detect(None, spec, chars)
        if isinstance(decision, Unavailability):
            return decision
        prepared = prepare(ts, args.fill, cfg.outlier_z)
        y = prepared.y
        season, strength = detect_season(y, ts.grain, cfg)
        splits = forecast_splits(len(y), args.horizon, self._cfg)
        if len(splits) < cfg.min_backtest_folds:
            return _refuse(
                f"{len(y)} {ts.grain} periods cannot hold {cfg.min_backtest_folds} backtest folds "
                f"of {args.horizon} periods after {cfg.min_train_points} training periods."
            )
        trainer = Trainer(self._cfg)
        chars_json: dict[str, JsonValue] = {
            "periods": len(y),
            "grain": ts.grain,
            "season": season,
            "seasonal_strength": strength,
            "filled_periods": prepared.filled,
            "outlier_periods": len(prepared.outliers),
        }
        sel = select_forecaster(y, season, splits, seed, trainer, chars_json, self._cfg)
        best = sel.best.name
        fitted = trainer.fit(
            best, lambda x: make_forecaster(best, season).fit(x), y, seed, can_downsample=False
        )
        points, bounded = forecast_points(
            fitted.model, ts.periods[-1], ts.grain, args.horizon, cfg.interval_level
        )
        metrics = Metrics(
            primary="mae",
            values=sel.best.values,
            baseline=BaselineScore(name=sel.baseline.name, values=sel.baseline.values),
        )
        model = ModelInfo(name=best, library=fitted.model.library, params=fitted.model.params)
        limitations = prepared.notes + sel.best.caveats + fitted.caveats
        if sel.selection.fell_back_to_baseline:
            limitations.append(
                f"No candidate model beat the {sel.baseline.name} baseline by the selection "
                "margin; the baseline forecast is shown."
            )
        season_note = (
            f"Seasonal period {season} (STL strength {strength:.2f})."
            if season and strength is not None
            else "No significant seasonality detected."
        )
        forecast = ForecastResult(
            metric=args.label(),
            grain=ts.grain,
            horizon=args.horizon,
            points=points,
            interval_level=cfg.interval_level if bounded else None,
            note=None if bounded else NO_INTERVALS_NOTE,
            model=model,
            backtest=metrics,
            assumptions=[
                season_note,
                "Intervals assume future errors behave like past (in-sample) errors.",
                *limitations,
            ],
        )
        run = self.persist(
            scope, version_id, MLTask.FORECAST, model, args, metrics, seed, sel.selection
        )
        result = ModelResult(
            model_run_id=run.id,
            task=MLTask.FORECAST,
            model=model,
            inputs=ModelInputs(
                dataset_version_id=version_id, metric=args.label(), grain=ts.grain, rows=len(y)
            ),
            training=TrainingInfo(
                split="rolling_origin", folds=sel.folds, horizon=args.horizon, seed=seed
            ),
            metrics=metrics,
            limitations=limitations,
        )
        return ForecastOutput(
            model_run_id=run.id,
            rows=ts.rows,
            calculation=calculation(
                run, model, f"{args.horizon}-period {ts.grain} forecast of {args.label()}"
            ),
            forecast=forecast,
            model_result=result,
            selection=sel.selection,
        )
