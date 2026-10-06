"""ChartSelector + ChartSpec builder (TASK-TOOL-007; design §27, TRD §30).

Deterministic: the chart type follows from the shape of the evidence's result rows (the
``evidence.result_rows`` preview) and what the WidgetRegistry allows for the tool:
- a date/timestamp column and a numeric column → line;
- a text column, a numeric column and at most ``MAX_BARS`` rows → bar;
- otherwise a table.
The y field is the ``value`` column when present (every analytics tool names its measure
column so), else the first numeric column. Values are never copied into the spec: the frontend
reads rows from ``GET /evidence/{id}/data``. Formatting uses the fixed FormatSpec vocabulary.
"""

import re

from app.dashboard.domain import ChartSpec, ChartType, FieldRef, FormatSpec, Overlay
from app.dashboard.registry import WidgetType
from app.evidence.preview import PreviewColumn, RowsPreview
from app.planner.domain import Filter

MAX_BARS = 25
NUMERIC = frozenset({"integer", "float"})
TIME = frozenset({"date", "timestamp"})
TEXT = frozenset({"string", "boolean"})
MAX_TABLE_VALUES = 6
_CURRENCY = re.compile(r"^currency:([A-Z]{3})$")
CHART_OF_WIDGET: dict[WidgetType, ChartType] = {
    "kpi_card": "kpi",
    "line_chart": "line",
    "bar_chart": "bar",
    "stacked_bar": "stacked_bar",
    "area_chart": "area",
    "scatter": "scatter",
    "table": "table",
    "heatmap": "heatmap",
}


def format_spec(unit: str | None) -> FormatSpec:
    """Precision per unit kind: pct/pp 1 decimal (of the x100 display), money 2, counts 0."""
    currency = _CURRENCY.match(unit or "")
    precision = 1 if unit in ("pct", "pp") else 0 if unit == "count" else 2
    iso = currency.group(1) if currency else None
    return FormatSpec(unit=unit, precision=precision, currency=iso)


def _value_column(preview: RowsPreview) -> PreviewColumn | None:
    numeric = [c for c in preview.columns if c.type in NUMERIC]
    return next((c for c in numeric if c.name == "value"), numeric[0] if numeric else None)


class ChartSelector:
    def choose(
        self, preview: RowsPreview | None, allowed: tuple[WidgetType, ...]
    ) -> WidgetType | None:
        if preview is None or not preview.rows:
            return None
        value = _value_column(preview)
        has_time = any(c.type in TIME for c in preview.columns)
        has_text = any(c.type in TEXT for c in preview.columns)
        if value is not None and has_time and "line_chart" in allowed:
            return "line_chart"
        few = len(preview.rows) <= MAX_BARS
        if value is not None and has_text and few and "bar_chart" in allowed:
            return "bar_chart"
        return "table" if "table" in allowed else None


def chart_spec(
    widget: WidgetType,
    preview: RowsPreview | None,
    *,
    evidence_id: str,
    source_operation: str,
    unit: str | None,
    filters: list[Filter] | None = None,
    overlays: list[Overlay] | None = None,
) -> ChartSpec:
    """The ChartSpec of ``widget`` over ``preview``'s columns (``unit`` = the evidence unit)."""
    chart = CHART_OF_WIDGET[widget]
    columns = preview.columns if preview is not None else []
    value = _value_column(preview) if preview is not None else None
    value_unit = (value.unit if value is not None else None) or unit
    x: FieldRef | None = None
    if chart == "kpi":
        y = [FieldRef(field="value", type="numeric", unit=unit)]
    elif chart == "table":
        label = next((c for c in columns if c.type not in NUMERIC), None)
        if label is not None:
            x = FieldRef(field=label.name, type="time" if label.type in TIME else "category")
        values = [c for c in columns if c.type in NUMERIC][:MAX_TABLE_VALUES] or columns[:1]
        y = [
            FieldRef(field=c.name, type="numeric" if c.type in NUMERIC else None, unit=c.unit)
            for c in values
        ]
    else:
        if value is None:
            raise ValueError(f"a {chart} chart needs a numeric column")
        kinds = TIME if chart in ("line", "area") else TEXT
        axis = next(c for c in columns if c.type in kinds)
        x = FieldRef(field=axis.name, type="time" if axis.type in TIME else "category")
        y = [FieldRef(field=value.name, type="numeric", unit=value_unit)]
    return ChartSpec(
        type=chart,
        x=x,
        y=y,
        aggregation="none",  # tool rows are already aggregated
        filters=filters or [],
        source_operation=source_operation,
        evidence_id=evidence_id,
        overlays=overlays or [],
        format=format_spec(value_unit),
    )
