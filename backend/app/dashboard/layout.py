"""LayoutEngine (TASK-DASH-004, design §28): deterministic 12-column grid packing.

Bands in widget order (the Prioritizer already sorted them): KPI cards 3 columns wide, four per
row; charts and tables 6 wide and 2 high, two per row, a lone last one spanning the full width;
unavailable cards 4 wide, three per row, last. Same widgets in the same order → same layout.
"""

from collections.abc import Sequence

from app.dashboard.domain import KpiCardWidget, Layout, LayoutItem, UnavailableWidget, Widget

COLUMNS = 12
BANDS = (("kpi", 3, 1), ("chart", 6, 2), ("unavailable", 4, 1))  # (band, width, height)


def _band(widget: Widget) -> str:
    if isinstance(widget, KpiCardWidget):
        return "kpi"
    return "unavailable" if isinstance(widget, UnavailableWidget) else "chart"


class LayoutEngine:
    def layout(self, widgets: Sequence[Widget]) -> Layout:
        items: list[LayoutItem] = []
        y = 0
        for band, width, height in BANDS:
            members = [w for w in widgets if _band(w) == band]
            per_row = COLUMNS // width
            for start in range(0, len(members), per_row):
                row = members[start : start + per_row]
                w = COLUMNS if band == "chart" and len(row) == 1 else width
                items += [
                    LayoutItem(id=m.id, x=i * w, y=y, w=w, h=height) for i, m in enumerate(row)
                ]
                y += height
        return Layout(columns=COLUMNS, items=items)
