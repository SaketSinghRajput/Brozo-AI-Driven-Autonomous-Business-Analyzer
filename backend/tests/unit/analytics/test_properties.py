"""TASK-TEST-009: property-based tests (hypothesis) for analytics invariants and metric parsers.

- contribution_to_change: member deltas sum to the total change, which equals after - before;
  defined shares (fractions, TRD 25.1) sum to 1, undefined shares are all None.
- growth: percent change has the sign of (current - base) and is undefined exactly when the base
  is 0 or a value is missing; percentage points are current - base.
- rank: competition rank = 1 + number of strictly better members, ties share a rank, dense ranks
  have no gaps, row ranks are 1..n, ``tied`` <=> the value occurs more than once, top-N keeps
  every member tied at the cutoff.
- metric expressions: the semantic grammar parser and the compiler's entry point (the same parser
  plus the type-checker, DEBT-006) never crash: any text either parses or raises SchemaError; text
  with a character outside the grammar's alphabet is always rejected; expressions generated from
  the grammar always parse; ``count()`` is accepted by both.
"""

import contextlib
import math
import string

from hypothesis import given, settings
from hypothesis import strategies as st

from app.analytics.services.contribution import contribution_to_change
from app.analytics.services.growth import pct_change, pp_change
from app.analytics.services.ranking import rank
from app.analytics.sql.compiler import parse_metric
from app.core.errors import SchemaError
from app.data.semantic import expression as semantic_expr
from tests.unit.analytics.conftest import semantic_model

MODEL = semantic_model()

settings.register_profile("ci", deadline=None, max_examples=200, derandomize=True)
settings.load_profile("ci")

money = st.floats(min_value=-1e9, max_value=1e9, allow_nan=False, allow_infinity=False)
keys = st.text(alphabet=string.ascii_letters, min_size=1, max_size=6)
members = st.dictionaries(keys, st.one_of(st.none(), money), max_size=12)


@given(members, members)
def test_contributions_sum_to_total_change(
    before: dict[str, float | None], after: dict[str, float | None]
) -> None:
    result = contribution_to_change(before, after)
    scale = 1.0 + sum(abs(i.before) + abs(i.after) for i in result.items)
    deltas = sum(i.delta for i in result.items)
    assert math.isclose(deltas, result.total_delta, abs_tol=1e-9 * scale)
    assert math.isclose(
        result.total_after - result.total_before, result.total_delta, abs_tol=1e-9 * scale
    )
    seen = {k for d in (before, after) for k, v in d.items() if v is not None}
    assert {i.key for i in result.items} == seen
    if result.undefined is not None:
        assert all(i.share_pct is None for i in result.items)
    elif abs(result.total_delta) > 1e-6 * scale:
        total = sum(i.share_pct or 0.0 for i in result.items)
        assert math.isclose(total, 1.0, abs_tol=1e-6 * scale / abs(result.total_delta))


@given(st.one_of(st.none(), money), st.one_of(st.none(), money))
def test_growth_sign_and_undefined_cases(base: float | None, current: float | None) -> None:
    pct = pct_change(base, current)
    if base is None or current is None:
        assert (pct.value, pct.undefined) == (None, "MISSING_VALUE")
        return
    if base == 0:
        assert (pct.value, pct.undefined) == (None, "BASE_IS_ZERO")
    else:
        assert pct.value is not None
        assert pct.undefined is None
        assert (pct.value > 0) == (current > base)
        assert (pct.value < 0) == (current < base)
    assert pp_change(base, current).value == current - base


small = st.one_of(st.none(), st.integers(-5, 5).map(float))


@given(st.lists(st.tuples(keys, small), max_size=15), st.booleans())
def test_ranking_ties(items: list[tuple[str, float | None]], descending: bool) -> None:
    values = [v for _, v in items if v is not None]
    competition = rank(items, "competition", descending)
    assert competition.excluded_nulls == len(items) - len(values)
    for item in competition.items:
        better = sum(1 for v in values if (v > item.value if descending else v < item.value))
        assert item.rank == 1 + better
        assert item.tied == (values.count(item.value) > 1)
    dense = [i.rank for i in rank(items, "dense", descending).items]
    assert sorted(set(dense)) == list(range(1, len(set(values)) + 1))
    rows = [i.rank for i in rank(items, "row", descending).items]
    assert rows == list(range(1, len(values) + 1))


@given(
    st.lists(st.tuples(keys, st.integers(-3, 3).map(float)), min_size=1, max_size=12),
    st.integers(1, 5),
)
def test_top_n_keeps_every_member_tied_at_the_cutoff(
    items: list[tuple[str, float]], top_n: int
) -> None:
    full = rank(items).items
    assert rank(items, top_n=top_n).items == [i for i in full if i.rank <= top_n]


def _compiler_parse(text: str) -> object:
    return parse_metric(text, MODEL)


PARSERS = (semantic_expr.parse, _compiler_parse)


@given(st.text(max_size=200))
def test_parsers_never_crash(text: str) -> None:
    for parse in PARSERS:
        with contextlib.suppress(SchemaError):
            parse(text)


OUTSIDE = st.sampled_from([*'"`;{}[]$#@!%^&|~=:?\\\n', "é", "\u0000", chr(0xFF1B)])
INSIDE = st.text(alphabet=string.ascii_lowercase + "()' ", max_size=30)


@given(INSIDE, OUTSIDE, st.text(max_size=30))
def test_text_outside_the_alphabet_is_rejected(prefix: str, bad: str, suffix: str) -> None:
    text = prefix + bad + suffix
    for parse in PARSERS:
        try:
            parse(text)
        except SchemaError:
            continue
        raise AssertionError(f"{parse.__module__} accepted {text!r}")


names = st.sampled_from(["Quantity", "Unit Price", "Revenue", "Order"])
leaf = st.one_of(
    st.integers(0, 999).map(str),
    names.map(lambda n: f"sum(measure('{n}'))"),
    names.map(lambda n: f"count_distinct(dimension_key('{n}'))"),
    names.map(lambda n: f"metric('{n}')"),
)
grammar = st.recursive(
    leaf,
    lambda inner: st.one_of(
        st.tuples(inner, st.sampled_from(["+", "-", "*", "/"]), inner).map(
            lambda t: f"{t[0]} {t[1]} {t[2]}"
        ),
        inner.map(lambda e: f"({e})"),
    ),
    max_leaves=8,
)


@given(grammar)
def test_expressions_from_the_grammar_parse(text: str) -> None:
    semantic_expr.parse(text)  # the compiler parses with this same parser (DEBT-006)


def test_both_parsers_accept_count_star() -> None:
    for parse in PARSERS:
        parse("count()")
