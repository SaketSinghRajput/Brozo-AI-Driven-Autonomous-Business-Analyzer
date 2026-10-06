"""SemanticTypeClassifier + lexicon (TASK-SCHEMA-003/004)."""

import pytest

from app.data.profiling.domain import ColumnProfile
from app.data.understanding.domain import SemanticType as T
from app.data.understanding.semantic_types import SemanticTypeClassifier, default_lexicon
from tests.unit.data.understanding.factories import col, key, numeric, profile, text, top

CLF = SemanticTypeClassifier()


def _types(*columns: ColumnProfile) -> dict[str, T]:
    return {c.name: c.semantic_type for c in CLF.classify(profile(*columns)).columns}


def test_lexicon_covers_every_semantic_type() -> None:
    assert set(default_lexicon().types) == set(T) - {T.UNKNOWN}


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("unit_cost", T.PRICE),  # phrase consumes "cost" -> not currency_amount
        ("market_price_per_t", T.PRICE),  # "per" + price token
        ("yield_t_per_ha", T.RATE_OR_RATIO),  # denominators after "per" do not vote
        ("is_active", T.BOOLEAN_FLAG),
        ("CustomerID", T.IDENTIFIER),
        ("total_amt", T.CURRENCY_AMOUNT),
    ],
)
def test_name_rules(header: str, expected: T) -> None:
    scores = CLF.name_scores(header)
    assert max(scores, key=lambda t: scores[t]) is expected


def test_typical_columns() -> None:
    got = _types(
        key("order_id"),
        col("order_date", "date"),
        col("quantity", "int", numeric=numeric(1, 10)),
        col("unit_price", "float", unique=0.5, card=500, numeric=numeric(5, 500)),
        col("discount_pct", "float", numeric=numeric(0, 30)),
        col("year", "int", card=6, numeric=numeric(2019, 2024)),
        col("crop", card=4),
        col("email", unique=0.9, card=900, text=text(email=1.0)),
        col("country", card=3, categorical=top("India", "Germany", "Japan")),
    )
    assert got == {
        "order_id": T.IDENTIFIER,
        "order_date": T.DATETIME,
        "quantity": T.QUANTITY,
        "unit_price": T.PRICE,
        "discount_pct": T.PERCENTAGE,
        "year": T.DATE_PART,
        "crop": T.CATEGORY,
        "email": T.EMAIL,
        "country": T.GEO_COUNTRY,
    }


def test_meaningless_numeric_column_abstains_with_flag() -> None:
    result = CLF.classify(profile(col("x", "float", unique=0.9, card=900)))
    column = result.columns[0]
    assert column.semantic_type is T.UNKNOWN
    assert result.review_flags == ["TYPE_UNCERTAIN:x"]
    assert any("abstained" in s for s in column.signals)


def test_every_decision_carries_confidence_and_signals() -> None:
    result = CLF.classify(profile(key("id"), col("amount", "float", unique=0.9, card=900)))
    for c in result.columns:
        assert 0 < c.confidence <= 1
        assert c.signals[0].startswith("top=")


def test_classification_is_deterministic() -> None:
    p = profile(key("id"), col("value", "float", unique=0.9, card=900), col("kind", card=3))
    assert CLF.classify(p) == CLF.classify(p)
