"""Deterministic insight and summary text for the dashboard (contract gap (d); TRD 14.4: no LLM
in dashboard planning).

Every sentence is built from evidence values with the composer's number formatting and is then
validated like any AI claim (DashboardService): numbers must round-match the cited evidence,
direction words must agree with the sign. "Observed" text states a calculation; "possible
explanation" text names the largest contributors of the RCA step, worded as contributions, never
causes. Dates are left out of the text on purpose (they would read as unbacked numbers).
"""

from app.evidence.domain import Evidence
from app.evidence.preview import RowsPreview
from app.orchestrator.composer import format_value

MAX_EXPLANATIONS = 3


def kpi_sentence(title: str, evidence: Evidence) -> str | None:
    value = evidence.result.value
    if value is None:
        return None
    return f"{title}: {format_value(float(value), evidence.result.unit)}."


def growth_sentence(metric: str, evidence: Evidence) -> str | None:
    value = evidence.result.value
    if value is None:
        return None
    if value == 0:
        return f"{metric} was unchanged vs the previous period."
    shown = format_value(abs(float(value)), evidence.result.unit)
    return f"{metric} {'rose' if value > 0 else 'fell'} {shown} vs the previous period."


def explanations(rows: RowsPreview | None) -> list[str]:
    """Top contributors of an explain_change result (its rows are the ranked findings)."""
    if rows is None:
        return []
    names = [c.name for c in rows.columns]
    if "factor" not in names:
        return []
    index = names.index("factor")
    factors = [str(r[index]) for r in rows.rows if r[index] is not None][:MAX_EXPLANATIONS]
    return [
        f"Large contribution to the change: {f} (a contribution, not a proven cause)."
        for f in factors
    ]
