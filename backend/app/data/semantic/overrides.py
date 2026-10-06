"""Semantic overrides: re-map with confirmations, then apply user corrections last
(TASK-SEM-009, TASK-SEM-010; design 15 step 7: "user overrides applied last and always win").

Supported ``Override.field`` paths (value is always a string; ``None`` only drops a metric):
- ``mapping.<column>``          confirm the column's concept; fed to the mapper as ``prior``
- ``measures.<name>.default_agg`` / ``measures.<name>.unit``
- ``metrics.<name>.expr``       add/replace a metric (type-checked) or drop it (value None)
The effective override list is carried to every new model version and to later dataset versions
(semantic memory per dataset). An override that no longer applies (column gone, concept no longer
allowed for the column's type, expression no longer type-checks) is dropped with a
``OVERRIDE_STALE:<field>`` review flag when re-applied by the pipeline, and rejected when it is
submitted (``strict``).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import get_args

from pydantic import ValidationError

from app.core.errors import AppValidationError
from app.data.semantic.domain import Aggregation, Override, SemanticMetric, SemanticModel
from app.data.semantic.expression import ExpressionError
from app.data.semantic.mapper import SemanticMapper
from app.data.semantic.typecheck import DictCatalog, Symbol, check_metric
from app.data.understanding.domain import DomainClassification, Understanding

MAPPING, MEASURES, METRICS = "mapping.", "measures.", "metrics."
_MEASURE_ATTRS = (".default_agg", ".unit")
_AGGREGATIONS = frozenset(get_args(Aggregation))


class OverrideError(AppValidationError):
    """An override that cannot be applied to this semantic model."""


@dataclass(frozen=True)
class Remapped:
    model: SemanticModel
    review_flags: list[str]


def prior_mappings(overrides: Sequence[Override]) -> dict[str, str]:
    return {
        o.field.removeprefix(MAPPING): o.value
        for o in overrides
        if o.field.startswith(MAPPING) and o.value is not None
    }


def remap(
    mapper: SemanticMapper,
    understanding: Understanding,
    domain: DomainClassification,
    *,
    table: str,
    overrides: Sequence[Override],
    previous: SemanticModel | None = None,
    strict: bool = False,
) -> Remapped:
    """Map (confirmed mappings win), then apply the remaining overrides in order."""
    result = mapper.map(
        understanding, domain, table=table, prior=prior_mappings(overrides), previous=previous
    )
    mapped = {m.column: m.concept for m in result.mappings}
    model = result.model
    kept: list[Override] = []
    stale: list[str] = []
    for override in overrides:
        try:
            model = _apply(model, override, mapped)
        except OverrideError:
            if strict:
                raise
            stale.append(override.field)
            continue
        kept.append(override)
    model = _rebuild(model, overrides=kept)
    flags = result.review_flags + [f"OVERRIDE_STALE:{f}" for f in stale]
    return Remapped(model, flags)


def _apply(
    model: SemanticModel, override: Override, mapped: Mapping[str, str | None]
) -> SemanticModel:
    field, value = override.field, override.value
    if field.startswith(MAPPING):
        column = field.removeprefix(MAPPING)
        if value is None or mapped.get(column) != value:
            raise OverrideError(
                f"{field}: column {column!r} cannot be mapped to concept {value!r} "
                "(unknown column, or the concept does not allow the column's type)"
            )
        return model
    if field.startswith(METRICS) and field.endswith(".expr"):
        return _metric(model, field.removeprefix(METRICS).removesuffix(".expr"), value)
    if field.startswith(MEASURES) and field.endswith(_MEASURE_ATTRS):
        name, _, attr = field.removeprefix(MEASURES).rpartition(".")
        return _measure(model, name, attr, value)
    raise OverrideError(f"{field}: unsupported override field")


def _measure(model: SemanticModel, name: str, attr: str, value: str | None) -> SemanticModel:
    if name not in {m.name for m in model.measures}:
        raise OverrideError(f"measures.{name}: unknown measure")
    if attr == "default_agg" and value not in _AGGREGATIONS:
        raise OverrideError(f"measures.{name}.default_agg: must be one of {sorted(_AGGREGATIONS)}")
    measures = [
        {**m.model_dump(), attr: value} if m.name == name else m.model_dump()
        for m in model.measures
    ]
    return _rebuild(model, measures=measures)


def _metric(model: SemanticModel, name: str, expr: str | None) -> SemanticModel:
    metrics = [m.model_dump() for m in model.metrics if m.name != name]
    if expr is not None:
        if not name.strip():
            raise OverrideError("metrics.<name>.expr: the metric needs a name")
        try:
            typed = check_metric(expr, DictCatalog(model_symbols(model, exclude_metric=name)))
        except ExpressionError as exc:
            raise OverrideError(f"metrics.{name}.expr: {exc.message}") from None
        metric = SemanticMetric(
            name=name, expr=expr, unit=typed.unit, additivity=typed.additivity, source="user"
        )
        metrics.append(metric.model_dump())
    return _rebuild(model, metrics=metrics)


def _rebuild(model: SemanticModel, **changes: object) -> SemanticModel:
    try:
        return SemanticModel.model_validate({**model.model_dump(by_alias=True), **changes})
    except ValidationError as exc:
        message = exc.errors()[0]["msg"]
        raise OverrideError(f"the override makes the semantic model invalid: {message}") from None


def model_symbols(
    model: SemanticModel, exclude_metric: str | None = None
) -> dict[tuple[str, str], Symbol]:
    """The expression catalog of a model (same symbol kinds as the mapper builds)."""
    symbols = {("measure", m.name): Symbol("number", m.unit, m.additivity) for m in model.measures}
    symbols |= {("dimension_key", e.name): Symbol("key") for e in model.entities}
    for d in model.dimensions:
        symbols[("dimension", d.name)] = Symbol("time" if d.type == "time" else "category")
    for metric in model.metrics:
        if metric.name != exclude_metric:
            symbols[("metric", metric.name)] = Symbol("number", metric.unit, metric.additivity)
    return symbols
