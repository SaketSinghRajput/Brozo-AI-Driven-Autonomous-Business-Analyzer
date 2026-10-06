"""WidgetRegistry (TASK-DASH-002, design §28): tool output model → compatible widget types.

Keyed by the tool name (what evidence records as ``operation``) with the output model name the
plan step declares as ``expected_output``, so both a plan step and a bare evidence record (the
``viz.spec`` tool) resolve the same entry. Order = preference. A tool that is not listed (a new
tool) gets a table when it has rows, so new tools degrade to a generic widget instead of none.
"""

from dataclasses import dataclass
from typing import Literal

WidgetType = Literal[
    "kpi_card",
    "line_chart",
    "bar_chart",
    "stacked_bar",
    "area_chart",
    "scatter",
    "table",
    "heatmap",
]
Overlay = Literal["forecast_band", "anomaly_markers"]


@dataclass(frozen=True)
class WidgetEntry:
    output_model: str
    widgets: tuple[WidgetType, ...]
    overlay: Overlay | None = None  # also drawn on the metric's time-series chart


_ENTRIES = {
    "analytics.metric_query": WidgetEntry(
        "MetricQueryOutput", ("kpi_card", "line_chart", "bar_chart", "table")
    ),
    "analytics.growth": WidgetEntry("GrowthOutput", ("kpi_card",)),
    "analytics.trend": WidgetEntry("TrendOutput", ("line_chart", "area_chart", "table")),
    "analytics.rank": WidgetEntry("RankOutput", ("bar_chart", "table")),
    "analytics.stats": WidgetEntry("StatsOutput", ("table",)),
    "analytics.explain_change": WidgetEntry("ExplainChangeOutput", ("table",)),
    "ml.forecast": WidgetEntry("ForecastOutput", ("line_chart", "table"), "forecast_band"),
    "ml.anomalies": WidgetEntry("AnomalyOutput", ("table",), "anomaly_markers"),
    "ml.segments": WidgetEntry("SegmentsOutput", ("table", "bar_chart")),
}
GENERIC = WidgetEntry("unknown", ("table",))


class WidgetRegistry:
    def entry(self, tool: str) -> WidgetEntry:
        return _ENTRIES.get(tool, GENERIC)

    def compatible(self, tool: str) -> tuple[WidgetType, ...]:
        return self.entry(tool).widgets
