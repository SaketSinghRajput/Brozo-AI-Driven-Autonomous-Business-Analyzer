"""QueryCompiler (TASK-SQL-004, design §21, TRD §15.1/§16).

MetricRequest → metric AST (``app.data.semantic.expression``, type-checked) → ``Select`` (a small
SQL AST) → parameterized SQL.

Injection rules (verified by the TASK-SQL-008 corpus):
- Identifiers come only from the semantic model (columns, entity keys) after the caller's names
  were resolved by ``SemanticResolver``; they are always double-quoted with quotes escaped. Output
  aliases are semantic names (quoted the same way) or fixed words.
- Every caller-supplied value (filter values, dates) is a named bind parameter (``$p0``...).
- The only literals rendered into SQL are finite numbers from the metric AST, ``LIMIT`` (an int
  validated by the contract) and date_trunc parts from a fixed allow-list.

Joins: a dataset version is one Parquet file registered as the single view ``data`` (design
§10), so every semantic column lives in that view. The graph-based ``JoinPathResolver`` is
TASK-SEM-008 and is needed only once multi-table dataset versions exist.
"""

import math
from dataclasses import dataclass, field
from datetime import date

from app.analytics.domain import ColumnRole, MetricRequest
from app.analytics.sql.engine import Param
from app.analytics.sql.periods import comparison_range
from app.analytics.sql.resolver import SemanticResolver
from app.core.errors import SchemaError
from app.data.semantic.domain import SemanticMeasure, SemanticModel
from app.data.semantic.expression import Binary, Call, Node, Number, Ref, Unary, parse
from app.data.semantic.typecheck import Symbol, check_metric
from app.planner.domain import Filter

DATA_VIEW = "data"
MAX_METRIC_DEPTH = 8
# Aggregations the SQL engine renders; the grammar's others (weighted_avg, last, days_since)
# are refused until the compiler supports them.
AGGREGATIONS = frozenset({"sum", "avg", "min", "max", "count", "count_distinct", "median"})
_AGG_SQL = {
    "sum": "SUM",
    "avg": "AVG",
    "min": "MIN",
    "max": "MAX",
    "count": "COUNT",
    "median": "MEDIAN",  # exact in DuckDB
}
_DATE_GRAINS = {g: g for g in ("day", "week", "month", "quarter", "year")}
_TIME_GRAINS = {g: g for g in ("hour", "minute", "second")}
_COMPARISONS = {"eq": "=", "gte": ">=", "lte": "<="}


def quote(identifier: str) -> str:
    if not identifier or len(identifier) > 255 or "\x00" in identifier:
        raise SchemaError("invalid identifier in the semantic model")
    return '"' + identifier.replace('"', '""') + '"'


@dataclass(frozen=True, slots=True)
class OutputColumn:
    name: str
    role: ColumnRole
    unit: str | None = None


@dataclass(frozen=True, slots=True)
class CompiledQuery:
    sql: str
    params: dict[str, Param]
    columns: tuple[OutputColumn, ...]
    limit: int  # rows the caller asked for; the SQL fetches one more to detect truncation
    warnings: tuple[str, ...] = ()
    current_range: tuple[date, date] | None = None
    compare_range: tuple[date, date] | None = None

    def json_params(self) -> dict[str, str | int | float | bool | None]:
        return {k: v.isoformat() if isinstance(v, date) else v for k, v in self.params.items()}


@dataclass
class Select:
    """The SQL AST: one aggregate SELECT over one registered view."""

    items: list[tuple[str, str]]  # (rendered expression, output alias)
    source: str
    where: list[str] = field(default_factory=list)
    group_count: int = 0  # the first N items are grouping keys
    limit: int = 1

    def render(self) -> str:
        cols = ", ".join(f"{expr} AS {quote(alias)}" for expr, alias in self.items)
        sql = f"SELECT {cols} FROM {quote(self.source)}"  # noqa: S608  # quoted allow-listed parts
        if self.where:
            sql += " WHERE " + " AND ".join(f"({w})" for w in self.where)
        if self.group_count:
            keys = ", ".join(str(i) for i in range(1, self.group_count + 1))
            sql += f" GROUP BY {keys} ORDER BY {keys}"
        return f"{sql} LIMIT {int(self.limit)}"


class _ModelCatalog:
    """The type-checker's symbol table over a semantic model (names resolved like requests)."""

    def __init__(self, resolver: SemanticResolver) -> None:
        self._r = resolver

    def lookup(self, ref_kind: str, name: str) -> Symbol | None:
        try:
            if ref_kind == "measure":
                m = self._r.measure(name)
                return Symbol("number", m.unit, m.additivity)
            if ref_kind == "dimension":
                d = self._r.dimension(name)
                return Symbol("time" if d.type == "time" else "category")
            if ref_kind == "dimension_key":
                self._r.entity(name)
                return Symbol("key")
            target = self._r.metric(name)  # a metric, or a measure at its default aggregation
        except SchemaError:
            return None
        return Symbol("number", target.unit, target.additivity)


def _supported(node: Node) -> None:
    match node:
        case Call(func=func, args=args):
            if func not in AGGREGATIONS:
                raise SchemaError(f"{func}() is not supported by the analytics engine yet")
            for arg in args:
                _supported(arg)
        case Unary(operand=operand):
            _supported(operand)
        case Binary(op=op, left=left, right=right):
            if op not in {"+", "-", "*", "/"}:
                raise SchemaError("comparisons are not allowed in a metric")
            _supported(left)
            _supported(right)


def parse_metric(expr: str, model: SemanticModel) -> Node:
    """The one metric-expression path (DEBT-006): the semantic grammar parser
    (app.data.semantic.expression) + its type-checker against this model, then the subset the
    SQL engine renders. Every failure is a ``SchemaError``."""
    node = parse(expr)
    check_metric(node, _ModelCatalog(SemanticResolver(model)))
    _supported(node)
    return node


class _Params:
    def __init__(self) -> None:
        self.values: dict[str, Param] = {}

    def add(self, value: Param) -> str:
        name = f"p{len(self.values)}"
        self.values[name] = value
        return f"${name}"


class QueryCompiler:
    def __init__(
        self,
        model: SemanticModel,
        view: str = DATA_VIEW,
        year_columns: frozenset[str] = frozenset(),  # integer columns holding a year
    ) -> None:
        self._r = SemanticResolver(model)
        self._view = view
        self._year_columns = year_columns

    def _day(self, column: str) -> str:
        """A time column as a DATE. Integer year columns (2024) become Jan 1 of that year: DuckDB
        cannot CAST BIGINT AS DATE (B2)."""
        if column in self._year_columns:
            return f"make_date(CAST({quote(column)} AS INTEGER), 1, 1)"
        return f"CAST({quote(column)} AS DATE)"

    def compile(self, req: MetricRequest) -> CompiledQuery:
        params, warnings = _Params(), list[str]()
        ast, unit = self._metric_ast(req.metric)
        items: list[tuple[str, str]] = []
        outputs: list[OutputColumn] = []
        for name in req.dimensions:
            dim = self._r.dimension(name)
            items.append((quote(dim.column), dim.name))
            outputs.append(OutputColumn(dim.name, "dimension"))
        where: list[str] = []
        current = compare = None
        current_range = compare_range = None
        if req.time is not None:
            time_column = self._r.time_dimension(req.time_dimension).column
            day = self._day(time_column)
            column = day if time_column in self._year_columns else quote(time_column)
            start, end = req.time.start, req.time.end
            if req.compare_to is not None and start is not None and end is not None:
                current_range = (start, end)
                compare_range = comparison_range(start, end, req.compare_to)
                current = f"{day} BETWEEN {params.add(start)} AND {params.add(end)}"
                low, high = compare_range
                compare = f"{day} BETWEEN {params.add(low)} AND {params.add(high)}"
                where.append(f"{current} OR {compare}")
            else:
                if start is not None:
                    where.append(f"{day} >= {params.add(start)}")
                if end is not None:
                    where.append(f"{day} <= {params.add(end)}")
                items.append((self._truncate(column, req.time.grain), "period"))
                outputs.append(OutputColumn("period", "period"))
        elif req.time_dimension is not None:
            raise SchemaError("time_dimension needs a time spec")
        where.extend(self._filter(f, params) for f in req.filters)
        group_count = len(items)
        items.append((self._render(ast, current, warnings, ()), "value"))
        outputs.append(OutputColumn("value", "value", unit))
        if compare is not None:
            items.append((self._render(ast, compare, warnings, ()), "compare_value"))
            outputs.append(OutputColumn("compare_value", "compare_value", unit))
        aliases = [alias for _, alias in items]
        if len(set(aliases)) != len(aliases):
            raise SchemaError("output column names collide; a dimension is named like a result")
        select = Select(items, self._view, where, group_count, req.limit + 1)
        return CompiledQuery(
            sql=select.render(),
            params=params.values,
            columns=tuple(outputs),
            limit=req.limit,
            warnings=tuple(dict.fromkeys(warnings)),
            current_range=current_range,
            compare_range=compare_range,
        )

    # -- metric AST ---------------------------------------------------------------------------

    def _metric_ast(self, name: str) -> tuple[Node, str | None]:
        target = self._r.metric(name)
        if isinstance(target, SemanticMeasure):
            return self._measure_ast(target), self._measure_unit(target)
        ast = self._parse(target.expr)
        return ast, target.unit if target.unit is not None else self._infer_unit(ast, ())

    def _parse(self, expr: str) -> Node:
        return parse_metric(expr, self._r.model)

    @staticmethod
    def _measure_ast(measure: SemanticMeasure) -> Node:
        if measure.default_agg not in AGGREGATIONS:
            raise SchemaError(f"aggregation {measure.default_agg!r} is not supported yet")
        return Call(measure.default_agg, (Ref("measure", measure.name),))

    @staticmethod
    def _measure_unit(measure: SemanticMeasure) -> str | None:
        return "count" if measure.default_agg in {"count", "count_distinct"} else measure.unit

    def _infer_unit(self, node: Node, stack: tuple[str, ...]) -> str | None:
        match node:
            case Call(func="count" | "count_distinct"):
                return "count"
            case Call(args=(Ref(kind="measure", name=name),)):
                return self._r.measure(name).unit
            case Ref(kind="metric", name=name) if len(stack) < MAX_METRIC_DEPTH:
                target = self._r.metric(name)
                if isinstance(target, SemanticMeasure):
                    return self._measure_unit(target)
                return target.unit or self._infer_unit(self._parse(target.expr), (*stack, name))
            case Binary(op="+" | "-", left=left, right=right):
                unit = self._infer_unit(left, stack)
                return unit if unit == self._infer_unit(right, stack) else None
            case Unary(operand=operand):
                return self._infer_unit(operand, stack)
        return None

    def _render(
        self, node: Node, pred: str | None, warnings: list[str], stack: tuple[str, ...]
    ) -> str:
        match node:
            case Number(value=value):
                number = float(value)
                if not math.isfinite(number):
                    raise SchemaError("non-finite number in metric expression")
                return repr(number)
            case Ref(kind="metric", name=name):
                target = self._r.metric(name)
                if target.name in stack or len(stack) >= MAX_METRIC_DEPTH:
                    raise SchemaError("metric definitions are circular or nested too deeply")
                if isinstance(target, SemanticMeasure):
                    return self._render(self._measure_ast(target), pred, warnings, stack)
                ast = self._parse(target.expr)
                return self._render(ast, pred, warnings, (*stack, target.name))
            case Ref():
                return quote(self._column(node))
            case Call(func="count", args=()):  # count(): rows (in the period, when compared)
                return "COUNT(*)" if pred is None else f"COUNT(CASE WHEN {pred} THEN 1 END)"
            case Call(func=func, args=(arg,)):
                self._check_additivity(func, arg, warnings)
                inner = self._render(arg, None, warnings, stack)
                if pred is not None:
                    inner = f"CASE WHEN {pred} THEN {inner} END"
                if func == "count_distinct":
                    return f"COUNT(DISTINCT {inner})"
                return f"{_AGG_SQL[func]}({inner})"
            case Binary(op=op, left=left, right=right):
                lhs = self._render(left, pred, warnings, stack)
                rhs = self._render(right, pred, warnings, stack)
                return f"({lhs} / NULLIF({rhs}, 0))" if op == "/" else f"({lhs} {op} {rhs})"
            case Unary(operand=operand):
                return f"(-{self._render(operand, pred, warnings, stack)})"
        raise SchemaError("unsupported metric expression node")

    def _check_additivity(self, func: str, arg: Node, warnings: list[str]) -> None:
        if func != "sum" or not (isinstance(arg, Ref) and arg.kind == "measure"):
            return
        measure = self._r.measure(arg.name)
        if measure.additivity == "non_additive":
            raise SchemaError(f"measure {measure.name!r} is non-additive and cannot be summed")
        if measure.additivity == "semi_additive":
            warnings.append(
                f"{measure.name} is semi-additive: sums are valid within one snapshot period only"
            )

    def _column(self, ref: Ref) -> str:
        if ref.kind == "measure":
            return self._r.measure(ref.name).column
        if ref.kind == "dimension":
            return self._r.dimension(ref.name).column
        return self._r.entity(ref.name).key

    # -- filters and time ---------------------------------------------------------------------

    @staticmethod
    def _truncate(column: str, grain: str) -> str:
        if grain in _DATE_GRAINS:
            return f"CAST(date_trunc('{_DATE_GRAINS[grain]}', {column}) AS DATE)"
        return f"date_trunc('{_TIME_GRAINS[grain]}', {column})"

    def _filter(self, flt: Filter, params: _Params) -> str:
        dim = self._r.dimension(flt.dimension)
        column = self._day(dim.column) if dim.type == "time" else quote(dim.column)
        value = flt.value
        if flt.op == "in" and isinstance(value, list):
            return f"{column} IN ({', '.join(params.add(v) for v in value)})"
        if flt.op == "between" and isinstance(value, list):
            return f"{column} BETWEEN {params.add(value[0])} AND {params.add(value[1])}"
        if isinstance(value, list):
            raise SchemaError("filter operator needs a single value")
        return f"{column} {_COMPARISONS[flt.op]} {params.add(value)}"
