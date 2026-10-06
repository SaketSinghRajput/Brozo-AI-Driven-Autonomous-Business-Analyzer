"""NumericalValidator (TASK-VAL-003; TRD 26 numeric check, design 40 numeric matching).

Number segments: the UI renders the value from evidence, so the checks are that the cited field
exists and is numeric, that the display format's unit equals the evidence unit (pct vs pp vs
currency code vs count), and that surrounding direction words agree with the sign. A field
``rows.<i>.<column>`` cites a cell of the evidence's stored result preview (forecast points,
anomaly scores, ...; ``app.evidence.preview``), with that column's unit; previews are passed in
by the caller, and without one such a field is not a number.

Free numbers (backstop): every quantity typed into text or caveat segments must round-match a
numeric field of the cited evidence (``result.value``, ``result.rows`` or a numeric argument)
under a compatible unit, see ``numbers`` for the tolerance. Nothing in segment text or evidence
content is interpreted as an instruction; it is only parsed for numbers.
"""

import re
from collections.abc import Iterable, Iterator, Mapping

from pydantic import JsonValue

from app.evidence.domain import Evidence
from app.evidence.preview import RowsPreview
from app.validator.domain import (
    EvidenceBundle,
    NumberSegment,
    ValidationIssue,
    ValidationRequest,
    cited_ids,
)
from app.validator.numbers import (
    Candidate,
    TextNumber,
    Unit,
    compatible,
    extract_numbers,
    is_coarse,
    parse_format,
    rounds_to,
    unit_of,
)
from app.validator.text import contradicts, direction_at, render

Previews = Mapping[str, RowsPreview]  # evidence_id -> stored result preview
_ROW_FIELD = re.compile(r"^rows\.(\d+)\.(.+)$")

# Issue codes about individual numbers (the confidence score counts their targets).
NUMBER_CODES = frozenset(
    {
        "NUMBER_WITHOUT_EVIDENCE",
        "UNIT_MISMATCH",
        "DIRECTION_MISMATCH",
        "INVALID_FORMAT",
        "FIELD_NOT_NUMERIC",
        "IMPRECISE_NUMBER",
        "SIGN_OMITTED",
    }
)


def _hard(code: str, target: str, detail: str) -> ValidationIssue:
    return ValidationIssue(code=code, severity="hard", target=target, detail=detail)


def _soft(code: str, target: str, detail: str) -> ValidationIssue:
    return ValidationIssue(code=code, severity="soft", target=target, detail=detail)


def _number(node: JsonValue) -> float | None:
    is_number = isinstance(node, int | float) and not isinstance(node, bool)
    return float(node) if is_number else None  # type: ignore[arg-type]


def _cell(preview: RowsPreview | None, path: str) -> tuple[float | None, str | None]:
    """(number, column unit) of ``rows.<i>.<column>`` in a stored result preview."""
    m = _ROW_FIELD.match(path)
    names = [c.name for c in preview.columns] if preview else []
    if m is None or preview is None or m[2] not in names or int(m[1]) >= len(preview.rows):
        return None, None
    j = names.index(m[2])
    return _number(preview.rows[int(m[1])][j]), preview.columns[j].unit


def resolve(evidence: Evidence, path: str, preview: RowsPreview | None = None) -> float | None:
    """Numeric value at a dotted path such as ``result.value``, ``arguments.limit`` or (with the
    evidence's stored preview) ``rows.0.lower``."""
    if path.startswith("rows."):
        return _cell(preview, path)[0]
    node: JsonValue = evidence.model_dump(mode="json")
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return None
    return _number(node)


def field_unit(evidence: Evidence, path: str, preview: RowsPreview | None = None) -> Unit:
    if path.startswith("rows."):
        return unit_of(_cell(preview, path)[1])
    if path == "result.value":
        return unit_of(evidence.result.unit)
    return Unit("count") if path == "result.rows" else Unit("plain")


def _leaves(node: JsonValue, path: str) -> Iterator[tuple[str, float]]:
    if isinstance(node, dict):
        for key, child in node.items():
            yield from _leaves(child, f"{path}.{key}")
    elif isinstance(node, list):
        for i, child in enumerate(node):
            yield from _leaves(child, f"{path}.{i}")
    elif (value := _number(node)) is not None:
        yield path, value


def candidates(cited: Iterable[Evidence]) -> list[Candidate]:
    pool: list[Candidate] = []
    for e in cited:
        paths = [("result.value", e.result.value), ("result.rows", e.result.rows)]
        for path, value in paths:
            if value is not None:
                pool.append(Candidate(float(value), field_unit(e, path), f"{e.evidence_id}:{path}"))
        for path, value in _leaves(e.arguments, "arguments"):
            pool.append(Candidate(value, Unit("plain"), f"{e.evidence_id}:{path}"))
    return pool


class NumericalValidator:
    name = "numerical"
    version = "1.1.0"  # 1.1: rows.<i>.<column> fields over stored result previews

    def __init__(self, previews: Previews | None = None) -> None:
        self._previews: Previews = previews or {}

    async def check(self, req: ValidationRequest, ev: EvidenceBundle) -> list[ValidationIssue]:
        segments = req.segments or []
        rendered = render(segments)
        pool = candidates(ev[i] for i in cited_ids(req) if i in ev)
        issues: list[ValidationIssue] = []
        for idx, seg in enumerate(segments):
            at = rendered.offsets[idx]
            if isinstance(seg, NumberSegment):
                evidence = ev.get(seg.evidence_id)
                if evidence is not None:  # a missing id is EvidenceValidator's MISSING_EVIDENCE
                    preview = self._previews.get(seg.evidence_id)
                    target = f"segments[{idx}]"
                    issues += _structured(target, seg, evidence, preview, rendered.text, at)
                continue
            for j, number in enumerate(extract_numbers(seg.text)):
                target = f"segments[{idx}].numbers[{j}]"
                shown = seg.text[number.start : number.end]
                issues += _free(target, shown, number, pool, rendered.text, at)
        return issues


def _structured(
    target: str,
    seg: NumberSegment,
    evidence: Evidence,
    preview: RowsPreview | None,
    text: str,
    at: int,
) -> list[ValidationIssue]:
    fmt = parse_format(seg.format)
    if fmt is None:
        return [_hard("INVALID_FORMAT", target, f"unknown display format {seg.format!r}")]
    value = resolve(evidence, seg.field, preview)
    if value is None:
        return [_hard("FIELD_NOT_NUMERIC", target, f"{seg.field} is not a number in evidence")]
    if fmt.unit != field_unit(evidence, seg.field, preview):
        detail = f"format {seg.format!r} does not match the evidence unit"
        return [_hard("UNIT_MISMATCH", target, detail)]
    if contradicts(direction_at(text, at, at + 1), value):
        return [_hard("DIRECTION_MISMATCH", target, "direction wording contradicts the sign")]
    return []


def _free(
    target: str, shown: str, n: TextNumber, pool: list[Candidate], text: str, at: int
) -> list[ValidationIssue]:
    direction = direction_at(text, at + n.start, at + n.end)
    if n.signed and contradicts(direction, n.value):
        return [_hard("DIRECTION_MISMATCH", target, f"{shown!r} contradicts its direction word")]
    hits = [c for c in pool if compatible(n.unit, c.unit) and rounds_to(n, c)]
    if not hits:
        if any(rounds_to(n, c) for c in pool):
            detail = f"{shown!r} matches cited evidence only under a different unit"
            return [_hard("UNIT_MISMATCH", target, detail)]
        detail = f"{shown!r} matches no cited evidence value at its displayed precision"
        return [_hard("NUMBER_WITHOUT_EVIDENCE", target, detail)]
    consistent = [c for c in hits if n.signed or not contradicts(direction, c.value)]
    if not consistent:
        return [_hard("DIRECTION_MISMATCH", target, f"direction wording contradicts {shown!r}")]
    omits_sign = not n.signed and direction is None
    best = min(consistent, key=lambda c: (is_coarse(n, c), omits_sign and c.value < 0))
    issues: list[ValidationIssue] = []
    if is_coarse(n, best):
        issues.append(_soft("IMPRECISE_NUMBER", target, f"{shown!r} is rounded too coarsely"))
    if omits_sign and best.value < 0:
        issues.append(_soft("SIGN_OMITTED", target, f"{shown!r} hides a negative value"))
    return issues
