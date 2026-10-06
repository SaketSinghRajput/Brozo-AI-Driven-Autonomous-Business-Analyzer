"""Display formats, free-text number extraction and precision-aware matching (TASK-VAL-003).

Units. Evidence ``result.unit`` is ``pct`` (a fraction: 0.142 means 14.2%), ``pp`` (percentage
points, also a fraction: 0.023 means 2.3 pp), ``currency:<ISO>``, ``count`` or ``None`` (plain).
Any other unit string is treated as plain. pct and pp are displayed x100.

Tolerance (TRD 26). A number shown with ``d`` decimals at scale ``S`` (1, 1e3, 1e6, 1e5 lakh, ...)
matches a value ``c`` when ``|c*k/S - n| <= 0.5*10^-d + 1e-9*max(1, |n|)`` with ``k = 100`` for
pct/pp: the text must equal the value rounded to the precision the text itself shows. Unsigned
text is compared with ``|c|`` (the sign is then checked against direction words). A match whose
relative error exceeds ``COARSE_RELATIVE_ERROR`` is still a match but is reported as imprecise,
so "0%" cannot silently stand in for 0.4%.
"""

import re
from dataclasses import dataclass
from typing import Literal

Kind = Literal["pct", "pp", "currency", "count", "plain"]
COARSE_RELATIVE_ERROR = 0.05


@dataclass(frozen=True, slots=True)
class Unit:
    kind: Kind
    currency: str | None = None

    @property
    def factor(self) -> float:
        return 100.0 if self.kind in ("pct", "pp") else 1.0


def unit_of(raw: str | None) -> Unit:
    """Evidence unit string -> Unit. Unknown or missing units are plain numbers."""
    simple: dict[str | None, Unit] = {"pct": Unit("pct"), "pp": Unit("pp"), "count": Unit("count")}
    if raw in simple:
        return simple[raw]
    if raw is not None and re.fullmatch(r"currency:[A-Z]{3}", raw):
        return Unit("currency", raw.removeprefix("currency:"))
    return Unit("plain")


@dataclass(frozen=True, slots=True)
class DisplayFormat:
    unit: Unit
    decimals: int


_FORMAT = re.compile(r"(pct|pp|number)(?::(\d))?|count|currency:([A-Z]{3})(?::(\d))?")


def parse_format(spec: str) -> DisplayFormat | None:
    """``pct:1``, ``pp:1``, ``number:2``, ``count``, ``currency:INR:0``; None when malformed."""
    m = _FORMAT.fullmatch(spec)
    if m is None:
        return None
    kind, decimals, currency, currency_decimals = m.groups()
    if currency is not None:
        return DisplayFormat(Unit("currency", currency), int(currency_decimals or 0))
    if kind is None:
        return DisplayFormat(Unit("count"), 0)
    unit = Unit("pct") if kind == "pct" else Unit("pp") if kind == "pp" else Unit("plain")
    return DisplayFormat(unit, int(decimals or 0))


@dataclass(frozen=True, slots=True)
class TextNumber:
    value: float  # as displayed: 14.2 for "14.2%", 4.8 for "4.8M"; negative only if signed
    decimals: int
    scale: float
    unit: Unit  # kind is pct, pp, currency or plain
    signed: bool  # an explicit + or - was written
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class Candidate:
    value: float
    unit: Unit
    source: str  # "<evidence_id>:<field>"


_CURRENCY = {"₹": "INR", "$": "USD", "€": "EUR", "£": "GBP", "rs": "INR", "rs.": "INR"}
_SCALE = {
    "k": 1e3,
    "thousand": 1e3,
    "m": 1e6,
    "mn": 1e6,
    "million": 1e6,
    "b": 1e9,
    "bn": 1e9,
    "billion": 1e9,
    "lakh": 1e5,
    "crore": 1e7,
}
_NUMBER = re.compile(
    r"""
    (?<![\w.])
    (?P<cur>[₹$€£]|(?:Rs\.?|INR|USD|EUR|GBP)\s?)?
    (?P<sign>[+\-\u2212])?
    (?P<cur2>[₹$€£])?
    (?P<num>\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)
    (?:\s?(?P<suffix>%|(?i:per\s?cent|percentage\s+points?|pp|thousand|million|billion|lakh
        |crore|mn|bn)\b|[kKmMbB]\b))?
    (?!\w)
    """,
    re.VERBOSE,
)
# Dates and clock times are not quantities; they are masked before scanning.
_DATES = re.compile(r"\b\d{4}-\d{2}(?:-\d{2})?\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b|\b\d{1,2}:\d{2}\b")
_YEAR_WORDS = (
    "in since from to of during until by through before after year fy jan feb mar apr may jun "
    "jul aug sep sept oct nov dec january february march april june july august september "
    "october november december"
)
_YEAR_CONTEXT = frozenset(_YEAR_WORDS.split())


def _currency_code(raw: str | None) -> str | None:
    if raw is None:
        return None
    key = raw.strip()
    return _CURRENCY.get(key.lower(), key.upper())


def _is_year(text: str, m: re.Match[str]) -> bool:
    """A bare 4-digit year after a date word ("in 2026", "August 2026") is not a quantity."""
    if m["cur"] or m["cur2"] or m["sign"] or m["suffix"] or not re.fullmatch(r"\d{4}", m["num"]):
        return False
    words = re.findall(r"[a-z]+", text[: m.start()].lower())
    return 1900 <= int(m["num"]) <= 2100 and bool(words) and words[-1] in _YEAR_CONTEXT


def _unit(m: re.Match[str]) -> Unit:
    suffix = (m["suffix"] or "").lower()
    if suffix == "pp" or suffix.startswith("percentage"):  # before "per": "percentage points"
        return Unit("pp")
    if suffix == "%" or suffix.startswith("per"):
        return Unit("pct")
    code = _currency_code(m["cur"] or m["cur2"])
    return Unit("currency", code) if code else Unit("plain")


def extract_numbers(text: str) -> list[TextNumber]:
    """Every quantity written in free text, with the precision and unit the text shows."""
    masked = _DATES.sub(lambda d: "#" * len(d.group()), text)
    found: list[TextNumber] = []
    for m in _NUMBER.finditer(masked):
        if _is_year(masked, m):
            continue
        digits = m["num"].replace(",", "")
        decimals = len(digits.partition(".")[2])
        value = float(digits) * (-1.0 if m["sign"] in ("-", "\u2212") else 1.0)
        suffix = (m["suffix"] or "").lower()
        found.append(
            TextNumber(
                value=value,
                decimals=decimals,
                scale=_SCALE.get(suffix, 1.0),
                unit=_unit(m),
                signed=m["sign"] is not None,
                start=m.start(),
                end=m.end(),
            )
        )
    return found


def compatible(shown: Unit, evidence: Unit) -> bool:
    """Whether a number written with unit ``shown`` may be backed by evidence in ``evidence``."""
    if shown.kind == "plain":
        return evidence.kind in ("plain", "count", "currency")
    if shown.kind == "currency":
        return evidence.kind == "currency" and evidence.currency == shown.currency
    return shown.kind == evidence.kind


def _shown(tn: TextNumber, c: Candidate) -> float:
    shown = c.value * c.unit.factor / tn.scale
    return shown if tn.signed else abs(shown)


def rounds_to(tn: TextNumber, c: Candidate) -> bool:
    tolerance = 0.5 * 10.0**-tn.decimals + 1e-9 * max(1.0, abs(tn.value))
    return abs(_shown(tn, c) - tn.value) <= tolerance


def is_coarse(tn: TextNumber, c: Candidate) -> bool:
    shown = _shown(tn, c)
    return shown != 0 and abs(shown - tn.value) / abs(shown) > COARSE_RELATIVE_ERROR
