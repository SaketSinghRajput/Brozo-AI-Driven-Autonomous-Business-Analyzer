"""TASK-SQL-008: compiler injection corpus. User/LLM text reaches the compiler as metric names,
dimension names and filter values. Names must resolve against the semantic model or be refused;
values must only ever be bind parameters. Hostile semantic-model content is covered too."""

from datetime import date
from typing import Any

import pyarrow as pa
import pytest

from app.analytics.domain import MetricRequest
from app.analytics.sql.compiler import QueryCompiler, parse_metric
from app.analytics.sql.engine import AnalyticalEngine
from app.analytics.sql.runner import MetricQueryRunner
from app.core.errors import SchemaError, ToolError
from app.data.semantic.domain import SemanticModel
from tests.unit.analytics.conftest import semantic_model, small_orders

CORPUS = [
    "Revenue; DROP TABLE data; --",
    "Revenue' OR '1'='1",
    "Revenue\" FROM data; COPY data TO 'x.csv'; --",
    "x') UNION SELECT * FROM read_csv('secrets.csv') --",
    "1); ATTACH 'evil.db' AS e; --",
    "$p0",
    "region /* comment */",
    "Region\x00",
    "Region\nUNION ALL SELECT 1",
    "(SELECT current_setting('enable_external_access'))",
    "Robert'); INSTALL httpfs; --",
    "' || getenv('HOME') || '",
    "\U0001d4e1\U0001d4ee\U0001d4ff",  # look-alike letters
    "a" * 200,
]


def req(**kw: Any) -> MetricRequest:
    return MetricRequest.model_validate(kw)


@pytest.mark.parametrize("text", CORPUS)
def test_hostile_names_are_refused(model: SemanticModel, text: str) -> None:
    compiler = QueryCompiler(model)
    with pytest.raises(SchemaError) as metric_err:
        compiler.compile(req(metric=text))
    with pytest.raises(SchemaError):
        compiler.compile(req(metric="Revenue", dimensions=[text]))
    bad_filter = [{"dimension": text, "op": "eq", "value": 1}]
    with pytest.raises(SchemaError):
        compiler.compile(req(metric="Revenue", filters=bad_filter))
    with pytest.raises(SchemaError):
        compiler.compile(req(metric="Revenue", time={"grain": "month"}, time_dimension=text))
    assert text not in metric_err.value.message  # errors never echo caller text


@pytest.mark.parametrize("text", CORPUS)
def test_hostile_values_are_only_parameters(runner: MetricQueryRunner, text: str) -> None:
    def filters(value: str) -> list[dict[str, Any]]:
        return [
            {"dimension": "Region", "op": "eq", "value": value},
            {"dimension": "Category", "op": "in", "value": [value, "Apparel"]},
        ]

    run = runner.run(req(metric="Revenue", dimensions=["Region"], filters=filters(text)))
    benign = filters("x")
    benign_sql = QueryCompiler(runner.model).compile(
        req(metric="Revenue", dimensions=["Region"], filters=benign)
    )
    assert run.compiled.sql == benign_sql.sql  # the SQL text never depends on the value
    assert list(run.compiled.params.values()) == [text, text, "Apparel"]
    assert run.table.rows == []  # the value matched nothing; nothing else happened
    assert runner.run(req(metric="Revenue")).table.rows == [[885.0]]  # data untouched


def test_date_values_stay_typed(runner: MetricQueryRunner) -> None:
    window = [date(2026, 1, 1), date(2026, 12, 31)]
    between = [{"dimension": "Order Date", "op": "between", "value": window}]
    run = runner.run(req(metric="Quantity", filters=between))
    assert run.compiled.params == {"p0": window[0], "p1": window[1]}
    assert run.table.rows == [[11]]


def test_hostile_column_in_semantic_model_stays_an_identifier() -> None:
    dims = semantic_model().model_dump()["dimensions"]
    dims[0]["column"] = "region\" FROM data; COPY data TO 'x.csv'; --"
    model = semantic_model(dimensions=dims)
    compiled = QueryCompiler(model).compile(req(metric="Revenue", dimensions=["Region"]))
    assert '"region"" FROM data; COPY data TO \'x.csv\'; --"' in compiled.sql
    runner = MetricQueryRunner(AnalyticalEngine(), model, small_orders())
    with pytest.raises(ToolError, match="query failed"):  # a missing column; nothing executed
        runner.run(req(metric="Revenue", dimensions=["Region"]))


def test_quoted_identifier_with_quote_character_works() -> None:
    dims = semantic_model().model_dump()["dimensions"]
    dims[0]["column"] = 'we"ird'
    names = ['we"ird' if n == "region" else n for n in small_orders().column_names]
    runner = MetricQueryRunner(
        AnalyticalEngine(), semantic_model(dimensions=dims), small_orders().rename_columns(names)
    )
    rows = runner.run(req(metric="Revenue", dimensions=["Region"])).table.rows
    assert rows == [["North", 445.0], ["South", 440.0]]


@pytest.mark.parametrize(
    "expr",
    [
        "sum(measure('Quantity')); DROP TABLE data",
        "sum(read_csv('x'))",
        "getenv('HOME')",
        "sum(measure('Quantity')) -- comment",
        "sum(measure('a\\' OR 1=1'))",
        "sum(sum(measure('Quantity')))",
        "measure('Quantity')",
        "__import__('os')",
        'sum(measure("Quantity"))',
        "sum(measure('Quantity')) " + "+ 1 " * 600,
        "(" * 40 + "1" + ")" * 40,
    ],
)
def test_hostile_metric_expressions_are_rejected(expr: str) -> None:
    with pytest.raises(SchemaError):
        parse_metric(expr, semantic_model())


def test_cell_values_cannot_carry_sql() -> None:
    hostile = pa.array(["'; DROP TABLE data; --"] * small_orders().num_rows)
    data = small_orders().set_column(2, "region", hostile)
    runner = MetricQueryRunner(AnalyticalEngine(), semantic_model(), data)
    rows = runner.run(req(metric="Quantity", dimensions=["Region"])).table.rows
    assert rows == [["'; DROP TABLE data; --", 11]]
