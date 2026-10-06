"""TASK-ML-015..018 anomaly detectors and ML-020..022 segmentation, on small synthetic data."""

import datetime as dt

import numpy as np
import polars as pl
import pytest
from sklearn.metrics import adjusted_rand_score

from app.ml.anomaly import (
    by_context,
    by_iqr,
    by_isolation_forest,
    by_peer_ratio,
    by_robust_z,
    by_stl_residual,
    display_scores,
)
from app.ml.config import ML_CONFIG
from app.ml.segments import candidates, choose, entity_features, profile, scale


def test_robust_z_hand_computed_and_mad_zero_fallback() -> None:
    d = by_robust_z(np.array([1.0, 2.0, 3.0, 4.0, 100.0]), 3.5)
    assert d.statistic[-1] == pytest.approx(0.6745 * 97)  # median 3, MAD 1
    assert d.flagged.tolist() == [False, False, False, False, True]
    flat = by_robust_z(np.array([5.0] * 9 + [50.0]), 3.5)  # MAD 0 -> mean-abs-dev scale
    assert flat.flagged.tolist() == [False] * 9 + [True]
    assert np.isfinite(flat.statistic).all()


def test_iqr_fences() -> None:
    d = by_iqr(np.array([1.0, 2.0, 3.0, 4.0, 5.0, 20.0]), 1.5)
    assert d.flagged.tolist() == [False] * 5 + [True]


def test_stl_residual_finds_spike_in_seasonal_series() -> None:
    t = np.arange(35)
    y = 50 + 10 * np.sin(2 * np.pi * t / 7) + np.random.default_rng(0).normal(0, 0.5, 35)
    y[20] += 25
    d = by_stl_residual(y, 7, 3.5)
    assert 20 in np.flatnonzero(d.flagged).tolist()
    assert d.flagged.sum() <= 2
    assert d.expected is not None


def test_contextual_within_member_and_peer_ratio() -> None:
    values = np.array([10.0, 11.0, 10.5, 30.0, 100.0, 101.0, 99.0, 100.5])
    d = by_context(values, np.array(["a"] * 4 + ["b"] * 4), 3.5)
    assert np.flatnonzero(d.flagged).tolist() == [3]  # 30 is normal globally, not for "a"
    periods = 12
    rng = np.random.default_rng(1)
    trend = np.linspace(100, 200, periods)[:, None]
    matrix = trend * np.array([1.0, 1.2, 0.8, 1.1]) + rng.normal(0, 1, (periods, 4))
    matrix[-1, 0] *= 0.5  # member 0 halves in the last period while the others grow
    peer = by_peer_ratio(matrix, 3.5)
    flagged = np.flatnonzero(peer.flagged)
    assert flagged.tolist() == [(periods - 1) * 4]
    assert peer.statistic[flagged[0]] < 0  # direction: low


def test_isolation_forest_is_seeded_and_flags_extreme_rows() -> None:
    rng = np.random.default_rng(2)
    x = rng.normal(0, 1, (300, 3))
    x[7] = [15.0, -15.0, 15.0]
    a, _ = by_isolation_forest(x, 0.01, 42, 100, 10_000)
    b, _ = by_isolation_forest(x, 0.01, 42, 100, 10_000)
    assert np.array_equal(a.statistic, b.statistic)
    assert a.flagged[7]
    assert display_scores(np.array([-2.0, 1.0])).tolist() == [1.0, 0.5]


def _blobs(seed: int = 0) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for c, (qty, price) in enumerate([(1, 10.0), (20, 10.0), (5, 200.0)]):
        for e in range(40):
            for _ in range(3):
                rows.append(
                    {
                        "cust": f"c{c}_{e}",
                        "qty": max(1.0, qty + rng.normal(0, 0.3)),
                        "price": price * (1 + rng.normal(0, 0.02)),
                        "d": dt.date(2026, 1, 1) + dt.timedelta(days=int(rng.integers(0, 90))),
                    }
                )
    return pl.DataFrame(rows)


def test_entity_features_and_segment_recovery() -> None:
    feats = entity_features(_blobs(), "cust", ["qty", "price"], None)
    assert feats.height == 120
    assert {"rows", "sum_qty", "mean_price"} <= set(feats.columns)
    assert "recency_days" in entity_features(_blobs(), "cust", ["qty"], "d").columns
    columns = [c for c in feats.columns if c != "cust"]
    best, _ = choose(candidates(scale(feats, columns), 42, ML_CONFIG, {"kmeans"}), ML_CONFIG)
    assert best is not None
    truth = np.array([int(c[1]) for c in feats["cust"].to_list()])
    assert adjusted_rand_score(truth, best.labels) > 0.95
    profiles = profile(feats, columns, best.labels)
    assert sum(p.size for p in profiles) == 120
    for p in profiles:  # descriptions quote only the profile's own numbers
        cited = [c for c in columns if f" {c} ({p.means[c]:.4g} vs " in p.description]
        assert len(cited) == 2
        assert all(f"{p.overall_means[c]:.4g} overall" in p.description for c in cited)


def test_structureless_data_fails_the_gates() -> None:
    x = np.random.default_rng(5).uniform(0, 1, (200, 2))
    best, excluded = choose(candidates(x, 42, ML_CONFIG, {"kmeans", "hdbscan"}), ML_CONFIG)
    assert best is None
    assert excluded
    assert all(e.reason for e in excluded)
