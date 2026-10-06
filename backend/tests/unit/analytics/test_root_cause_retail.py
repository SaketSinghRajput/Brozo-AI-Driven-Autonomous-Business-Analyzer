"""TASK-ANALYSIS-014: RCA ranks the seeded retail driver first and never claims causation.

Ground truth: evals/fixtures/retail/labels.json ``rca_ground_truth`` (North region, revenue,
2026-08 vs 2026-07): total delta -6568.47, Electronics delta -6949.01 (share 105.79 %).
The generator rounds each order line to 0.01 (Decimal) before summing; the engine sums the
unrounded line amounts, so money is compared within CENTS = 0.05.
"""

import json
from datetime import date

import pytest
from pydantic import ValidationError

from app.analytics.services.root_cause import (
    FactorFinding,
    RootCauseRequest,
    RootCauseResult,
    RootCauseService,
)
from app.analytics.services.statistics import correlate
from app.analytics.sql.engine import AnalyticalEngine
from app.analytics.sql.runner import MetricQueryRunner
from tests.unit.analytics.conftest import (
    FIXTURES,
    retail_orders_with_category,
    semantic_model,
    small_orders,
)

CENTS = 0.05
TRUTH = json.loads((FIXTURES / "retail" / "labels.json").read_text(encoding="utf-8"))[
    "rca_ground_truth"
]


def retail_runner() -> MetricQueryRunner:
    dims = [
        *semantic_model().model_dump()["dimensions"],
        {"name": "Channel", "column": "channel", "type": "category"},
    ]
    return MetricQueryRunner(
        AnalyticalEngine(), semantic_model(dimensions=dims), retail_orders_with_category()
    )


@pytest.fixture(scope="module")
def result() -> RootCauseResult:
    request = RootCauseRequest(
        metric="Revenue",
        current_start=date(2026, 8, 1),
        current_end=date(2026, 8, 31),
        filters=[{"dimension": "Region", "op": "eq", "value": TRUTH["scope"]["region"]}],
        dimensions=["Category", "Channel"],
        volume_metric="Quantity",
    )
    return RootCauseService(retail_runner()).explain(request)


def test_seeded_driver_ranks_first(result: RootCauseResult) -> None:
    assert result.compare_range == (date(2026, 7, 1), date(2026, 7, 31))
    assert result.delta == pytest.approx(float(TRUTH["region_total_delta"]), abs=CENTS)
    top = result.findings[0]
    assert (top.dimension, top.member) == ("Category", TRUTH["expected_dominant_category"])
    assert top.relation_type == "contribution"
    expected_delta = float(TRUTH["delta_by_category"]["Electronics"])
    assert top.magnitude == pytest.approx(expected_delta, abs=CENTS)
    # labels.json records shares x100; the service reports fractions (TRD 25.1)
    expected_share = TRUTH["contribution_share_pct_by_category"]["Electronics"] / 100
    assert top.share_pct == pytest.approx(expected_share, abs=CENTS / 100)
    assert "not established" in result.causal_certainty.lower()
    assert result.dimensions[0].dimension == "Category"  # the most concentrated dimension


def test_every_category_matches_ground_truth(result: RootCauseResult) -> None:
    category = next(d for d in result.dimensions if d.dimension == "Category")
    deltas = {i.key: i.delta for i in category.change.items}
    for name, expected in TRUTH["delta_by_category"].items():
        assert deltas[name] == pytest.approx(float(expected), abs=CENTS)
    assert sum(deltas.values()) == pytest.approx(category.change.total_delta)
    assert sum(i.share_pct or 0 for i in category.change.items) == pytest.approx(1.0)


def test_findings_carry_association_and_caveats(result: RootCauseResult) -> None:
    for finding in result.findings:
        assert finding.relation_type == "contribution"
        assert finding.association.n >= 2
        assert any("not evidence of causation" in c for c in finding.association.caveats)
        assert "a contribution to the change, not a proven cause" in finding.caveats
    pvm = result.price_volume_mix
    assert pvm is not None
    assert pvm.residual == pytest.approx(0, abs=1e-6)
    assert pvm.total_change == pytest.approx(result.delta or 0, abs=CENTS)
    assert len(result.queries) >= 6  # every number traces to a recorded, parameterized query
    assert all("$p0" in str(q["sql"]) for q in result.queries)


def test_causation_is_refused_by_the_contract() -> None:
    with pytest.raises(ValidationError, match="CausalEvidence"):
        FactorFinding(
            factor="Category=Electronics",
            dimension="Category",
            member="Electronics",
            relation_type="causation",
            magnitude=-1.0,
            unit=None,
            share_pct=1.0,
            temporal="coincident",
            association=correlate([1, 2, 3], [1, 2, 3]),
            score=1.0,
            caveats=[],
        )


def test_small_fixture_rca_by_hand() -> None:
    # Revenue Jul 345 → Aug 540 (+195): South +240 (1.2308 = 123.08 %), North -45 (-0.2308)
    runner = MetricQueryRunner(AnalyticalEngine(), semantic_model(), small_orders())
    res = RootCauseService(runner).explain(
        RootCauseRequest(
            metric="Revenue",
            current_start=date(2026, 8, 1),
            current_end=date(2026, 8, 31),
            dimensions=["Region"],
            history_periods=2,
        )
    )
    assert (res.before, res.after, res.delta) == (345.0, 540.0, 195.0)
    assert res.outcome.value == pytest.approx(195 / 345)
    assert [(f.member, round(f.share_pct or 0, 4)) for f in res.findings] == [
        ("South", 1.2308),
        ("North", -0.2308),
    ]
