"""ModelSelector for forecasting (TASK-ML-006, design 24).

1. The baseline (seasonal-naive if a season was detected, else naive) is always scored.
2. Candidates come from the versioned catalog (``ML_CONFIG.catalog``, cheapest first); each is
   excluded with a stated reason when it needs a season that was not found, the series is too
   short for it, its estimated cost is over budget, or it fails to fit.
3. All are backtested on the same rolling-origin splits; primary metric = MAE.
4. Margin rule: the best candidate replaces the baseline only if its MAE is at least
   ``selection_margin`` lower AND it has the lower MAE in a strict majority of folds.
   Otherwise the baseline is selected and ``fell_back_to_baseline`` is true.
"""

from dataclasses import dataclass

from pydantic import JsonValue

from app.core.errors import ModelError
from app.ml.backtest import Backtest, backtest
from app.ml.baselines import forecast_baseline
from app.ml.config import ML_CONFIG, MLConfig
from app.ml.domain import ExcludedCandidate, Selection
from app.ml.splitter import Split, temporal
from app.ml.timeseries import Floats
from app.ml.trainer import Trainer

PRIMARY = "mae"


@dataclass(frozen=True)
class ForecastSelection:
    selection: Selection
    baseline: Backtest
    best: Backtest
    folds: int


def forecast_splits(n: int, horizon: int, cfg: MLConfig = ML_CONFIG) -> list[Split]:
    f = cfg.forecast
    return temporal(n, f.backtest_folds, horizon, f.min_train_points)


def margin_rule(cfg: MLConfig = ML_CONFIG) -> str:
    return (
        f"candidate MAE <= (1 - {cfg.forecast.selection_margin}) x baseline MAE and lower MAE "
        "in a strict majority of backtest folds; otherwise keep the baseline"
    )


def _beats(cand: Backtest, base: Backtest, margin: float) -> bool:
    wins = sum(c < b for c, b in zip(cand.fold_mae, base.fold_mae, strict=True))
    better = cand.values[PRIMARY] <= (1 - margin) * base.values[PRIMARY]
    return better and wins * 2 > len(base.fold_mae)


def select_forecaster(
    y: Floats,
    season: int | None,
    splits: list[Split],
    seed: int,
    trainer: Trainer,
    chars: dict[str, JsonValue],
    cfg: MLConfig = ML_CONFIG,
) -> ForecastSelection:
    level = cfg.forecast.interval_level
    base_name = forecast_baseline(season)
    try:
        base = backtest(base_name, season, y, splits, level, trainer, seed)
    except ModelError:
        base_name, base = "naive", backtest("naive", None, y, splits, level, trainer, seed)
    excluded: list[ExcludedCandidate] = []
    scored: dict[str, Backtest] = {}
    budget = cfg.resources.max_cost_units
    for entry in (e for e in cfg.catalog if e.task == "forecast"):
        reason = None
        if entry.needs_season and not season:
            reason = "needs a significant seasonal period; none detected"
        elif len(y) < entry.min_points:
            reason = f"needs at least {entry.min_points} periods; history has {len(y)}"
        elif trainer.cost(entry.name, len(y), 1) * len(splits) > budget:
            reason = "estimated backtest cost exceeds the resource budget"
        if reason is None:
            try:
                scored[entry.name] = backtest(entry.name, season, y, splits, level, trainer, seed)
            except ModelError as exc:
                reason = exc.message
        if reason is not None:
            excluded.append(ExcludedCandidate(candidate=entry.name, reason=reason))
    best = min(scored.values(), key=lambda b: b.values[PRIMARY], default=None)
    margin = cfg.forecast.selection_margin
    chosen = best if best is not None and _beats(best, base, margin) else base
    scores = {base_name: base.values[PRIMARY]} | {n: b.values[PRIMARY] for n, b in scored.items()}
    selection = Selection(
        best=chosen.name,
        primary_metric=PRIMARY,
        scores=scores,
        excluded=excluded,
        margin_rule=margin_rule(cfg),
        fell_back_to_baseline=chosen is base,
        data_characteristics=chars | {"catalog_version": cfg.catalog_version},
    )
    return ForecastSelection(selection=selection, baseline=base, best=chosen, folds=len(splits))
