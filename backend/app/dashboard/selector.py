"""WidgetSelector + Prioritizer (TASK-DASH-003, design §28).

WidgetSelector turns each executed plan step into at most one widget, through the
WidgetRegistry (what the tool's output can show) and the ChartSelector (what the rows' shape
suggests): a single number becomes a KPI card, a series a line chart, a small breakdown a bar
chart, anything else a table. Forecast / anomaly results are drawn as overlays on the trend
chart of the same metric when there is one. A step the tool refused (UNAVAILABLE) and every
dashboard analysis the capability report rules out become ``unavailable`` widgets carrying the
reason code and message; so does a failed or skipped step (ANALYSIS_FAILED, GAPS-5c). A result
that is mathematically undefined (growth from a zero base) is a KPI card carrying
``undefined_reason`` ("Undefined (base is zero)") instead of a number (GAPS-5d).

Prioritizer orders KPI cards by the metric's importance in its domain pack (the pack lists its
KPI templates most important first), then generic-pack metrics, then raw measures. When the
domain is UNKNOWN or AMBIGUOUS only generic metrics are kept (``MetricFilter``), so the
dashboard is never domain-specific (CLAUDE.md §11).

Every widget is built with status UNCERTAIN; DashboardService sets the validator's result.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from app.analytics.domain import MetricRequest
from app.dashboard.charts import ChartSelector, chart_spec, format_spec
from app.dashboard.domain import (
    ChartWidget,
    EvidenceRef,
    KpiCardWidget,
    Overlay,
    UnavailableWidget,
    Widget,
)
from app.dashboard.registry import WidgetRegistry
from app.data.domain_packs.loader import default_packs
from app.data.domain_packs.model import DomainPack
from app.data.semantic.domain import SemanticModel
from app.evidence.domain import Evidence
from app.evidence.preview import RowsPreview
from app.planner.capabilities import LABELS
from app.planner.default_planner import OBJECTIVES
from app.planner.domain import AnalysisStep, CapabilityReport, ReasonCode, Unavailability
from app.planner.runs import StepRecord, StepState
from app.validator.domain import ValidationStatus

PENDING_STATUS = ValidationStatus.UNCERTAIN
DASHBOARD_ANALYSES = tuple(dict.fromkeys(a for _, a in OBJECTIVES))
GENERIC_PACK = "generic"
TYPE_ORDER = {"kpi_card": 0, "line_chart": 1, "area_chart": 1, "bar_chart": 2, "table": 3}


@dataclass(frozen=True)
class StepOutcome:
    step: AnalysisStep
    record: StepRecord
    evidence: Evidence | None
    rows: RowsPreview | None


def _pack_id(source: str | None) -> str | None:
    if source is None or not source.startswith("pack:"):
        return None
    return source.removeprefix("pack:").split("@", 1)[0]


class Prioritizer:
    def __init__(self, packs: dict[str, DomainPack] | None = None) -> None:
        self._packs = packs if packs is not None else default_packs()

    def domain_specific(self, model: SemanticModel, metric: str) -> bool:
        found = next((m for m in model.metrics if m.name == metric), None)
        pack = _pack_id(found.source) if found else None
        return pack is not None and pack != GENERIC_PACK

    def metric_rank(self, model: SemanticModel, metric: str) -> tuple[int, int]:
        """(tier, position): domain-pack KPIs by pack order, generic ones, then measures."""
        found = next((m for m in model.metrics if m.name == metric), None)
        pack = self._packs.get(_pack_id(found.source) or "") if found else None
        if pack is not None:
            names = [t.name for t in pack.metrics]
            position = names.index(metric) if metric in names else len(names)
            return (1 if pack.id == GENERIC_PACK else 0, position)
        measures = [m.name for m in model.measures]
        return (2, measures.index(metric) if metric in measures else len(measures))

    def order(self, widgets: Sequence[Widget], model: SemanticModel) -> list[Widget]:
        def key(item: tuple[int, Widget]) -> tuple[int, tuple[int, int], int]:
            index, widget = item
            if isinstance(widget, UnavailableWidget):
                return (9, (0, 0), index)
            if isinstance(widget, KpiCardWidget):
                return (0, self.metric_rank(model, widget.metric), index)
            return (TYPE_ORDER.get(widget.widget_type, 5), (0, 0), index)

        return [w for _, w in sorted(enumerate(widgets), key=key)]


class MetricFilter:
    """Which metrics a dashboard may show: all, or only generic ones for an uncertain domain."""

    def __init__(self, model: SemanticModel, prioritizer: Prioritizer, generic_only: bool) -> None:
        self._model = model
        self._prioritizer = prioritizer
        self._generic_only = generic_only

    def __call__(self, metric: str) -> bool:
        if not self._generic_only or not metric:
            return True
        return not self._prioritizer.domain_specific(self._model, metric)


def _metric(step: AnalysisStep) -> str:
    return str(step.args.get("metric") or "")


def _title(step: AnalysisStep) -> str:
    metric, args = _metric(step), step.args
    raw_dims = args.get("dimensions")
    dims = [str(d) for d in raw_dims] if isinstance(raw_dims, list) else []
    match step.type:
        case "aggregate" if dims:
            return f"{metric} by {', '.join(dims)}"
        case "timeseries_trend":
            return f"{metric} by {args.get('grain', 'period')}"
        case "growth_rate":
            return f"{metric} growth vs previous period"
        case "rank":
            return f"Top {args.get('dimension')} by {metric}"
        case "explain_change":
            return f"What contributed to the change in {metric}"
        case "forecast" | "anomaly_detection" | "segmentation":
            return LABELS.get(step.type, step.type).capitalize()
        case _:
            return metric or step.type


def _query(step: AnalysisStep) -> MetricRequest | None:
    if step.tool != "analytics.metric_query":
        return None
    return MetricRequest.model_validate(step.args)


def _unavailable(wid: str, title: str, gaps: Sequence[Unavailability]) -> UnavailableWidget:
    return UnavailableWidget(
        id=wid,
        widget_type="unavailable",
        title=title,
        reason_code=gaps[0].reason_code,
        message=" ".join(dict.fromkeys(g.message for g in gaps)),
        would_enable=next((g.would_enable for g in gaps if g.would_enable), None),
    )


_UNDEFINED = re.compile(r"undefined: ([A-Z_]+)")
UNDEFINED_TEXT = {
    "BASE_IS_ZERO": "Undefined (base is zero)",
    "MISSING_VALUE": "Undefined (a value is missing)",
    "NON_POSITIVE_START": "Undefined (the start value is not positive)",
    "NEGATIVE_END": "Undefined (the end value is negative)",
    "ZERO_DURATION": "Undefined (the period has no duration)",
}


def _undefined(evidence: Evidence) -> str | None:
    """Display text of an undefined result, from the reason its calculation records."""
    m = _UNDEFINED.search(evidence.calculation or "")
    return UNDEFINED_TEXT.get(m[1], "Undefined") if m else None


def _failed(o: StepOutcome) -> Unavailability:
    if o.record.state is StepState.SKIPPED:
        message = "Skipped because an analysis it depends on did not succeed."
    else:
        detail = o.record.error.message.rstrip(".") if o.record.error else "an error occurred"
        message = f"This analysis could not be completed: {detail}."
    return Unavailability(
        analysis=o.step.type, reason_code=ReasonCode.ANALYSIS_FAILED, message=message
    )


class WidgetSelector:
    def __init__(
        self, registry: WidgetRegistry | None = None, charts: ChartSelector | None = None
    ) -> None:
        self._registry = registry or WidgetRegistry()
        self._charts = charts or ChartSelector()

    def select(
        self,
        plan_id: str,
        outcomes: Sequence[StepOutcome],
        report: CapabilityReport,
        keep_metric: MetricFilter,
    ) -> list[Widget]:
        overlays = self._overlays(outcomes)
        has_series = any(o.step.type == "timeseries_trend" and o.evidence for o in outcomes)
        widgets: list[Widget] = []
        for o in outcomes:
            if o.record.state is StepState.UNAVAILABLE and o.record.unavailable is not None:
                gap = o.record.unavailable
                widgets.append(_unavailable(f"u_{o.step.id}", _title(o.step), [gap]))
                continue
            if not keep_metric(_metric(o.step)):
                continue
            if o.record.state in (StepState.FAILED, StepState.SKIPPED):
                widgets.append(_unavailable(f"u_{o.step.id}", _title(o.step), [_failed(o)]))
                continue
            if o.evidence is None:
                continue
            if self._registry.entry(o.step.tool).overlay is not None and has_series:
                continue  # drawn on the trend chart
            extra = overlays if o.step.type == "timeseries_trend" else []
            widget = self._widget(plan_id, o, o.evidence, extra)
            if widget is not None:
                widgets.append(widget)
        shown = {w.title for w in widgets if isinstance(w, UnavailableWidget)}
        for analysis in DASHBOARD_ANALYSES:
            gaps = [u for u in report.unavailable if u.analysis == analysis]
            title = LABELS.get(analysis, analysis).capitalize()
            if gaps and title not in shown:
                widgets.append(_unavailable(f"u_{analysis}", title, gaps))
        return widgets

    def _overlays(self, outcomes: Sequence[StepOutcome]) -> list[Overlay]:
        found = []
        for o in outcomes:
            overlay = self._registry.entry(o.step.tool).overlay
            if overlay is not None and o.evidence is not None:
                found.append(Overlay(type=overlay, evidence_id=o.evidence.evidence_id))
        return found

    def _widget(
        self, plan_id: str, o: StepOutcome, evidence: Evidence, overlays: list[Overlay]
    ) -> Widget | None:
        operation = f"{plan_id}-{o.step.id}"
        allowed = self._registry.compatible(o.step.tool)
        value, unit = evidence.result.value, evidence.result.unit
        grouped = o.rows is not None and len(o.rows.rows) > 1
        undefined = _undefined(evidence) if value is None else None
        if "kpi_card" in allowed and (isinstance(value, int | float) or undefined) and not grouped:
            return KpiCardWidget(
                id=f"w_{o.step.id}",
                widget_type="kpi_card",
                title=_title(o.step),
                metric=_metric(o.step),
                value_ref=EvidenceRef(evidence_id=evidence.evidence_id, field="result.value"),
                validation_status=PENDING_STATUS,
                operation_id=operation,
                format=format_spec(unit),
                query=_query(o.step),
                undefined_reason=undefined,
            )
        widget = self._charts.choose(o.rows, tuple(w for w in allowed if w != "kpi_card"))
        if widget is None:
            return None
        chart = chart_spec(
            widget,
            o.rows,
            evidence_id=evidence.evidence_id,
            source_operation=operation,
            unit=unit,
            overlays=overlays if widget in ("line_chart", "area_chart") else [],
        )
        return ChartWidget(
            id=f"w_{o.step.id}",
            widget_type=widget,  # never kpi_card: excluded from the choice above
            title=_title(o.step),
            chart=chart,
            validation_status=PENDING_STATUS,
            operation_id=operation,
            query=_query(o.step),
        )
