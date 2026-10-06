"""Metric expression parser (TASK-SEM-003; grammar: docs/metric-grammar.md, TASK-SEM-002).

Hand-written tokenizer + recursive-descent parser producing an immutable AST. Nothing is ever
evaluated or executed here: no ``eval``/``exec``/``compile``, no attribute access, no imports.
Anything outside the grammar (unknown characters, unknown functions, wrong arity, too long, too
deep) raises ``ExpressionError`` with the character position.
"""

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, cast

from app.core.errors import SchemaError

MAX_LENGTH = 2000
MAX_TOKENS = 512
MAX_DEPTH = 32

RefKind = Literal["measure", "metric", "dimension", "dimension_key"]
REFERENCES: frozenset[str] = frozenset({"measure", "metric", "dimension", "dimension_key"})
# name -> (min args, max args)
FUNCTIONS: dict[str, tuple[int, int]] = {
    "sum": (1, 1),
    "avg": (1, 1),
    "min": (1, 1),
    "max": (1, 1),
    "median": (1, 1),
    "last": (1, 1),
    "count": (0, 1),
    "count_distinct": (1, 1),
    "weighted_avg": (2, 2),
    "days_since": (1, 1),
}
COMPARISON = frozenset({"<", "<=", ">", ">=", "==", "!="})
_ADD = frozenset({"+", "-"})
_MUL = frozenset({"*", "/"})

_TOKEN = re.compile(
    r"""(?P<ws>[ \t]+)
      | (?P<number>\d+(?:\.\d+)?)
      | (?P<string>'[^'\\\n\r]{1,128}')
      | (?P<ident>[a-z_][a-z0-9_]*)
      | (?P<op><=|>=|==|!=|[-+*/(),<>])""",
    re.VERBOSE,
)


class ExpressionError(SchemaError):
    def __init__(self, message: str, position: int | None = None) -> None:
        details: dict[str, object] = {"reason": "INVALID_EXPRESSION"}
        if position is not None:
            details["position"] = position
        super().__init__(message, details=details)
        self.position = position


@dataclass(frozen=True)
class Number:
    value: Decimal


@dataclass(frozen=True)
class Ref:
    kind: RefKind
    name: str


@dataclass(frozen=True)
class Call:
    func: str
    args: tuple[Node, ...]


@dataclass(frozen=True)
class Unary:
    op: Literal["-"]
    operand: Node


@dataclass(frozen=True)
class Binary:
    op: str  # + - * / or a comparison operator
    left: Node
    right: Node


type Node = Number | Ref | Call | Unary | Binary


@dataclass(frozen=True)
class _Tok:
    kind: str
    text: str
    pos: int


def tokenize(text: str) -> list[_Tok]:
    if len(text) > MAX_LENGTH:
        raise ExpressionError(f"expression longer than {MAX_LENGTH} characters")
    out: list[_Tok] = []
    pos = 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if m is None or m.lastgroup is None:
            raise ExpressionError(f"unexpected character {text[pos]!r}", pos)
        if m.lastgroup != "ws":
            out.append(_Tok(m.lastgroup, m.group(), pos))
        pos = m.end()
    if len(out) > MAX_TOKENS:
        raise ExpressionError(f"expression has more than {MAX_TOKENS} tokens")
    out.append(_Tok("end", "", len(text)))
    return out


def parse(text: str) -> Node:
    parser = _Parser(tokenize(text))
    node = parser.comparison()
    parser.expect("end")
    return node


def references(node: Node) -> set[tuple[str, str]]:
    """Every ``(kind, name)`` reference in the tree."""
    match node:
        case Ref(kind, name):
            return {(kind, name)}
        case Call(_, args):
            found: set[tuple[str, str]] = set()
            for arg in args:
                found |= references(arg)
            return found
        case Unary(_, operand):
            return references(operand)
        case Binary(_, left, right):
            return references(left) | references(right)
        case _:
            return set()


class _Parser:
    def __init__(self, toks: list[_Tok]) -> None:
        self._toks = toks
        self._i = 0
        self._depth = 0

    @property
    def cur(self) -> _Tok:
        return self._toks[self._i]

    def take(self) -> _Tok:
        tok = self.cur
        self._i += 1
        return tok

    def expect(self, kind: str, text: str | None = None) -> _Tok:
        tok = self.cur
        if tok.kind != kind or (text is not None and tok.text != text):
            wanted = text or kind
            raise ExpressionError(f"expected {wanted!r}, found {tok.text or 'end'!r}", tok.pos)
        return self.take()

    def _is_op(self, ops: frozenset[str]) -> bool:
        return self.cur.kind == "op" and self.cur.text in ops

    def _enter(self) -> None:
        self._depth += 1
        if self._depth > MAX_DEPTH:
            raise ExpressionError(f"expression nested deeper than {MAX_DEPTH}", self.cur.pos)

    def comparison(self) -> Node:
        left = self.additive()
        if self._is_op(COMPARISON):
            op = self.take().text
            left = Binary(op, left, self.additive())
            if self._is_op(COMPARISON):
                raise ExpressionError("chained comparisons are not allowed", self.cur.pos)
        return left

    def additive(self) -> Node:
        node = self.term()
        while self._is_op(_ADD):
            op = self.take().text
            node = Binary(op, node, self.term())
        return node

    def term(self) -> Node:
        node = self.unary()
        while self._is_op(_MUL):
            op = self.take().text
            node = Binary(op, node, self.unary())
        return node

    def unary(self) -> Node:
        if self._is_op(frozenset({"-"})):
            self.take()
            self._enter()
            node = Unary("-", self.unary())
            self._depth -= 1
            return node
        return self.primary()

    def primary(self) -> Node:
        tok = self.cur
        if tok.kind == "number":
            self.take()
            return Number(Decimal(tok.text))
        if tok.kind == "op" and tok.text == "(":
            self.take()
            self._enter()
            node = self.additive()
            self._depth -= 1
            self.expect("op", ")")
            return node
        if tok.kind == "ident":
            return self.call()
        raise ExpressionError(f"unexpected {tok.text or 'end'!r}", tok.pos)

    def call(self) -> Node:
        name_tok = self.take()
        name = name_tok.text
        if name not in REFERENCES and name not in FUNCTIONS:
            raise ExpressionError(f"unknown function {name!r}", name_tok.pos)
        self.expect("op", "(")
        if name in REFERENCES:
            literal = self.expect("string")
            self.expect("op", ")")
            ref_name = literal.text[1:-1].strip()
            if not ref_name:
                raise ExpressionError("empty reference name", literal.pos)
            return Ref(cast(RefKind, name), ref_name)
        self._enter()
        args: list[Node] = []
        if not self._is_op(frozenset({")"})):
            args.append(self.additive())
            while self._is_op(frozenset({","})):
                self.take()
                args.append(self.additive())
        self._depth -= 1
        self.expect("op", ")")
        low, high = FUNCTIONS[name]
        if not low <= len(args) <= high:
            raise ExpressionError(f"{name}() takes {low}..{high} arguments", name_tok.pos)
        return Call(name, tuple(args))
