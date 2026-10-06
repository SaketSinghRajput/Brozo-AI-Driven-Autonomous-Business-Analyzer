"""Dashboard and chart contracts (TASK-DASH-001; design 27-28, TRD 30.1).

Every widget is either evidence-bound or ``unavailable``, and the types enforce this: each
evidence-bearing widget variant has required evidence references, and the only variant without
them is ``UnavailableWidget``, which must carry a ``reason_code``.

Deviations from TRD 30.1:
- ``schema_version`` replaces ``spec_version``, so every contract uses the TRD 55 field name.
- Chart widgets embed a design-27 ``ChartSpec`` (which carries ``evidence_id``) instead of
  inlining x/y/data_ref. The frontend renders only ``ChartSpec`` (design 27).
- P0 widget variants only: kpi_card, the chart widgets, and unavailable. ``segment_summary``,
  ``insight_card`` (P1) and ``evidence_panel`` are added together with their producers.

Version 1.1 (minor, backward compatible; readers accept 1.0 and 1.1; TASK-DASH-005):
- ``FormatSpec.currency`` and ``KpiCardWidget.format``: KPI format hints (unit, precision,
  currency). ``unit`` uses the evidence unit vocabulary: ``currency:<ISO>`` (``currency`` then
  carries the ISO code), ``pct`` and ``pp`` are FRACTIONS rendered x100 (TRD 25.1: 0.142 is
  14.2%, 0.023 is 2.3 pp), ``count``, any other string is a plain unit label.
- ``EvidenceRef.field`` semantics: a dotted path into the referenced Evidence record, resolved
  exactly as the validator resolves ``NumberSegment.field`` (app.validator.numerical.resolve):
  dict keys, list indexes as digits. Producers emit ``result.value`` (the headline number);
  ``result.rows`` is the only other numeric path in use. The value is read from evidence, never
  copied into the spec.
- ``query`` on KPI and chart widgets: the ``MetricRequest`` behind a ``analytics.metric_query``
  widget, which ``FilterBinder`` re-runs through ``POST /api/v1/analytics/query`` with the
  dashboard's active filters appended (new evidence). None: the widget is not filterable.
- Row data of chart widgets: ``GET /api/v1/evidence/{chart.evidence_id}/data``.
- ``insights`` and ``summary``: typed, evidence-bound text blocks with their validation status.
"""

from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.analytics.domain import MetricRequest
from app.planner.domain import Filter, ReasonCode
from app.validator.domain import ValidationStatus

EvidenceId = Annotated[str, Field(pattern=r"^ev_\w+$")]
ChartType = Literal["kpi", "line", "bar", "stacked_bar", "area", "scatter", "table", "heatmap"]
SpecVersion = Literal["1.0", "1.1"]  # minor versions a 1.x reader accepts


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FieldRef(Contract):
    field: str
    type: Literal["time", "category", "numeric"] | None = None
    unit: str | None = None  # "currency:INR", "pct"


class Overlay(Contract):
    type: Literal["forecast_band", "anomaly_markers"]
    evidence_id: EvidenceId


class FormatSpec(Contract):
    """Fixed formatting vocabulary; the frontend never evaluates format strings (design 57)."""

    unit: str | None = None  # evidence unit vocabulary (module docstring)
    precision: Annotated[int, Field(ge=0, le=6)] = 0
    # U-1: one app-wide number locale, en-US grouping (1,318,451.67), as the backend's text
    # (Python "{:,}") and frontend/src/lib/format.ts APP_LOCALE; the frontend ignores the field.
    locale: str = "en-US"
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None = None  # ISO 4217 (1.1)


class ChartSpec(Contract):
    schema_version: SpecVersion = "1.1"
    type: ChartType
    x: FieldRef | None = None
    y: Annotated[list[FieldRef], Field(min_length=1)]
    series: FieldRef | None = None
    aggregation: Literal["sum", "avg", "count", "count_distinct", "min", "max", "median", "none"]
    filters: list[Filter] = []
    source_operation: str  # "<plan>-<step>", e.g. "A123-S1"
    evidence_id: EvidenceId
    overlays: list[Overlay] = []
    format: FormatSpec = FormatSpec()

    @model_validator(mode="after")
    def _axes(self) -> Self:
        if self.type in {"line", "area", "bar", "stacked_bar", "scatter", "heatmap"} and not self.x:
            raise ValueError(f"{self.type} charts need an x field")
        if self.type == "kpi" and (self.x or self.series):
            raise ValueError("kpi charts have no x or series")
        return self


class EvidenceRef(Contract):
    evidence_id: EvidenceId
    field: str  # dotted path into the Evidence record, e.g. "result.value" (module docstring)


class ComparisonRef(EvidenceRef):
    label: str  # e.g. "vs Jul"


class KpiCardWidget(Contract):
    id: str
    widget_type: Literal["kpi_card"]
    title: str
    metric: str
    value_ref: EvidenceRef
    comparison_ref: ComparisonRef | None = None
    validation_status: ValidationStatus
    operation_id: str
    format: FormatSpec | None = None  # 1.1
    query: MetricRequest | None = None  # 1.1: re-run with dashboard filters (FilterBinder)
    # 1.1, optional (GAPS-5d): the value is mathematically undefined; show this text instead of
    # a number, e.g. "Undefined (base is zero)" for growth from a zero base.
    undefined_reason: str | None = None


_CHART_FOR_WIDGET = {
    "line_chart": "line",
    "bar_chart": "bar",
    "stacked_bar": "stacked_bar",
    "area_chart": "area",
    "scatter": "scatter",
    "table": "table",
    "heatmap": "heatmap",
}


class ChartWidget(Contract):
    id: str
    widget_type: Literal[
        "line_chart", "bar_chart", "stacked_bar", "area_chart", "scatter", "table", "heatmap"
    ]
    title: str
    chart: ChartSpec
    validation_status: ValidationStatus
    operation_id: str
    query: MetricRequest | None = None  # 1.1: re-run with dashboard filters (FilterBinder)

    @model_validator(mode="after")
    def _matching_chart(self) -> Self:
        if _CHART_FOR_WIDGET[self.widget_type] != self.chart.type:
            raise ValueError(f"widget {self.widget_type} cannot render chart {self.chart.type}")
        return self


class UnavailableWidget(Contract):
    id: str
    widget_type: Literal["unavailable"]
    title: str
    reason_code: ReasonCode
    message: str
    would_enable: str | None = None


Widget = Annotated[
    KpiCardWidget | ChartWidget | UnavailableWidget, Field(discriminator="widget_type")
]


class DashboardFilter(Contract):
    id: str
    dimension: str
    type: Literal["multi_select", "single_select", "date_range"]


class LayoutItem(Contract):
    id: str
    x: Annotated[int, Field(ge=0)]
    y: Annotated[int, Field(ge=0)]
    w: Annotated[int, Field(ge=1)]
    h: Annotated[int, Field(ge=1)]


class Layout(Contract):
    columns: Literal[12] = 12
    items: list[LayoutItem]

    @model_validator(mode="after")
    def _fits(self) -> Self:
        if any(i.x + i.w > self.columns for i in self.items):
            raise ValueError("layout items must fit within the grid columns")
        return self


class EvidencedText(Contract):
    text: str  # plain text; numbers in it are checked against evidence_ids by the validator
    evidence_ids: Annotated[list[EvidenceId], Field(min_length=1)]


class Insight(Contract):
    """What was observed (a calculation) kept apart from possible explanations (contributions or
    associations, never causes). Only VALID insights are shown (PRD-DASH-006)."""

    id: str
    observed: EvidencedText
    possible_explanations: list[EvidencedText] = []
    validation_status: ValidationStatus


class SummarySentence(EvidencedText):
    validation_status: ValidationStatus


class DashboardSpec(Contract):
    schema_version: SpecVersion = "1.1"
    dashboard_id: Annotated[str, Field(pattern=r"^db_\w+$")]
    dataset_version_id: Annotated[str, Field(pattern=r"^dsv_\w+$")]
    plan_id: Annotated[str, Field(pattern=r"^pl_\w+$")]
    generated_at: AwareDatetime
    filters: list[DashboardFilter] = []
    widgets: list[Widget]
    layout: Layout
    insights: list[Insight] = []  # 1.1
    summary: Annotated[list[SummarySentence], Field(max_length=3)] = []  # 1.1: executive summary
    # 1.1, optional (GAPS-5b): set when serving the last READY dashboard while a newer semantic
    # model version is being re-planned; the UI shows "updating". Never stored as true.
    stale: bool = False

    @model_validator(mode="after")
    def _references(self) -> Self:
        widget_ids = [w.id for w in self.widgets]
        if len(widget_ids) != len(set(widget_ids)):
            raise ValueError("widget ids must be unique")
        laid_out = [i.id for i in self.layout.items]
        if len(laid_out) != len(set(laid_out)) or set(laid_out) - set(widget_ids):
            raise ValueError("layout items must reference each existing widget at most once")
        return self
