"""Semantic model contract (TASK-SEM-001; TRD 13.1 + 11.4, design 15-16).

Shape follows TRD 13.1 plus the design 16 ``graph``. Metric/rule ``expr`` stays an opaque string
here; its grammar is TASK-SEM-002. Enforced: unique names per kind, time dimensions carry a grain,
a non-additive measure never defaults to ``sum`` (TRD 11.4), graph edges reference known nodes.
"""

from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.data.domain import Ratio
from app.data.profiling.domain import TimeGrain
from app.data.understanding.domain import Additivity

Aggregation = Literal[
    "sum", "avg", "weighted_avg", "min", "max", "count", "count_distinct", "median", "last"
]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SemanticEntity(Contract):
    name: str
    key: str
    source_table: str
    confidence: Ratio


class SemanticDimension(Contract):
    name: str
    column: str
    type: Literal["category", "time", "geo", "boolean"]
    entity: str | None = None
    grain: TimeGrain | None = None


class SemanticMeasure(Contract):
    name: str
    column: str
    unit: str | None  # "currency:INR", "pct", None = unitless
    additivity: Additivity
    default_agg: Aggregation
    confidence: Ratio
    aliases: list[str] = []


class SemanticMetric(Contract):
    name: str
    expr: str
    unit: str | None = None
    direction: Literal["higher_is_better", "lower_is_better", "neutral"] | None = None
    additivity: Additivity | None = None
    source: str | None = None  # e.g. "pack:retail@1.2"


class SemanticRule(Contract):
    name: str
    expr: str


class GraphNode(Contract):
    id: str
    type: Literal["entity", "measure", "dimension", "metric"]
    name: str


class GraphEdge(Contract):
    model_config = ConfigDict(
        validate_by_name=True, validate_by_alias=True, serialize_by_alias=True
    )

    from_: Annotated[str, Field(alias="from")]
    to: str
    type: Literal[
        "places",
        "contains",
        "belongs_to",
        "references",
        "measured_on",
        "attribute_of",
        "derived_from",
    ]
    confidence: Ratio
    evidence: str


class SemanticGraph(Contract):
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []


class Override(Contract):
    """One user correction, re-applied after every (re-)mapping (TASK-SEM-009).

    ``field`` paths (app.data.overrides): ``mapping.<column>`` (value = concept name, confirms
    the column's mapping), ``measures.<name>.default_agg`` / ``measures.<name>.unit``, and
    ``metrics.<name>.expr`` (value = metric expression; adds or replaces the metric).
    """

    field: Annotated[str, Field(min_length=1, max_length=300)]
    value: Annotated[str, Field(max_length=2000)] | None  # None = remove (unmap / drop metric)
    by: str
    at: AwareDatetime


class SemanticModel(Contract):
    schema_version: Literal["1.0"] = "1.0"
    semantic_model_id: Annotated[str, Field(pattern=r"^sm_\w+$")]
    dataset_version_id: Annotated[str, Field(pattern=r"^dsv_\w+$")]
    version: Annotated[int, Field(ge=1)]
    entities: list[SemanticEntity]
    dimensions: list[SemanticDimension]
    measures: list[SemanticMeasure]
    metrics: list[SemanticMetric]
    rules: list[SemanticRule] = []
    graph: SemanticGraph = SemanticGraph()
    overrides: list[Override] = []

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        groups: dict[str, list[str]] = {
            "entities": [e.name for e in self.entities],
            "dimensions": [d.name for d in self.dimensions],
            "measures": [m.name for m in self.measures],
            "metrics": [m.name for m in self.metrics],
            "rules": [r.name for r in self.rules],
        }
        for kind, names in groups.items():
            if len(names) != len(set(names)):
                raise ValueError(f"{kind} names must be unique")
        for d in self.dimensions:
            if (d.type == "time") != (d.grain is not None):
                raise ValueError(f"dimension {d.name!r}: grain is required exactly for time")
        for m in self.measures:
            if m.additivity == "non_additive" and m.default_agg == "sum":
                raise ValueError(f"measure {m.name!r}: non-additive measure cannot default to sum")
        node_ids = [n.id for n in self.graph.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("graph node ids must be unique")
        if any(e.from_ not in node_ids or e.to not in node_ids for e in self.graph.edges):
            raise ValueError("graph edges must reference existing nodes")
        return self
