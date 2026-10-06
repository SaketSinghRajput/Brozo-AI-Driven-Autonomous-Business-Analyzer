"""Analytics tools for the ToolRegistry (TASK-SQL-007, TASK-TOOL-006)."""

from app.analytics.source import DatasetSource
from app.analytics.sql.engine import AnalyticalEngine
from app.analytics.tools.analysis import ExplainChangeTool, StatsTool, TrendTool
from app.analytics.tools.query import GrowthTool, MetricQueryTool, RankTool
from app.tools.domain import Tool


def analytics_tools(source: DatasetSource, engine: AnalyticalEngine) -> list[Tool]:
    classes = (MetricQueryTool, GrowthTool, RankTool, StatsTool, TrendTool, ExplainChangeTool)
    return [cls(source, engine) for cls in classes]
