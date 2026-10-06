"""SemanticMapper (SEM-006), template instantiation (SEM-007), graph + join paths (SEM-008)."""

from collections.abc import Mapping

import pytest

from app.core.errors import SchemaError
from app.data.profiling.domain import ProfileResult
from app.data.semantic.domain import GraphEdge, GraphNode, SemanticGraph, SemanticModel
from app.data.semantic.graph import JoinPathResolver
from app.data.semantic.mapper import MappingResult, SemanticMapper
from app.data.understanding.analyzer import SchemaAnalyzer, TableInput
from app.data.understanding.domain import Understanding
from app.data.understanding.domain_detector import DomainDetector
from tests.unit.data.understanding.factories import col, key, numeric, orders_profile, profile

MAPPER = SemanticMapper()


def _understand(p: ProfileResult) -> Understanding:
    tables = [TableInput("orders", p)]
    return SchemaAnalyzer().analyze(tables, default_currency="INR")["orders"].understanding


def _map(
    p: ProfileResult,
    prior: Mapping[str, str] | None = None,
    previous: SemanticModel | None = None,
) -> MappingResult:
    u = _understand(p)
    domain = DomainDetector().detect([u])
    return MAPPER.map(u, domain, table="orders", prior=prior, previous=previous)


def test_retail_orders_map_to_pack_concepts_and_metrics() -> None:
    result = _map(orders_profile())
    concepts = {m.column: m.concept for m in result.mappings}
    assert concepts["unit_price"] == "Unit Price"
    assert concepts["order_id"] == "Order"
    metrics = {m.name: m for m in result.model.metrics}
    assert metrics["Revenue"].unit == "currency:INR"
    assert metrics["Revenue"].source == "pack:retail@1.0.2"
    assert "Average Order Value" in metrics
    assert "Gross Margin" not in metrics  # no unit cost mapped -> never proposed
    assert "retail:Gross Margin" in result.skipped_metrics
    assert result.review_flags == []


def test_gross_margin_proposed_when_cost_is_mapped() -> None:
    cost = col("unit_cost", "float", unique=0.5, card=500, numeric=numeric(1, 400))
    base = orders_profile()
    p = base.model_copy(update={"columns": [*base.columns, cost]})
    margin = next(m for m in _map(p).model.metrics if m.name == "Gross Margin")
    assert margin.unit == "pct"
    assert "100" not in margin.expr  # pct is a fraction (TRD 25.1): no x100 in the formula


def test_conflicting_columns_are_ambiguous_and_block_metrics() -> None:
    p = profile(
        key("order_id"),
        col("quantity", "int", numeric=numeric(1, 10)),
        col("net_sales", "float", unique=0.9, card=900),
        col("sales_value", "float", unique=0.9, card=900),
        col("unit_price", "float", unique=0.5, card=500, numeric=numeric(5, 500)),
    )
    result = _map(p)
    flags = set(result.review_flags)
    assert {"MAPPING_AMBIGUOUS:net_sales", "MAPPING_AMBIGUOUS:sales_value"} <= flags
    mapped = {m.column: m.concept for m in result.mappings}
    assert mapped["net_sales"] is None
    assert mapped["sales_value"] is None
    assert "Total Sales" not in {m.name for m in result.model.metrics}


def test_prior_confirmation_resolves_conflict() -> None:
    p = profile(
        key("order_id"),
        col("net_sales", "float", unique=0.9, card=900),
        col("sales_value", "float", unique=0.9, card=900),
    )
    u = _understand(p)
    domain = DomainDetector().override("retail", by="usr_1")
    result = MAPPER.map(u, domain, table="orders", prior={"net_sales": "Sales Amount"})
    mapped = {m.column: m.concept for m in result.mappings}
    assert mapped["net_sales"] == "Sales Amount"
    assert "MAPPING_AMBIGUOUS:net_sales" not in result.review_flags


def test_unknown_domain_uses_generic_pack_only() -> None:
    p = profile(key("id"), col("amount", "float", unique=0.9, card=900), col("date", "date"))
    result = _map(p)
    assert result.review_flags[0] == "DOMAIN_UNCERTAIN"
    assert {m.pack for m in result.mappings if m.pack} == {"generic"}
    assert all((m.source or "").startswith("pack:generic") for m in result.model.metrics)


def test_version_increments_and_model_is_deterministic() -> None:
    first = _map(orders_profile())
    assert _map(orders_profile()).model == first.model
    second = _map(orders_profile(), previous=first.model)
    assert second.model.version == 2
    assert second.model.semantic_model_id != first.model.semantic_model_id


def test_graph_and_join_path_on_mapped_model() -> None:
    graph = _map(orders_profile()).model.graph
    types = {(e.from_, e.to, e.type) for e in graph.edges}
    assert ("entity:Customer", "entity:Order", "places") in types
    assert ("metric:Revenue", "measure:Unit Price", "derived_from") in types
    resolver = JoinPathResolver(graph)
    assert [e.type for e in resolver.path("Customer", "Product")] == ["places", "contains"]
    assert resolver.path("Order", "Order") == []


def _graph(*edges: tuple[str, str]) -> SemanticGraph:
    names = sorted({n for e in edges for n in e} | {"Island"})
    return SemanticGraph(
        nodes=[GraphNode(id=f"entity:{n}", type="entity", name=n) for n in names],
        edges=[
            GraphEdge(from_=f"entity:{a}", to=f"entity:{b}", type="references", confidence=1.0,
                      evidence="test")
            for a, b in edges
        ],
    )  # fmt: skip


def test_ambiguous_and_missing_join_paths_raise() -> None:
    diamond = JoinPathResolver(_graph(("A", "B"), ("A", "C"), ("B", "D"), ("C", "D")))
    with pytest.raises(SchemaError) as ambiguous:
        diamond.path("A", "D")
    assert ambiguous.value.details == {"reason": "AMBIGUOUS_JOIN_PATH"}
    with pytest.raises(SchemaError) as missing:
        diamond.path("A", "Island")
    assert missing.value.details == {"reason": "NO_JOIN_PATH"}
    with pytest.raises(SchemaError, match="unknown entity"):
        diamond.path("A", "Nowhere")
