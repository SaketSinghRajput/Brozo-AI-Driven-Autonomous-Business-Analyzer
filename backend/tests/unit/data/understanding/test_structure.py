"""Entity, relationship, time, measure, dimension detectors and units (SCHEMA-005..009, SEM-004)."""

from collections.abc import Collection, Sequence

import pytest

from app.data.profiling.domain import ProfileResult
from app.data.understanding.dimensions import DimensionDetector
from app.data.understanding.domain import ColumnUnderstanding
from app.data.understanding.domain import SemanticType as T
from app.data.understanding.entities import EntityDetector, entity_name
from app.data.understanding.measures import MetricDetector, classify_measure
from app.data.understanding.relationships import RelationshipDetector, TableSnapshot
from app.data.understanding.semantic_types import SemanticTypeClassifier
from app.data.understanding.time_dimensions import TimeDimensionDetector
from app.data.understanding.units import detect_unit
from tests.unit.data.understanding.factories import (
    col,
    key,
    numeric,
    orders_profile,
    profile,
    temporal,
    text,
    top,
)

CLF = SemanticTypeClassifier()


class _Counter:
    def __init__(self, unique_combo: Sequence[str]) -> None:
        self.combo = set(unique_combo)

    def count_distinct(self, columns: Sequence[str]) -> int:
        return 360 if set(columns) == self.combo else 100


def _typed(p: ProfileResult) -> list[ColumnUnderstanding]:
    return CLF.classify(p).columns


def _snapshot(name: str, p: ProfileResult, values: dict[str, Collection[str]]) -> TableSnapshot:
    return TableSnapshot(name, p, _typed(p), lambda c: frozenset(values[c]))


def test_entity_names() -> None:
    assert entity_name("customer_id", "orders") == "Customer"
    assert entity_name("id", "customers") == "Customer"
    assert entity_name("id", "data") == "Record"


def test_primary_and_referenced_entities() -> None:
    p = orders_profile()
    entities = EntityDetector().detect("orders", p, _typed(p))
    assert [(e.name, e.key_columns) for e in entities] == [
        ("Order", ["order_id"]),
        ("Customer", ["customer_id"]),
        ("Product", ["product_id"]),
    ]
    assert entities[0].confidence == 1.0
    assert entities[1].confidence == 0.6


def test_composite_key_needs_counter() -> None:
    p = profile(
        col("farm_id", unique=0.08, card=30, text=text(mixed=1.0)),
        col("year", "int", card=6, numeric=numeric(2019, 2024)),
        col("season", card=2),
        col("area_ha", "float", unique=0.5, card=180),
        rows=360,
    )
    columns = _typed(p)
    without = EntityDetector().detect("production", p, columns)
    assert all(len(e.key_columns) == 1 for e in without)
    counter = _Counter(["farm_id", "year", "season"])
    with_counter = EntityDetector().detect("production", p, columns, counter)
    assert ["farm_id", "year", "season"] in [e.key_columns for e in with_counter]


def test_foreign_key_found_with_evidence() -> None:
    customers = profile(key("customer_id", 50), rows=50)
    orders = profile(key("order_id"), col("customer_id", unique=0.05, card=50, text=text(1.0)))
    ids = {f"C{i:03d}" for i in range(50)}
    src = _snapshot("orders", orders, {"customer_id": ids, "order_id": set()})
    dst = _snapshot("customers", customers, {"customer_id": ids | {"C999"}})
    findings = RelationshipDetector().detect(src, [src, dst])
    assert len(findings) == 1
    rel = findings[0].relationship
    assert (rel.from_, rel.to, rel.cardinality, rel.inclusion) == (
        "orders.customer_id", "customers.customer_id", "many_to_one", 1.0,
    )  # fmt: skip
    assert any("inclusion" in e for e in findings[0].evidence)


def test_no_false_join_on_unrelated_integer_ids() -> None:
    a = profile(col("id", "int", unique=1.0, card=100), col("score", "int"), rows=100)
    b = profile(col("id", "int", unique=1.0, card=100), rows=100)
    ints = {str(i) for i in range(100)}
    src = _snapshot("scores", a, {"id": ints, "score": ints})
    dst = _snapshot("people", b, {"id": ints})
    found = RelationshipDetector().detect(src, [dst])
    assert all(f.relationship.from_ != "scores.score" for f in found)


def test_low_inclusion_is_not_a_relationship() -> None:
    customers = profile(key("customer_id", 50), rows=50)
    orders = profile(col("customer_id", unique=0.05, card=50, text=text(1.0)))
    src = _snapshot("orders", orders, {"customer_id": {f"X{i}" for i in range(50)}})
    dst = _snapshot("customers", customers, {"customer_id": {f"C{i}" for i in range(50)}})
    assert RelationshipDetector().detect(src, [dst]) == []


def test_time_dimensions_ranked_and_none_when_absent() -> None:
    p = profile(
        col("end_date", "date", nulls=0.4, card=300, temporal=temporal("day", 0.3)),
        col("start_date", "date", card=500, temporal=temporal("day", 0.9)),
        col("year", "int", card=6, numeric=numeric(2019, 2024)),
    )
    dims = TimeDimensionDetector().detect(p, _typed(p))
    order = [d.column for d in dims]
    assert order.index("start_date") < order.index("end_date")  # secondary name + nulls rank lower
    assert {d.column: d.grain for d in dims}["year"] == "year"
    no_time = profile(col("amount", "float", unique=0.9, card=900))
    assert TimeDimensionDetector().detect(no_time, _typed(no_time)) == []


@pytest.mark.parametrize(
    ("header", "sem", "expected"),
    [
        ("sales", T.CURRENCY_AMOUNT, ("amount", "additive")),
        ("mrr", T.CURRENCY_AMOUNT, ("balance", "semi_additive")),
        ("stock_level", T.QUANTITY, ("balance", "semi_additive")),
        ("clicks", T.COUNT, ("count", "additive")),
        ("unit_price", T.PRICE, ("price", "non_additive")),
        ("unit_cost", T.PRICE, ("unit_cost", "non_additive")),
        ("conversion_rate", T.RATE_OR_RATIO, ("rate", "non_additive")),
        ("discount_pct", T.PERCENTAGE, ("percentage", "non_additive")),
        ("region", T.CATEGORY, None),
    ],
)
def test_additivity_table(header: str, sem: T, expected: tuple[str, str] | None) -> None:
    assert classify_measure(header, sem) == expected


def test_measures_skip_keys_and_carry_units() -> None:
    p = orders_profile()
    measures = MetricDetector().detect(p, _typed(p), frozenset({"order_id"}), "INR")
    assert {m.column: m.unit for m in measures} == {
        "quantity": "count",
        "unit_price": "currency:INR",
    }


@pytest.mark.parametrize(
    ("header", "sem", "default", "unit"),
    [
        ("discount_pct", T.PERCENTAGE, None, "pct"),
        ("revenue_usd", T.CURRENCY_AMOUNT, "INR", "currency:USD"),
        ("revenue", T.CURRENCY_AMOUNT, "INR", "currency:INR"),
        ("revenue", T.CURRENCY_AMOUNT, None, "currency"),
        ("market_price_per_t", T.PRICE, "INR", "currency:INR/tonne"),
        ("yield_t_per_ha", T.RATE_OR_RATIO, None, "t/ha"),
        ("area_ha", T.QUANTITY, None, "hectare"),
        ("quantity", T.QUANTITY, None, "count"),
        ("region", T.CATEGORY, None, None),
    ],
)
def test_unit_detection(header: str, sem: T, default: str | None, unit: str | None) -> None:
    assert detect_unit(header, sem, None, default) == unit


def test_currency_symbol_in_values() -> None:
    column = col("price", categorical=top("₹120", "₹99"))
    assert detect_unit("price", T.PRICE, column) == "currency:INR"


def test_dimensions_bands_and_types() -> None:
    p = profile(
        key("order_id"),
        col("customer_id", unique=0.2, card=200, text=text(mixed=1.0)),
        col("region", card=4),
        col("is_active", "bool", card=2),
        col("city", card=1500, unique=0.5),
        rows=3000,
    )
    dims = DimensionDetector().detect(p, _typed(p), frozenset({"order_id"}))
    got = {d.column: (d.type, d.cardinality_band) for d in dims}
    assert got["customer_id"] == ("identifier", "mid")
    assert got["region"] == ("category", "low")
    assert got["is_active"] == ("boolean", "low")
    assert got["city"] == ("geo", "high")
    assert "order_id" not in got
