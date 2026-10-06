"""Evidence result preview (dashboard contract gap (a)): which table, typing, bounds."""

from app.evidence.preview import MAX_ROWS, as_data, rows_preview


def test_result_table_is_taken_as_is() -> None:
    table = {
        "columns": [
            {"name": "period", "type": "date", "role": "period"},
            {"name": "value", "type": "float", "role": "value", "unit": "pct"},
        ],
        "rows": [["2026-07-01", 0.1], ["2026-08-01", 0.2]],
        "truncated": True,
    }
    preview = rows_preview({"table": table, "value": None})
    assert preview is not None
    assert [(c.name, c.type, c.unit) for c in preview.columns] == [
        ("period", "date", None),
        ("value", "float", "pct"),
    ]
    assert preview.truncated  # the source table was truncated by the engine


def test_preferred_list_wins_and_rows_are_flattened() -> None:
    output = {
        "result": {
            "dimensions": [{"dimension": "Region", "change": {"total_delta": 1}}],
            "findings": [
                {"factor": "Region=North", "share_pct": 0.5, "association": {"r": 0.9, "n": 4}},
                {"factor": "Region=South", "share_pct": None, "association": {"r": None, "n": 4}},
            ],
        },
        "unit": "currency:INR",
    }
    preview = rows_preview(output)
    assert preview is not None
    names = [c.name for c in preview.columns]
    assert names == ["factor", "share_pct", "association.r", "association.n"]
    assert [c.type for c in preview.columns] == ["string", "float", "float", "integer"]
    data = as_data("ev_x", preview)
    assert data.rows[1] == {
        "factor": "Region=South",
        "share_pct": None,
        "association.r": None,
        "association.n": 4,
    }


def test_series_value_column_carries_the_output_unit_and_rows_are_bounded() -> None:
    points = [{"period": f"2026-01-{d:02d}", "value": d} for d in range(1, 29)] * 20
    preview = rows_preview({"series": {"points": points}, "unit": "count"})
    assert preview is not None
    assert (len(preview.rows), preview.row_count, preview.truncated) == (MAX_ROWS, 560, True)
    assert [(c.name, c.type, c.unit) for c in preview.columns] == [
        ("period", "date", None),
        ("value", "integer", "count"),
    ]


def test_outputs_without_a_table_have_no_preview() -> None:
    assert rows_preview({"value": 1.0, "growth": {"value": 0.1}}) is None
    assert as_data("ev_x", None).rows == []
