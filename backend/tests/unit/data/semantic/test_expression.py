"""Metric expression parser: valid, invalid, malicious and fuzzed input (TASK-SEM-003)."""

import builtins
import random
from decimal import Decimal

import pytest

from app.data.semantic.expression import (
    MAX_DEPTH,
    MAX_LENGTH,
    Binary,
    Call,
    ExpressionError,
    Number,
    Ref,
    Unary,
    parse,
    references,
)


def test_precedence_and_structure() -> None:
    node = parse("sum(measure('Quantity') * measure('Unit Price')) / count() - 1")
    assert isinstance(node, Binary)
    assert node.op == "-"
    assert node.right == Number(Decimal(1))
    assert isinstance(node.left, Binary)
    assert node.left.op == "/"
    assert node.left.right == Call("count", ())


def test_references_and_unary_and_comparison() -> None:
    node = parse("days_since(max(dimension('Order Date'))) <= 90")
    assert isinstance(node, Binary)
    assert node.op == "<="
    assert references(node) == {("dimension", "Order Date")}
    assert parse("-measure('x')") == Unary("-", Ref("measure", "x"))
    assert parse("  sum( measure( 'Sales Amount' ) )  ") == Call(
        "sum", (Ref("measure", "Sales Amount"),)
    )


@pytest.mark.parametrize(
    "expr",
    [
        "",
        "sum(",
        "sum(measure('x')))",
        "sum(measure('x'), measure('y'))",  # wrong arity
        "weighted_avg(measure('x'))",
        "measure(x)",  # reference needs a string literal
        "measure('  ')",
        "median()",
        "1 < 2 < 3",
        "1 +",
        "sum measure('x')",
        "(1 + 2",
        "1 2",
        "SUM(measure('x'))",  # identifiers are lowercase only
    ],
)
def test_invalid_expressions_rejected(expr: str) -> None:
    with pytest.raises(ExpressionError):
        parse(expr)


@pytest.mark.parametrize(
    "expr",
    [
        "__import__('os').system('rm -rf /')",
        "eval('1')",
        "exec('x')",
        "measure('x').__class__",
        "measure('x')[0]",
        "sum(measure('x')); drop table users",
        "sum(measure('x')) -- comment",
        "sum(measure('x')) # comment",
        'sum(measure("x"))',
        "measure('a\\'b')",
        "measure('x\ny')",
        "lambda: 1",
        "sum(measure('x')) or 1",
        "select * from secrets",
        "sum(measure(x=1))",
        "{}",
        "`id`",
    ],
)
def test_malicious_expressions_rejected(expr: str) -> None:
    with pytest.raises(ExpressionError):
        parse(expr)


def test_limits() -> None:
    with pytest.raises(ExpressionError, match="longer"):
        parse("1+" * MAX_LENGTH + "1")
    deep = "(" * (MAX_DEPTH + 5) + "1" + ")" * (MAX_DEPTH + 5)
    with pytest.raises(ExpressionError, match="deeper"):
        parse(deep)
    with pytest.raises(ExpressionError, match="deeper"):
        parse("-" * (MAX_DEPTH + 5) + "1")


def test_error_reports_position() -> None:
    with pytest.raises(ExpressionError) as info:
        parse("sum(measure('x')) ; 1")
    assert info.value.position == 18
    assert info.value.details == {"reason": "INVALID_EXPRESSION", "position": 18}


def test_fuzz_never_executes_or_crashes(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("parser must never evaluate code")

    monkeypatch.setattr(builtins, "eval", forbidden)
    monkeypatch.setattr(builtins, "exec", forbidden)
    monkeypatch.setattr(builtins, "compile", forbidden)
    alphabet = "sumavgcountmeasure()'+-*/<>=!,._ 0123456789xyz;\"[]{}#\\\n"
    rng = random.Random(1234)  # noqa: S311  # deterministic fuzz, not crypto
    for _ in range(3000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        try:
            parse(text)
        except ExpressionError:
            continue
