"""Hand-computable analytics fixtures.

SMALL (7 orders; revenue = quantity * unit_price * (1 - discount_pct / 100)):

    id  date        region category     qty price disc  revenue
    O1  2026-07-03  North  Electronics  2   100   0     200
    O2  2026-07-10  North  Apparel      1   50    10    45
    O3  2026-07-15  South  Electronics  1   100   0     100
    O4  2026-08-02  North  Electronics  1   100   0     100
    O5  2026-08-05  North  Apparel      2   50    0     100
    O6  2026-08-20  South  Electronics  3   100   0     300
    O7  2026-08-21  South  Apparel      1   40    0     40

Jul revenue 345 (North 245, South 100); Aug revenue 540 (North 200, South 340).
Quantity Jul 4, Aug 7. Orders Jul 3, Aug 4 → AOV Jul 115, Aug 135.
"""

from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.csv as pacsv
import pytest

from app.analytics.sql.engine import AnalyticalEngine
from app.analytics.sql.runner import MetricQueryRunner
from app.data.semantic.domain import SemanticModel

FIXTURES = Path(__file__).resolve().parents[4] / "evals" / "fixtures"
REVENUE = "sum(measure('Quantity') * measure('Unit Price') * (1 - measure('Discount Pct') / 100))"


def semantic_model(**overrides: object) -> SemanticModel:
    base: dict[str, object] = {
        "semantic_model_id": "sm_test",
        "dataset_version_id": "dsv_test",
        "version": 1,
        "entities": [
            {"name": "Order", "key": "order_id", "source_table": "orders", "confidence": 1.0}
        ],
        "dimensions": [
            {"name": "Region", "column": "region", "type": "category"},
            {"name": "Category", "column": "category", "type": "category"},
            {"name": "Order Date", "column": "order_date", "type": "time", "grain": "day"},
        ],
        "measures": [
            {
                "name": "Quantity",
                "column": "quantity",
                "unit": "count",
                "additivity": "additive",
                "default_agg": "sum",
                "confidence": 1.0,
                "aliases": ["qty", "units sold"],
            },
            {
                "name": "Unit Price",
                "column": "unit_price",
                "unit": "currency:INR",
                "additivity": "non_additive",
                "default_agg": "avg",
                "confidence": 1.0,
            },
            {
                "name": "Discount Pct",
                "column": "discount_pct",
                "unit": "pct",
                "additivity": "non_additive",
                "default_agg": "avg",
                "confidence": 1.0,
            },
        ],
        "metrics": [
            {"name": "Revenue", "expr": REVENUE, "unit": "currency:INR"},
            {"name": "Orders", "expr": "count_distinct(dimension_key('Order'))"},
            {"name": "AOV", "expr": "metric('Revenue') / metric('Orders')", "unit": "currency:INR"},
        ],
    }
    base.update(overrides)
    return SemanticModel.model_validate(base)


def small_orders() -> pa.Table:
    rows = [
        ("O1", date(2026, 7, 3), "North", "Electronics", 2, 100.0, 0.0),
        ("O2", date(2026, 7, 10), "North", "Apparel", 1, 50.0, 10.0),
        ("O3", date(2026, 7, 15), "South", "Electronics", 1, 100.0, 0.0),
        ("O4", date(2026, 8, 2), "North", "Electronics", 1, 100.0, 0.0),
        ("O5", date(2026, 8, 5), "North", "Apparel", 2, 50.0, 0.0),
        ("O6", date(2026, 8, 20), "South", "Electronics", 3, 100.0, 0.0),
        ("O7", date(2026, 8, 21), "South", "Apparel", 1, 40.0, 0.0),
    ]
    names = ["order_id", "order_date", "region", "category", "quantity", "unit_price"]
    names.append("discount_pct")
    return pa.table({n: [r[i] for r in rows] for i, n in enumerate(names)})


def retail_orders_with_category() -> pa.Table:
    """evals/fixtures/retail orders with the product category attached (one table per version)."""
    orders = pacsv.read_csv(FIXTURES / "retail" / "orders.csv")
    products = pacsv.read_csv(FIXTURES / "retail" / "products.csv").select(
        ["product_id", "category"]
    )
    return orders.join(products, "product_id", join_type="left outer")


@pytest.fixture
def model() -> SemanticModel:
    return semantic_model()


@pytest.fixture
def runner(model: SemanticModel) -> MetricQueryRunner:
    return MetricQueryRunner(AnalyticalEngine(), model, small_orders())
