"""B7: retail revenue definitions agree with the fixture labels (evals/fixtures/retail/labels.json).

Pack semantics: "Revenue" is gross (quantity x unit price, the pack's cited definition);
"Net Revenue" applies the line discount (a fraction since GAPS-1). The labels' monthly KPI is net
of discount, so it is checked against "Net Revenue"; gross is checked against the raw CSV.
"""

import csv
import json
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest

from app.analytics.domain import MetricRequest
from app.analytics.sql.engine import AnalyticalEngine
from app.analytics.sql.runner import MetricQueryRunner
from app.infra.storage import LocalStorage

from .conftest import FIXTURES, understand

AUG = {"grain": "month", "start": "2026-08-01", "end": "2026-08-31"}


@pytest.fixture(scope="module")
def runner(tmp_path_factory: pytest.TempPathFactory) -> MetricQueryRunner:
    tmp: Path = tmp_path_factory.mktemp("retail_revenue")
    model = understand("retail", tmp).facts.model
    with LocalStorage(tmp / "store").open_read("v.parquet") as f:
        frame = pl.read_parquet(f)
    return MetricQueryRunner(AnalyticalEngine(), model, frame.to_arrow())


def _august(runner: MetricQueryRunner, metric: str) -> float:
    run = runner.run(MetricRequest.model_validate({"metric": metric, "time": AUG}))
    (row,) = run.table.rows
    value = row[-1]
    assert isinstance(value, int | float)
    return float(value)


def test_net_revenue_matches_the_labelled_monthly_kpi(runner: MetricQueryRunner) -> None:
    labels = json.loads((FIXTURES / "retail" / "labels.json").read_text(encoding="utf-8"))
    kpi = labels["kpis"]["net_revenue_by_month"]
    assert kpi["semantic_metric"] == "Net Revenue"
    with (FIXTURES / "retail" / "orders.csv").open(encoding="utf-8") as f:
        lines = sum(row["order_date"].startswith("2026-08") for row in csv.DictReader(f))
    # the generator rounds each line to 2 decimals (scripts/generate_fixtures.py line_revenue)
    tolerance = 0.005 * lines
    expected = float(kpi["values"]["2026-08"])
    assert _august(runner, "Net Revenue") == pytest.approx(expected, abs=tolerance)


def test_revenue_is_gross_of_discount(runner: MetricQueryRunner) -> None:
    gross = Decimal(0)
    with (FIXTURES / "retail" / "orders.csv").open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["order_date"].startswith("2026-08"):
                gross += Decimal(row["quantity"]) * Decimal(row["unit_price"])
    assert _august(runner, "Revenue") == pytest.approx(float(gross))
    assert _august(runner, "Revenue") > _august(runner, "Net Revenue")
