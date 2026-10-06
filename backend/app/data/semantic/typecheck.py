"""Unit + additivity type-checker for metric expressions (TASK-SEM-003; TRD 13.2).

Types carry a kind (number / bool / time / key / category), a level (const / row / agg), a unit and
an additivity. Rules (docs/metric-grammar.md section "Typing"):
- aggregations take row-level input and return agg; nesting aggregations is an error;
- ``sum(measure(x))`` of a non-additive measure is an error (TRD 11.4);
- arithmetic may not mix row-level and aggregated operands;
- ``+``/``-``/comparisons need equal units (a bare number adapts); ``count`` is dimensionless for
  ``*``/``/`` so currency / count -> currency;
- a metric must be a number at agg level; a rule must be a bool.
"""

from dataclasses import dataclass
from typing import Literal, Protocol

from app.data.semantic.expression import (
    COMPARISON,
    Binary,
    Call,
    ExpressionError,
    Node,
    Number,
    Ref,
    Unary,
    parse,
)
from app.data.understanding.domain import Additivity

Kind = Literal["number", "bool", "time", "key", "category"]
Level = Literal["const", "row", "agg"]
_DIMENSIONLESS = frozenset({None, "count"})


@dataclass(frozen=True)
class Symbol:
    kind: Kind  # what a reference evaluates to
    unit: str | None = None
    additivity: Additivity | None = None


@dataclass(frozen=True)
class ExprType:
    kind: Kind
    level: Level
    unit: str | None = None
    additivity: Additivity | None = None


class Catalog(Protocol):
    def lookup(self, ref_kind: str, name: str) -> Symbol | None: ...


class DictCatalog:
    """Catalog backed by ``{(ref_kind, name): Symbol}``."""

    def __init__(self, symbols: dict[tuple[str, str], Symbol]) -> None:
        self._symbols = symbols

    def lookup(self, ref_kind: str, name: str) -> Symbol | None:
        return self._symbols.get((ref_kind, name))


def check_metric(expr: str | Node, catalog: Catalog) -> ExprType:
    result = infer(parse(expr) if isinstance(expr, str) else expr, catalog)
    if result.kind != "number" or result.level != "agg":
        raise ExpressionError("a metric must be an aggregated number (e.g. sum(measure('x')))")
    return result


def check_rule(expr: str | Node, catalog: Catalog) -> ExprType:
    result = infer(parse(expr) if isinstance(expr, str) else expr, catalog)
    if result.kind != "bool":
        raise ExpressionError("a rule must be a comparison")
    return result


def infer(node: Node, catalog: Catalog) -> ExprType:
    match node:
        case Number():
            return ExprType("number", "const")
        case Ref(kind, name):
            symbol = catalog.lookup(kind, name)
            if symbol is None:
                raise ExpressionError(f"unknown {kind} {name!r}")
            level: Level = "agg" if kind == "metric" else "row"
            return ExprType(symbol.kind, level, symbol.unit, symbol.additivity)
        case Unary(_, operand):
            return _number(infer(operand, catalog), "unary minus")
        case Call(func, args):
            return _call(func, [infer(a, catalog) for a in args], args)
        case Binary(op, left, right):
            return _binary(op, infer(left, catalog), infer(right, catalog))
    raise ExpressionError("unsupported expression node")  # pragma: no cover


def _number(t: ExprType, where: str) -> ExprType:
    if t.kind != "number":
        raise ExpressionError(f"{where} needs a number, got {t.kind}")
    return t


def _row(t: ExprType, func: str) -> ExprType:
    if t.level == "agg":
        raise ExpressionError(f"{func}() cannot take an aggregated value (nested aggregation)")
    return t


def _call(func: str, args: list[ExprType], nodes: tuple[Node, ...]) -> ExprType:
    if func == "count":
        if args:
            _row(args[0], func)
        return ExprType("number", "agg", None, "additive")
    if func == "count_distinct":
        _row(args[0], func)
        return ExprType("number", "agg", None, "non_additive")
    if func == "days_since":
        if args[0].kind != "time":
            raise ExpressionError("days_since() needs a time value")
        return ExprType("number", args[0].level, "day", "non_additive")
    if func == "weighted_avg":
        _number(_row(args[1], func), func)
        value = _number(_row(args[0], func), func)
        return ExprType("number", "agg", value.unit, "non_additive")
    arg = _row(args[0], func)
    if func in {"min", "max", "last"} and arg.kind == "time":
        return ExprType("time", "agg")
    _number(arg, f"{func}()")
    if func == "sum":
        if isinstance(nodes[0], Ref) and arg.additivity == "non_additive":
            raise ExpressionError(f"sum() of non-additive measure {nodes[0].name!r}")
        semi = arg.additivity == "semi_additive"
        return ExprType("number", "agg", arg.unit, "semi_additive" if semi else "additive")
    return ExprType("number", "agg", arg.unit, "non_additive")  # avg, min, max, median, last


def _level(a: ExprType, b: ExprType, op: str) -> Level:
    levels = {a.level, b.level} - {"const"}
    if len(levels) > 1:
        raise ExpressionError(f"'{op}' mixes row-level and aggregated values")
    return levels.pop() if levels else "const"


def _binary(op: str, a: ExprType, b: ExprType) -> ExprType:
    level = _level(a, b, op)
    if op in COMPARISON:
        if a.kind != b.kind or a.kind not in {"number", "time"}:
            raise ExpressionError(f"cannot compare {a.kind} with {b.kind}")
        _same_unit(a, b, op)
        return ExprType("bool", level)
    _number(a, f"'{op}'")
    _number(b, f"'{op}'")
    if op in {"+", "-"}:
        unit = _same_unit(a, b, op)
        additive = a.additivity == b.additivity == "additive" or level == "row"
        return ExprType("number", level, unit, "additive" if additive else "non_additive")
    unit = _mul_unit(a.unit, b.unit) if op == "*" else _div_unit(a.unit, b.unit)
    return ExprType("number", level, unit, "additive" if level == "row" else "non_additive")


def _same_unit(a: ExprType, b: ExprType, op: str) -> str | None:
    if a.level == "const" or a.unit is None:
        return b.unit
    if b.level == "const" or b.unit is None or a.unit == b.unit:
        return a.unit
    raise ExpressionError(f"'{op}' on incompatible units {a.unit!r} and {b.unit!r}")


def _mul_unit(a: str | None, b: str | None) -> str | None:
    if a in _DIMENSIONLESS or a == "pct":
        return b
    if b in _DIMENSIONLESS or b == "pct":
        return a
    for x, y in ((a, b), (b, a)):  # tonne * currency:INR/tonne -> currency:INR
        if x is not None and y is not None and y.endswith(f"/{x}"):
            return y[: -len(x) - 1]
    return f"{a}*{b}"


def _div_unit(a: str | None, b: str | None) -> str | None:
    if b in _DIMENSIONLESS:
        return a
    if a == b:
        return None
    return f"1/{b}" if a in _DIMENSIONLESS else f"{a}/{b}"
