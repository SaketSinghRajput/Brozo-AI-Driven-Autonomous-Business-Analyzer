"""Deterministic answer segments for ML tool results (forecast, anomalies; GAPS-2, design 25-26).

The LLM never writes ML numbers. For every ok ``ml.forecast`` / ``ml.anomalies`` call, code adds
number segments that cite the evidence's stored result rows (``rows.<i>.<column>``, the
``app.evidence.preview`` table the EvidenceRecorder stored with the evidence), which the
validator checks like any other number:
- forecast: each point is labelled "estimate" with its prediction interval (lower to upper), or
  the model's note when it gave no intervals; a caveat says forecasts are estimates, not facts;
- anomalies: each flagged point with observed (and expected) value, and always the caveat
  ``ANOMALY_CAVEAT`` ("statistical anomaly ≠ business problem").
Text around the numbers holds no digits except ISO dates (the validator masks dates), so free
text never carries an unbacked number. Sizes are bounded (``MAX_POINTS``, ``MAX_ANOMALIES``).
"""

from collections.abc import Mapping

from app.evidence.preview import rows_preview
from app.ml.anomaly_tool import AnomalyOutput
from app.ml.domain import ANOMALY_CAVEAT
from app.ml.forecast_tool import ForecastOutput
from app.orchestrator.tool_loop import ExecutedCall
from app.validator.domain import AnswerSegment, CaveatSegment, NumberSegment, TextSegment

MAX_POINTS = 12
MAX_ANOMALIES = 5
ESTIMATE_CAVEAT = "Forecasts are estimates from past patterns, not facts."
NUMBER = "number:2"


def _num(evidence_id: str, row: int, column: str) -> NumberSegment:
    return NumberSegment(evidence_id=evidence_id, field=f"rows.{row}.{column}", format=NUMBER)


def _forecast(call: ExecutedCall, evidence_id: str) -> list[AnswerSegment]:
    out = ForecastOutput.model_validate(call.result.output).forecast
    segments: list[AnswerSegment] = [TextSegment(text=f"Forecast by {out.grain}:\n")]
    for i, p in enumerate(out.points[:MAX_POINTS]):
        segments += [TextSegment(text=f"{p.period}: estimate "), _num(evidence_id, i, "value")]
        if p.lower is not None and p.upper is not None:
            segments += [
                TextSegment(text=" (prediction interval "),
                _num(evidence_id, i, "lower"),
                TextSegment(text=" to "),
                _num(evidence_id, i, "upper"),
                TextSegment(text=")"),
            ]
        segments.append(TextSegment(text=".\n"))
    if out.note:
        segments.append(CaveatSegment(text=out.note))
    segments.append(CaveatSegment(text=ESTIMATE_CAVEAT))
    return segments


def subject_label(subject: Mapping[str, object]) -> str:
    return ", ".join(str(v) for v in subject.values() if isinstance(v, str)) or "a point"


def _anomalies(call: ExecutedCall, evidence_id: str) -> list[AnswerSegment]:
    out = AnomalyOutput.model_validate(call.result.output).result
    method = out.explanation.method.replace("_", " ")
    if not out.anomalies:
        text = f"No statistical anomalies were detected ({method})."
        return [TextSegment(text=text), CaveatSegment(text=ANOMALY_CAVEAT)]
    segments: list[AnswerSegment] = [TextSegment(text=f"Anomalies detected ({method}):\n")]
    for i, a in enumerate(out.anomalies[:MAX_ANOMALIES]):
        where = subject_label(a.subject)
        segments += [TextSegment(text=f"{where}: observed "), _num(evidence_id, i, "observed")]
        if a.expected is not None:
            segments += [TextSegment(text=", expected "), _num(evidence_id, i, "expected")]
        segments.append(TextSegment(text=f" ({a.direction}).\n"))
    segments.append(CaveatSegment(text=ANOMALY_CAVEAT))
    return segments


BUILDERS = {"ml.forecast": _forecast, "ml.anomalies": _anomalies}


def ml_segments(calls: list[ExecutedCall]) -> list[AnswerSegment]:
    segments: list[AnswerSegment] = []
    for call in calls:
        build = BUILDERS.get(call.name)
        ev = call.result.evidence_id
        if build is not None and call.result.status == "ok" and ev and call.result.output:
            segments += build(call, ev)
    return segments


def row_value(call: ExecutedCall, field: str) -> tuple[float | None, str | None]:
    """(value, unit) of a ``rows.<i>.<column>`` field in this call's result preview."""
    preview = rows_preview(call.result.output) if call.result.output else None
    _, index, column = field.split(".", 2)
    if preview is None or not index.isdigit() or int(index) >= len(preview.rows):
        return None, None
    names = [c.name for c in preview.columns]
    if column not in names:
        return None, None
    cell = preview.rows[int(index)][names.index(column)]
    number = cell if isinstance(cell, int | float) and not isinstance(cell, bool) else None
    return number, preview.columns[names.index(column)].unit
