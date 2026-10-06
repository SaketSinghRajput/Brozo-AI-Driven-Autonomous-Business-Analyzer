"""Versioned ML configuration: thresholds, model catalog and resource budget (ML-006/007).

Every threshold used to refuse, select or flag lives here, so a model run can record exactly
which rule set produced it (``ML_CONFIG.version`` goes into every ``ModelRun.params``).
Values are documented in ``docs/ML-models.md``; changing any of them bumps ``version``.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

ForecastGrain = Literal["day", "week", "month", "quarter", "year"]
FORECAST_GRAINS: tuple[ForecastGrain, ...] = ("day", "week", "month", "quarter", "year")


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CatalogEntry(_Frozen):
    name: str
    task: Literal["forecast", "anomaly", "segmentation"]
    needs_season: bool = False
    min_points: int = 0
    cost_per_row: float  # relative fit cost per row x feature (resource-guard units)


class ForecastConfig(_Frozen):
    min_periods: dict[ForecastGrain, int] = {
        "day": 28,
        "week": 16,
        "month": 12,
        "quarter": 8,
        "year": 5,
    }
    default_grain: ForecastGrain = "month"  # used when the data is finer than a month
    min_regularity: float = 0.8
    max_missing_ratio: float = 0.2
    max_horizon_ratio: float = 0.5  # horizon <= ratio x history
    season_by_grain: dict[ForecastGrain, int] = {"day": 7, "week": 52, "month": 12, "quarter": 4}
    min_seasonal_strength: float = 0.5  # STL strength (Hyndman F_s) needed to call it seasonal
    min_seasonal_cycles: int = 3  # with 2 cycles STL's seasonal term absorbs noise (F_s inflated)
    backtest_folds: int = 3
    min_backtest_folds: int = 2
    min_train_points: int = 6
    interval_level: float = 0.9
    selection_margin: float = 0.05  # candidate must beat the baseline MAE by >= 5 %
    outlier_z: float = 3.5


class AnomalyConfig(_Frozen):
    min_rows: int = 10
    robust_z_threshold: float = 3.5  # Iglewicz & Hoaglin modified z-score
    iqr_k: float = 1.5
    contamination: float = 0.01
    isolation_trees: int = 200
    isolation_max_samples: int = 10_000
    max_reported: int = 50


class SegmentationConfig(_Frozen):
    min_entities: int = 30
    k_min: int = 2
    k_max: int = 8
    min_silhouette: float = 0.25
    min_silhouette_gain: float = 0.1  # above the uniform-noise reference silhouette
    min_stability_ari: float = 0.8
    stability_runs: int = 5
    min_cluster_share: float = 0.02
    max_noise_ratio: float = 0.3
    silhouette_sample: int = 5000


class ResourceConfig(_Frozen):
    max_cost_units: float = 5e7


class MLConfig(_Frozen):
    version: str = "ml-config-1.0.0"
    seed: int = 42
    forecast: ForecastConfig = ForecastConfig()
    anomaly: AnomalyConfig = AnomalyConfig()
    segmentation: SegmentationConfig = SegmentationConfig()
    resources: ResourceConfig = ResourceConfig()
    catalog_version: str = "catalog-1.0.0"
    # Cheapest first: ModelSelector keeps catalog order (design 24).
    catalog: tuple[CatalogEntry, ...] = (
        CatalogEntry(name="ets", task="forecast", min_points=10, cost_per_row=50),
        CatalogEntry(name="arima", task="forecast", min_points=12, cost_per_row=400),
        CatalogEntry(name="stl_ets", task="forecast", needs_season=True, cost_per_row=100),
        CatalogEntry(name="isolation_forest", task="anomaly", cost_per_row=200),
        CatalogEntry(name="kmeans", task="segmentation", cost_per_row=40),
        CatalogEntry(name="hdbscan", task="segmentation", cost_per_row=400),
    )

    def entry(self, name: str) -> CatalogEntry:
        return next(e for e in self.catalog if e.name == name)


ML_CONFIG = MLConfig()
