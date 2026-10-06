"""Pack metric-template instantiation (TASK-SEM-007; design 15 step 6).

A template becomes a ``SemanticMetric`` only when every concept it ``requires`` is mapped to a
column of this model AND its expression type-checks against the model's real measures (units and
additivity from the data, not the pack). So "Gross Margin" is never proposed without a mapped unit
cost, and a template that sums a column the data shows is non-additive is rejected.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.data.domain_packs.model import DomainPack
from app.data.semantic.domain import SemanticMetric
from app.data.semantic.expression import ExpressionError, parse, references
from app.data.semantic.typecheck import DictCatalog, Symbol, check_metric


@dataclass(frozen=True)
class MetricResult:
    metrics: list[SemanticMetric]
    derived_from: dict[str, list[tuple[str, str]]]  # metric -> [(ref kind, name)]
    skipped: dict[str, str]  # "pack:metric" -> reason


def instantiate_metrics(
    packs: Sequence[DomainPack],
    mapped_concepts: frozenset[str],
    symbols: Mapping[tuple[str, str], Symbol],
) -> MetricResult:
    catalog = dict(symbols)
    metrics: list[SemanticMetric] = []
    derived: dict[str, list[tuple[str, str]]] = {}
    skipped: dict[str, str] = {}
    for pack in packs:
        for template in pack.metrics:
            key = f"{pack.id}:{template.name}"
            if template.name in derived:
                skipped[key] = "a higher-priority pack already defines this metric"
                continue
            missing = sorted(set(template.requires) - mapped_concepts)
            if missing:
                skipped[key] = f"required concepts not mapped: {missing}"
                continue
            try:
                result = check_metric(template.expr, DictCatalog(catalog))
            except ExpressionError as exc:
                skipped[key] = f"type check failed: {exc.message}"
                continue
            catalog[("metric", template.name)] = Symbol("number", result.unit, result.additivity)
            derived[template.name] = sorted(references(parse(template.expr)))
            metrics.append(
                SemanticMetric(
                    name=template.name,
                    expr=template.expr,
                    unit=template.unit or result.unit,
                    direction=template.direction,
                    additivity=result.additivity,
                    source=pack.ref,
                )
            )
    return MetricResult(metrics, derived, skipped)
