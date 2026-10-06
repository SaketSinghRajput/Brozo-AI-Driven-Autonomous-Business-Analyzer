"""Unit/additivity type-checker (SEM-003) and alias resolution (SEM-005)."""

import pytest

from app.data.domain_packs.loader import default_packs
from app.data.semantic.aliases import alias_similarity, resolve
from app.data.semantic.expression import ExpressionError
from app.data.semantic.typecheck import DictCatalog, Symbol, check_metric, check_rule

CATALOG = DictCatalog(
    {
        ("measure", "Sales Amount"): Symbol("number", "currency:INR", "additive"),
        ("measure", "Unit Price"): Symbol("number", "currency:INR", "non_additive"),
        ("measure", "Quantity"): Symbol("number", "count", "additive"),
        ("measure", "Discount"): Symbol("number", "pct", "non_additive"),
        ("measure", "MRR"): Symbol("number", "currency:INR", "semi_additive"),
        ("measure", "Production"): Symbol("number", "tonne", "additive"),
        ("measure", "Market Price"): Symbol("number", "currency:INR/tonne", "non_additive"),
        ("metric", "Revenue"): Symbol("number", "currency:INR", "additive"),
        ("dimension_key", "Order"): Symbol("key"),
        ("dimension", "Order Date"): Symbol("time"),
        ("dimension", "Region"): Symbol("category"),
    }
)


@pytest.mark.parametrize(
    ("expr", "unit", "additivity"),
    [
        ("sum(measure('Sales Amount'))", "currency:INR", "additive"),
        ("sum(measure('Quantity') * measure('Unit Price'))", "currency:INR", "additive"),
        ("metric('Revenue') / count_distinct(dimension_key('Order'))", "currency:INR",
         "non_additive"),
        ("sum(measure('MRR'))", "currency:INR", "semi_additive"),
        ("avg(measure('Unit Price'))", "currency:INR", "non_additive"),
        ("weighted_avg(measure('Unit Price'), measure('Quantity'))", "currency:INR",
         "non_additive"),
        ("sum(measure('Production') * measure('Market Price'))", "currency:INR", "additive"),
        ("metric('Revenue') / metric('Revenue') * 100", None, "non_additive"),
        ("count()", None, "additive"),
    ],
)  # fmt: skip
def test_valid_metrics(expr: str, unit: str | None, additivity: str) -> None:
    result = check_metric(expr, CATALOG)
    assert (result.unit, result.additivity) == (unit, additivity)


@pytest.mark.parametrize(
    ("expr", "message"),
    [
        ("sum(measure('Unit Price'))", "non-additive"),
        ("sum(sum(measure('Quantity')))", "nested aggregation"),
        ("sum(metric('Revenue'))", "nested aggregation"),
        ("sum(measure('Sales Amount')) + avg(measure('Discount'))", "incompatible units"),
        ("sum(measure('Sales Amount')) / measure('Quantity')", "mixes row-level"),
        ("measure('Sales Amount') + 1", "aggregated number"),
        ("sum(dimension('Region'))", "needs a number"),
        ("days_since(sum(measure('Quantity')))", "time value"),
        ("sum(measure('Nope'))", "unknown measure"),
        ("count() > 5", "aggregated number"),
    ],
)
def test_type_errors(expr: str, message: str) -> None:
    with pytest.raises(ExpressionError, match=message):
        check_metric(expr, CATALOG)


def test_rules() -> None:
    assert check_rule("days_since(max(dimension('Order Date'))) <= 90", CATALOG).kind == "bool"
    with pytest.raises(ExpressionError, match="comparison"):
        check_rule("sum(measure('Quantity'))", CATALOG)
    with pytest.raises(ExpressionError, match="cannot compare"):
        check_rule("max(dimension('Order Date')) > 5", CATALOG)


@pytest.mark.parametrize("header", ["total_amt", "net_sales", "sales_value", "NetSales"])
def test_sales_amount_aliases_resolve(header: str) -> None:
    retail = default_packs()["retail"]
    best = resolve(header, [c for c in retail.concepts if c.kind == "measure"])[0]
    assert best.concept.name == "Sales Amount"
    assert best.score >= 0.95


def test_alias_similarity_ladder() -> None:
    assert alias_similarity("customer_id", "customer id") == 1.0
    assert alias_similarity("id_customer", "customer id") == 0.95
    assert alias_similarity("customer_id", "customer") == pytest.approx(0.8)
    assert alias_similarity("price", "unit price") == 0.5
    assert alias_similarity("zzz", "customer") == 0.0


def test_confirmed_prior_mapping_wins() -> None:
    retail = default_packs()["retail"]
    matches = resolve("amt_x", retail.concepts, prior={"amt_x": "Unit Cost"})
    assert [(m.concept.name, m.from_prior) for m in matches] == [("Unit Cost", True)]
