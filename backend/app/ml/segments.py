"""Segmentation (TASK-ML-020 entity features, ML-021 clustering gates, ML-022 profiles).

- ``entity_features``: one row per entity — row count, sum and mean of each measure, and with a
  date column recency/tenure in days (relative to the dataset's last date, not "today").
- ``candidates``/``choose``: K-Means for k in [k_min, k_max] (seeded, n_init=10) and HDBSCAN
  (deterministic) on log1p + standardised features. A candidate passes only if silhouette ≥
  min_silhouette and ≥ min_silhouette_gain above the silhouette K-Means gets on uniform noise
  in the same bounding box (structureless reference), stability (mean ARI of re-fits with
  other seeds / 90 % subsamples) ≥
  min_stability_ari, every cluster holds ≥ min_cluster_share of entities, and HDBSCAN noise ≤
  max_noise_ratio. The best passing silhouette wins; if none passes the caller refuses.
- ``profile``: per segment size, share and feature means vs overall; the description is built
  only from those profile numbers.
"""

from dataclasses import dataclass

import numpy as np
import polars as pl
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict
from sklearn.cluster import HDBSCAN, KMeans
from sklearn.metrics import adjusted_rand_score

from app.core.errors import DataError
from app.ml.config import MLConfig
from app.ml.domain import ExcludedCandidate
from app.ml.evaluator import clustering_metrics
from app.ml.features import FeatureBuilder
from app.ml.timeseries import as_dates

Floats = NDArray[np.float64]
Labels = NDArray[np.intp]


def entity_features(
    frame: pl.DataFrame, entity: str, measures: list[str], time_column: str | None
) -> pl.DataFrame:
    if entity not in frame.columns:
        raise DataError(f"unknown column {entity!r}")
    for m in measures:
        if m not in frame.columns or not frame[m].dtype.is_numeric():
            raise DataError(f"column {m!r} is not a numeric column")
    aggs = [pl.len().cast(pl.Float64).alias("rows")]
    for m in measures:
        aggs += [pl.col(m).sum().alias(f"sum_{m}"), pl.col(m).mean().alias(f"mean_{m}")]
    data = frame.filter(pl.col(entity).is_not_null())
    if time_column is not None:
        data = data.with_columns(as_dates(data, time_column).alias("_t"))
        last = data["_t"].max()
        aggs += [
            (pl.lit(last) - pl.col("_t").max()).dt.total_days().alias("recency_days"),
            (pl.col("_t").max() - pl.col("_t").min()).dt.total_days().alias("tenure_days"),
        ]
    return data.group_by(entity).agg(aggs).sort(entity)


@dataclass(frozen=True)
class Candidate:
    name: str  # "kmeans_k3", "hdbscan"
    labels: Labels
    silhouette: float
    davies_bouldin: float
    stability: float
    noise_ratio: float
    reference: float  # silhouette of the same k on structureless (uniform) data


def _reference(x: Floats, k: int, cfg: MLConfig) -> float:
    """Silhouette K-Means reaches on uniform noise in x's bounding box: even structureless data
    scores ~0.4, so real segments must beat this reference, not only an absolute floor."""
    rng = np.random.default_rng(cfg.seed)
    n = min(len(x), 1000)
    noise = rng.uniform(x.min(axis=0), x.max(axis=0), size=(n, x.shape[1]))
    labels = _kmeans(noise, k, cfg.seed)
    if len(set(labels.tolist())) < 2:
        return 0.0
    sample = cfg.segmentation.silhouette_sample
    return clustering_metrics(noise, labels, cfg.seed, sample)["silhouette"]


def _kmeans(x: Floats, k: int, seed: int) -> Labels:
    return np.asarray(KMeans(n_clusters=k, n_init=10, random_state=seed).fit_predict(x))


def _hdbscan(x: Floats, min_size: int) -> Labels:
    return np.asarray(HDBSCAN(min_cluster_size=min_size, copy=True).fit_predict(x))


def _scored(name: str, x: Floats, labels: Labels, stability: float, cfg: MLConfig) -> Candidate:
    keep = labels >= 0
    metrics = clustering_metrics(
        x[keep], labels[keep], cfg.seed, cfg.segmentation.silhouette_sample
    )
    return Candidate(
        name=name,
        labels=labels,
        silhouette=metrics["silhouette"],
        davies_bouldin=metrics["davies_bouldin"],
        stability=stability,
        noise_ratio=float(np.mean(~keep)),
        reference=_reference(x, len(set(labels[keep].tolist())), cfg),
    )


def _gate(c: Candidate, cfg: MLConfig) -> str | None:
    s = cfg.segmentation
    sizes = np.bincount(c.labels[c.labels >= 0])
    if c.silhouette < s.min_silhouette:
        return f"silhouette {c.silhouette:.2f} < {s.min_silhouette}"
    if c.silhouette - c.reference < s.min_silhouette_gain:
        return (
            f"silhouette {c.silhouette:.2f} is not {s.min_silhouette_gain} above structureless "
            f"data ({c.reference:.2f})"
        )
    if c.stability < s.min_stability_ari:
        return f"stability ARI {c.stability:.2f} < {s.min_stability_ari}"
    if sizes.min() < s.min_cluster_share * len(c.labels):
        return f"smallest cluster {int(sizes.min())} < {s.min_cluster_share:.0%} of entities"
    if c.noise_ratio > s.max_noise_ratio:
        return f"noise {c.noise_ratio:.0%} > {s.max_noise_ratio:.0%}"
    return None


def candidates(x: Floats, seed: int, cfg: MLConfig, methods: set[str]) -> list[Candidate]:
    s, n = cfg.segmentation, len(x)
    out: list[Candidate] = []
    if "kmeans" in methods:
        for k in range(s.k_min, min(s.k_max, n - 1) + 1):
            labels = _kmeans(x, k, seed)
            if len(set(labels.tolist())) < 2:
                continue
            reruns = [_kmeans(x, k, seed + r + 1) for r in range(s.stability_runs)]
            stability = float(np.mean([adjusted_rand_score(labels, r) for r in reruns]))
            out.append(_scored(f"kmeans_k{k}", x, labels, stability, cfg))
    if "hdbscan" in methods:
        min_size = max(5, int(np.ceil(s.min_cluster_share * n)))
        labels = _hdbscan(x, min_size)
        if len(set(labels[labels >= 0].tolist())) >= 2:
            rng = np.random.default_rng(seed)
            aris = []
            for _ in range(s.stability_runs):
                idx = np.sort(rng.choice(n, size=int(0.9 * n), replace=False))
                aris.append(adjusted_rand_score(labels[idx], _hdbscan(x[idx], min_size)))
            out.append(_scored("hdbscan", x, labels, float(np.mean(aris)), cfg))
    return out


def choose(
    cands: list[Candidate], cfg: MLConfig
) -> tuple[Candidate | None, list[ExcludedCandidate]]:
    excluded = []
    passing = []
    for c in cands:
        reason = _gate(c, cfg)
        if reason is None:
            passing.append(c)
        else:
            excluded.append(ExcludedCandidate(candidate=c.name, reason=reason))
    return max(passing, key=lambda c: c.silhouette, default=None), excluded


def scale(features: pl.DataFrame, columns: list[str]) -> Floats:
    """log1p on non-negative columns, then standardise (FeatureBuilder fitted on all entities:
    clustering is unsupervised, so there is no held-out set to leak into)."""
    nonneg = [c for c in columns if not (features[c].cast(pl.Float64) < 0).any()]
    fitted = FeatureBuilder().fit(features, numeric=columns, log1p=nonneg)
    return fitted.transform(features)


class SegmentProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    segment: int  # -1 = HDBSCAN noise (unassigned)
    size: int
    share: float
    means: dict[str, float]
    overall_means: dict[str, float]
    description: str


def profile(features: pl.DataFrame, columns: list[str], labels: Labels) -> list[SegmentProfile]:
    raw = features.select(columns).cast(pl.Float64).fill_null(0.0).to_numpy()
    overall, spread = raw.mean(axis=0), raw.std(axis=0)
    spread = np.where(spread > 0, spread, 1.0)
    out = []
    for seg in sorted(set(labels.tolist())):
        rows = raw[labels == seg]
        means = rows.mean(axis=0)
        z = (means - overall) / spread
        parts = [
            f"{'higher' if z[j] > 0 else 'lower'} {columns[j]} ({means[j]:.4g} vs "
            f"{overall[j]:.4g} overall)"
            for j in np.argsort(-np.abs(z), kind="stable")[:2]
        ]
        who = "Unassigned (noise)" if seg < 0 else f"Segment {seg}"
        out.append(
            SegmentProfile(
                segment=int(seg),
                size=len(rows),
                share=len(rows) / len(raw),
                means={c: float(means[j]) for j, c in enumerate(columns)},
                overall_means={c: float(overall[j]) for j, c in enumerate(columns)},
                description=f"{who}: {len(rows)} entities; " + "; ".join(parts) + ".",
            )
        )
    return out
