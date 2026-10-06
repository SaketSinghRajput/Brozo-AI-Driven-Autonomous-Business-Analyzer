"""TASK-ANALYSIS-015: design §62.1 invariants over seeded random inputs (stdlib ``random``;
each case is reproducible from its seed)."""

import random
from datetime import date, timedelta
from itertools import pairwise

import pyarrow as pa
import pytest

from app.analytics.domain import MetricRequest
from app.analytics.services.aggregation import aggregate
from app.analytics.services.contribution import (
    contribution_to_change,
    price_volume_mix,
    ratio_decomposition,
    share_of_total,
)
from app.analytics.services.ranking import RankMethod, rank
from app.analytics.sql.engine import AnalyticalEngine
from app.analytics.sql.runner import MetricQueryRunner
from tests.unit.analytics.conftest import semantic_model

SEEDS = range(25)
EPS = 1e-6
METHODS: tuple[RankMethod, ...] = ("competition", "dense", "row")


def _rng(seed: int) -> random.Random:
    return random.Random(seed)  # noqa: S311  # reproducible test data, not security


def _members(rng: random.Random, low: float = -1000, high: float = 1000) -> dict[str, float]:
    return {f"m{i}": rng.uniform(low, high) for i in range(rng.randint(1, 12))}


@pytest.mark.parametrize("seed", SEEDS)
def test_contributions_sum_to_total_change(seed: int) -> None:
    rng = _rng(seed)
    before, after = _members(rng), _members(rng)  # member sets overlap only partly
    result = contribution_to_change(before, after)
    assert sum(i.delta for i in result.items) == pytest.approx(result.total_delta, abs=EPS)
    assert result.total_delta == pytest.approx(result.total_after - result.total_before, abs=EPS)
    if result.undefined is None:
        assert sum(i.share_pct or 0 for i in result.items) == pytest.approx(1.0, abs=EPS)


@pytest.mark.parametrize("seed", SEEDS)
def test_shares_sum_to_one_without_negatives(seed: int) -> None:
    result = share_of_total(_members(_rng(seed), low=0.01))
    assert sum(i.share_pct or 0 for i in result.items) == pytest.approx(1.0, abs=EPS)
    assert all(0 <= (i.share_pct or 0) <= 1 for i in result.items)


@pytest.mark.parametrize("seed", SEEDS)
def test_price_volume_mix_and_ratio_decompositions_are_exact(seed: int) -> None:
    rng = _rng(seed)

    def parts() -> dict[str, tuple[float, float]]:
        return {
            f"i{k}": (rng.uniform(0, 500), float(rng.randint(0, 20)))
            for k in range(rng.randint(1, 8))
        }

    before, after = parts(), parts()
    assert price_volume_mix(before, after).residual == pytest.approx(0, abs=1e-6)
    ratio = ratio_decomposition(before, after)
    if ratio.change is not None:
        assert ratio.residual == pytest.approx(0, abs=1e-6)


def _random_orders(rng: random.Random, n: int) -> pa.Table:
    start = date(2026, 1, 1)
    return pa.table(
        {
            "order_id": [f"O{i}" for i in range(n)],
            "order_date": [start + timedelta(days=rng.randint(0, 240)) for _ in range(n)],
            "region": [rng.choice(["North", "South", "East", None]) for _ in range(n)],
            "category": [rng.choice(["A", "B", "C"]) for _ in range(n)],
            "quantity": [rng.randint(0, 9) for _ in range(n)],
            "unit_price": [round(rng.uniform(1, 300), 2) for _ in range(n)],
            "discount_pct": [rng.choice([0.0, 5.0, 10.0]) for _ in range(n)],
        }
    )


@pytest.mark.parametrize("seed", range(8))
def test_group_totals_equal_the_total(seed: int) -> None:
    rng = _rng(seed)
    runner = MetricQueryRunner(AnalyticalEngine(), semantic_model(), _random_orders(rng, 300))
    total = runner.run(MetricRequest(metric="Revenue")).table.rows[0][0]
    by_both = runner.run(MetricRequest(metric="Revenue", dimensions=["Region", "Category"])).table
    assert isinstance(total, float)
    assert sum(float(r[2] or 0) for r in by_both.rows) == pytest.approx(total, rel=1e-9)
    rolled = aggregate(by_both, "value", "sum", ["Region"], "additive")
    assert sum(g.value or 0 for g in rolled.groups) == pytest.approx(rolled.total or 0, rel=1e-9)
    assert rolled.total == pytest.approx(total, rel=1e-9)
    monthly = runner.run(
        MetricRequest.model_validate({"metric": "Quantity", "time": {"grain": "month"}})
    ).table
    quantity = runner.run(MetricRequest(metric="Quantity")).table.rows[0][0]
    assert sum(int(r[1] or 0) for r in monthly.rows) == quantity


@pytest.mark.parametrize("seed", SEEDS)
def test_ranking_invariants(seed: int) -> None:
    rng = _rng(seed)
    values = [float(rng.randint(0, 5)) for _ in range(rng.randint(1, 15))]
    items: list[tuple[str, float | None]] = [(f"k{i}", v) for i, v in enumerate(values)]
    for method in METHODS:
        result = rank(items, method=method)
        ranks = [i.rank for i in result.items]
        assert ranks == sorted(ranks)
        assert [i.value for i in result.items] == sorted(values, reverse=True)
        if method == "row":
            assert ranks == list(range(1, len(items) + 1))
        for a, b in pairwise(result.items):
            assert (a.rank == b.rank) == (a.value == b.value and method != "row")


@pytest.mark.parametrize("seed", range(5))
def test_same_inputs_give_identical_hashes(seed: int) -> None:
    data = _random_orders(_rng(seed), 200)
    request = MetricRequest(metric="AOV", dimensions=["Category"])
    first = MetricQueryRunner(AnalyticalEngine(), semantic_model(), data).run(request)
    again = MetricQueryRunner(AnalyticalEngine(), semantic_model(), data).run(request)
    assert first.table.result_hash == again.table.result_hash
