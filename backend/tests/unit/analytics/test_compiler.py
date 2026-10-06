"""TASK-SQL-003/004/006: resolver, compiler (golden SQL + results on the small fixture), results.
Expected numbers are hand-computed in the conftest docstring."""

from datetime import date
from typing import Any

import pyarrow as pa
import pytest

from app.analytics.domain import MetricRequest
from app.analytics.sql.compiler import QueryCompiler
from app.analytics.sql.engine import AnalyticalEngine, QueryLimits
from app.analytics.sql.periods import calendar_periods, comparison_range
from app.analytics.sql.resolver import SemanticResolver
from app.analytics.sql.runner import MetricQueryRunner
from app.core.errors import SchemaError
from app.data.semantic.domain import SemanticModel
from tests.unit.analytics.conftest import semantic_model, small_orders

JUL = {"grain": "month", "start": date(2026, 7, 1), "end": date(2026, 7, 31)}
AUG = {"grain": "month", "start": date(2026, 8, 1), "end": date(2026, 8, 31)}


def req(**kw: Any) -> MetricRequest:
    return MetricRequest.model_validate(kw)


def rows(runner: MetricQueryRunner, **kw: Any) -> list[list[Any]]:
    return runner.run(req(**kw)).table.rows


def test_golden_sql_is_parameterized(model: SemanticModel) -> None:
    compiled = QueryCompiler(model).compile(
        req(
            metric="qty",
            dimensions=["region"],
            filters=[{"dimension": "Category", "op": "eq", "value": "Apparel"}],
            limit=10,
        )
    )
    assert compiled.sql == (
        'SELECT "region" AS "Region", SUM("quantity") AS "value" FROM "data" '
        'WHERE ("category" = $p0) GROUP BY 1 ORDER BY 1 LIMIT 11'
    )
    assert compiled.params == {"p0": "Apparel"}


def test_golden_sql_compare_to(model: SemanticModel) -> None:
    compiled = QueryCompiler(model).compile(
        req(metric="Quantity", time=AUG, compare_to="previous_period")
    )
    day = 'CAST("order_date" AS DATE)'
    assert compiled.sql == (
        f'SELECT SUM(CASE WHEN {day} BETWEEN $p0 AND $p1 THEN "quantity" END) AS "value", '  # noqa: S608
        f'SUM(CASE WHEN {day} BETWEEN $p2 AND $p3 THEN "quantity" END) AS "compare_value" '
        f'FROM "data" WHERE ({day} BETWEEN $p0 AND $p1 OR {day} BETWEEN $p2 AND $p3) LIMIT 1001'
    )
    assert compiled.json_params() == {
        "p0": "2026-08-01",
        "p1": "2026-08-31",
        "p2": "2026-07-01",
        "p3": "2026-07-31",
    }


def test_metric_totals(runner: MetricQueryRunner) -> None:
    assert rows(runner, metric="Revenue") == [[885.0]]  # 345 + 540
    assert rows(runner, metric="Quantity") == [[11]]
    assert rows(runner, metric="Orders") == [[7]]
    assert rows(runner, metric="Unit Price") == [[pytest.approx(540 / 7)]]  # avg of prices


def test_group_by_dimension_and_month(runner: MetricQueryRunner) -> None:
    result = runner.run(req(metric="Revenue", dimensions=["Region"], time={"grain": "month"}))
    assert [c.name for c in result.table.columns] == ["Region", "period", "value"]
    assert [c.type for c in result.table.columns] == ["string", "date", "float"]
    assert result.table.columns[2].unit == "currency:INR"
    assert result.table.rows == [
        ["North", "2026-07-01", 245.0],
        ["North", "2026-08-01", 200.0],
        ["South", "2026-07-01", 100.0],
        ["South", "2026-08-01", 340.0],
    ]


def test_compare_to_previous_period(runner: MetricQueryRunner) -> None:
    run = runner.run(req(metric="Revenue", time=AUG, compare_to="previous_period"))
    assert run.table.rows == [[540.0, 345.0]]
    assert run.compiled.compare_range == (date(2026, 7, 1), date(2026, 7, 31))
    aov = rows(runner, metric="AOV", dimensions=["Region"], time=AUG, compare_to="previous_period")
    # North: Aug 200/2 = 100, Jul 245/2 = 122.5; South: Aug 340/2 = 170, Jul 100/1 = 100
    assert aov == [["North", 100.0, 122.5], ["South", 170.0, 100.0]]


def test_filters(runner: MetricQueryRunner) -> None:
    def f(op: str, value: Any, dim: str = "Region") -> list[dict[str, Any]]:
        return [{"dimension": dim, "op": op, "value": value}]

    assert rows(runner, metric="Revenue", filters=f("eq", "North")) == [[445.0]]
    assert rows(runner, metric="Revenue", filters=f("in", ["North", "South"])) == [[885.0]]
    between = f("between", [date(2026, 8, 1), date(2026, 8, 5)], "Order Date")
    assert rows(runner, metric="Revenue", filters=between) == [[200.0]]
    late = f("gte", date(2026, 8, 20), "Order Date")
    assert rows(runner, metric="Quantity", filters=late) == [[4]]
    assert rows(runner, metric="Quantity", filters=f("eq", "Nowhere")) == [[None]]


def test_time_range_without_compare(runner: MetricQueryRunner) -> None:
    assert rows(runner, metric="Revenue", time=JUL) == [["2026-07-01", 345.0]]


def test_row_cap_is_flagged_not_hidden(runner: MetricQueryRunner) -> None:
    table = runner.run(req(metric="Revenue", dimensions=["Region", "Category"], limit=3)).table
    assert (table.row_count, table.truncated) == (3, True)
    assert "result truncated at 3 rows" in table.warnings
    engine = AnalyticalEngine(QueryLimits(max_rows=2))
    capped = MetricQueryRunner(engine, semantic_model(), small_orders())
    assert capped.run(req(metric="Revenue", dimensions=["Category", "Region"])).table.truncated


def test_result_hash_is_stable_and_content_sensitive(runner: MetricQueryRunner) -> None:
    first = runner.run(req(metric="Revenue", dimensions=["Region"])).table.result_hash
    again = runner.run(req(metric="Revenue", dimensions=["Region"])).table.result_hash
    other = runner.run(req(metric="Quantity", dimensions=["Region"])).table.result_hash
    assert first == again != other


def test_null_aggregate_warning(runner: MetricQueryRunner) -> None:
    table = runner.run(
        req(metric="Revenue", filters=[{"dimension": "Region", "op": "eq", "value": "X"}])
    ).table
    assert "the aggregate is null for every row (no matching data)" in table.warnings


def test_unknown_names_give_suggestions(model: SemanticModel) -> None:
    compiler = QueryCompiler(model)
    with pytest.raises(SchemaError, match="did you mean: Revenue") as info:
        compiler.compile(req(metric="revenu"))
    assert info.value.details == {"suggestions": ["Revenue"]}
    with pytest.raises(SchemaError, match="unknown dimension; did you mean: Region"):
        compiler.compile(req(metric="Revenue", dimensions=["regoin"]))


def test_resolver_matches_names_aliases_and_columns(model: SemanticModel) -> None:
    resolver = SemanticResolver(model)
    assert resolver.metric("units_sold").name == "Quantity"
    assert resolver.metric("QTY").name == "Quantity"
    assert resolver.dimension("order-date").name == "Order Date"
    assert resolver.time_dimension(None).name == "Order Date"
    with pytest.raises(SchemaError, match="not a time dimension"):
        resolver.time_dimension("Region")


def test_ambiguous_alias_is_refused() -> None:
    measures = semantic_model().model_dump()["measures"]
    measures[1]["aliases"] = ["qty"]  # Unit Price now also claims "qty"
    with pytest.raises(SchemaError, match="ambiguous"):
        SemanticResolver(semantic_model(measures=measures)).measure("qty")


def test_additivity_is_enforced() -> None:
    metrics = [{"name": "Bad", "expr": "sum(measure('Unit Price'))"}]
    with pytest.raises(SchemaError, match="non-additive"):
        QueryCompiler(semantic_model(metrics=metrics)).compile(req(metric="Bad"))


def test_semi_additive_sum_warns() -> None:
    measures = semantic_model().model_dump()["measures"]
    measures[0]["additivity"] = "semi_additive"
    model = semantic_model(measures=measures)
    table = MetricQueryRunner(AnalyticalEngine(), model, small_orders()).run(req(metric="qty"))
    assert any("semi-additive" in w for w in table.table.warnings)


@pytest.mark.parametrize(
    "expr",
    ["metric('A')", "sum(measure('Quantity')) + metric('B')"],  # A→A and A→B→A
)
def test_circular_metrics_are_refused(expr: str) -> None:
    metrics = [{"name": "A", "expr": expr}, {"name": "B", "expr": "metric('A')"}]
    with pytest.raises(SchemaError, match="circular"):
        QueryCompiler(semantic_model(metrics=metrics)).compile(req(metric="A"))


def test_unit_inference(model: SemanticModel) -> None:
    compiled = QueryCompiler(model).compile(req(metric="Orders"))
    assert compiled.columns[-1].unit == "count"


def test_period_arithmetic() -> None:
    assert comparison_range(date(2026, 7, 1), date(2026, 7, 31), "previous_period") == (
        date(2026, 6, 1),
        date(2026, 6, 30),
    )
    assert comparison_range(date(2026, 3, 10), date(2026, 3, 16), "previous_period") == (
        date(2026, 3, 3),
        date(2026, 3, 9),
    )
    assert comparison_range(date(2024, 2, 1), date(2024, 2, 29), "same_period_last_year") == (
        date(2023, 2, 1),
        date(2023, 2, 28),
    )
    assert comparison_range(date(2025, 2, 1), date(2025, 2, 28), "same_period_last_year") == (
        date(2024, 2, 1),
        date(2024, 2, 29),
    )
    assert calendar_periods(date(2026, 1, 15), date(2026, 7, 2), "quarter") == [
        date(2026, 1, 1),
        date(2026, 4, 1),
        date(2026, 7, 1),
    ]


def test_count_star_metric_counts_rows_and_compares_periods() -> None:
    """B3 / DEBT-006: the generic pack's count() ("Record Count") compiles through the one
    semantic parser; with compare_to each period is counted separately."""
    metrics = [*semantic_model().model_dump()["metrics"], {"name": "Rows", "expr": "count()"}]
    runner = MetricQueryRunner(AnalyticalEngine(), semantic_model(metrics=metrics), small_orders())
    assert runner.run(req(metric="Rows")).table.rows == [[7]]
    aug = {"grain": "month", "start": "2026-08-01", "end": "2026-08-31"}
    run = runner.run(req(metric="Rows", time=aug, compare_to="previous_period"))
    assert run.table.rows == [[4, 3]]  # Aug 4 orders, Jul 3


def _yearly() -> MetricQueryRunner:
    """Integer ``year`` time dimension (agriculture-style data)."""
    dims = [
        {"name": "Region", "column": "region", "type": "category"},
        {"name": "Crop Year", "column": "year", "type": "time", "grain": "year"},
    ]
    model = semantic_model(dimensions=dims)
    table = pa.table(
        {
            "order_id": ["a", "b", "c", "d"],
            "year": [2023, 2024, 2025, 2025],
            "region": ["N", "N", "S", "N"],
            "quantity": [1, 2, 3, 4],
            "unit_price": [10.0, 10.0, 10.0, 10.0],
            "discount_pct": [0.0, 0.0, 0.0, 0.0],
        }
    )
    return MetricQueryRunner(AnalyticalEngine(), model, table)


def test_integer_year_time_dimension_supports_ranges_and_comparisons() -> None:
    """B2: an integer year column is compared as a year, never CAST AS DATE (DuckDB rejects it)."""
    runner = _yearly()
    y25 = {"grain": "year", "start": "2025-01-01", "end": "2025-12-31"}
    assert runner.run(req(metric="Quantity", time=y25)).table.rows == [["2025-01-01", 7]]
    run = runner.run(req(metric="Quantity", time=y25, compare_to="previous_period"))
    assert run.table.rows == [[7, 2]]
    between = [{"dimension": "Crop Year", "op": "between", "value": ["2024-01-01", "2025-12-31"]}]
    assert runner.run(req(metric="Quantity", filters=between)).table.rows == [[9]]


@pytest.mark.parametrize(
    "expr", ["weighted_avg(measure('Unit Price'), measure('Quantity'))", "count() > 3"]
)
def test_grammar_beyond_the_engine_is_refused(expr: str) -> None:
    metrics = [{"name": "X", "expr": expr}]
    with pytest.raises(SchemaError):
        QueryCompiler(semantic_model(metrics=metrics)).compile(req(metric="X"))
