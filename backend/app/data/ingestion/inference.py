"""Physical type inference (TASK-DATA-007) and the column half of the Normalizer (TASK-DATA-006).

Runs over the all-string staging Parquet with one streaming Polars aggregation, counting per
column how many non-null values parse as each candidate (int, dot/comma-decimal number, bool,
each date and datetime format). A column is typed only when a candidate parses *every* value;
otherwise it is kept as text, losslessly, as `mixed` (best ratio >= 0.5, flagged) or `string`.
Ambiguity is flagged, never guessed: all-`1,234` numbers, or `05/01/2025` dates whose day/month
order no value disambiguates, stay text with a review flag. A numeric column whose values carry a
`%` is stored as fractions (12% -> 0.12), the meaning of unit `pct` everywhere downstream; so is a
numeric column whose header names a percentage ("discount_pct") when some |value| > 1, i.e. it is
in points (flag PERCENT_SCALED_FROM_HEADER). All values within [-1, 1]: already fractions, kept.
"""

import re
from typing import Any, Literal

import polars as pl
import pyarrow as pa
from pydantic import BaseModel, ConfigDict

from app.data.domain import Ratio
from app.data.profiling.domain import PhysicalType, UnitHint

DATE_FORMATS = ("%Y-%m-%d", "%Y-%m", "%Y/%m/%d", "%Y.%m.%d", "%d.%m.%Y", "%b %d, %Y", "%d %b %Y")
ORDER_PAIRS = (("%d/%m/%Y", "%m/%d/%Y"), ("%d-%m-%Y", "%m-%d-%Y"))
AWARE_FORMATS = ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z")
NAIVE_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M")
_ALL_DATES = DATE_FORMATS + tuple(f for pair in ORDER_PAIRS for f in pair)
_ALL_DATETIMES = AWARE_FORMATS + NAIVE_FORMATS

_INT = r"^[+-]?\d{1,18}$"
_DOT = r"^[+-]?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?([eE][+-]?\d+)?$"
_COMMA = r"^[+-]?(\d{1,3}(\.\d{3})+|\d+)(,\d+)?$"
_BOOL = r"(?i)^(true|false|yes|no)$"
_CURRENCY = r"[$€£₹¥]"
# A header naming a percentage ("discount_pct", "Growth %", "share percent").
_PERCENT_HEADER = re.compile(r"(?i)(?:^|[^a-z])(?:pct|percent|percentage)(?:[^a-z]|$)|%")

ARROW_TYPES: dict[str, pa.DataType] = {
    "int": pa.int64(),
    "float": pa.float64(),
    "bool": pa.bool_(),
    "date": pa.date32(),
    "datetime": pa.timestamp("us", tz="UTC"),
}


class ColumnDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    source_name: str
    physical_type: PhysicalType
    parse_success_ratio: Ratio
    formats: list[str] = []
    decimal: Literal[".", ","] | None = None
    unit_hint: UnitHint | None = None
    tz_mixed: bool = False
    flags: list[str] = []

    def arrow_type(self) -> pa.DataType:
        return ARROW_TYPES.get(self.physical_type, pa.string())


def _cleaned(key: str) -> pl.Expr:
    stripped = pl.col(key).str.replace_all(r"^[\s$€£₹¥]+|[\s$€£₹¥%]+$", "")
    return stripped.str.replace_all(r"\s", "")  # Unicode \s includes NBSP


def _date(key: str, fmt: str) -> pl.Expr:
    if fmt == "%Y-%m":  # month-grain values ("2024-09") are stored as the first of the month
        return (pl.col(key) + "-01").str.to_date("%Y-%m-%d", strict=False)
    return pl.col(key).str.to_date(fmt, strict=False)


def _datetime(key: str, fmt: str) -> pl.Expr:
    iso = pl.col(key).str.replace(r"Z$", "+00:00")
    return iso.str.to_datetime(fmt, strict=False, time_unit="us", time_zone="UTC")


def _count_exprs(key: str) -> list[pl.Expr]:
    c = _cleaned(key)
    lead0 = c.str.contains(r"^[+-]?0\d")
    counts = {
        "n": pl.col(key).is_not_null(),
        "int": c.str.contains(_INT) & ~lead0,
        "lead0": lead0,
        "dot": c.str.contains(_DOT),
        "comma": c.str.contains(_COMMA),
        "has_dot": c.str.contains(".", literal=True),
        "has_comma": c.str.contains(",", literal=True),
        "bool": pl.col(key).str.contains(_BOOL),
        "pct": pl.col(key).str.contains(r"%\s*$"),
        "cur": pl.col(key).str.contains(_CURRENCY),
        # |value| > 1: a percent-named column in points (10 = 10 %), not already a fraction
        "gt1": c.str.replace_all(",", "").cast(pl.Float64, strict=False).abs() > 1,
    }
    counts |= {fmt: _date(key, fmt).is_not_null() for fmt in _ALL_DATES}
    counts |= {fmt: _datetime(key, fmt).is_not_null() for fmt in _ALL_DATETIMES}
    counts |= {
        f"both{i}": _date(key, a).is_not_null() & _date(key, b).is_not_null()
        for i, (a, b) in enumerate(ORDER_PAIRS)
    }
    return [expr.sum().alias(f"{key}\x1f{metric}") for metric, expr in counts.items()]


def infer_columns(staging: pl.LazyFrame, names: list[tuple[str, str, str]]) -> list[ColumnDecision]:
    """`names` = (staging key, normalized name, source name) per column."""
    if not names:
        return []
    exprs = [e for key, _, _ in names for e in _count_exprs(key)]
    row = staging.select(exprs).collect(engine="streaming").row(0, named=True)
    decisions = []
    for key, name, source in names:
        prefix = f"{key}\x1f"
        m = {k.removeprefix(prefix): int(v or 0) for k, v in row.items() if k.startswith(prefix)}
        decisions.append(decide(name, source, m))
    return decisions


def decide(name: str, source: str, m: dict[str, int]) -> ColumnDecision:
    n = m["n"]
    base: dict[str, Any] = {"name": name, "source_name": source, "parse_success_ratio": 1.0}
    if n == 0:
        return ColumnDecision(**base, physical_type="string")
    unit = "percent" if m["pct"] else ("currency" if m["cur"] else None)
    scaled: list[str] = []
    if unit is None and _PERCENT_HEADER.search(name) and m.get("gt1", 0):
        unit, scaled = "percent", [f"PERCENT_SCALED_FROM_HEADER:{name}"]
    if m["bool"] == n:
        return ColumnDecision(**base, physical_type="bool")
    if m["int"] == n:
        if unit == "percent":  # "12%" is stored as the fraction 0.12 (unit pct, TRD 25.1)
            return ColumnDecision(
                **base, physical_type="float", decimal=".", unit_hint=unit, flags=scaled
            )
        return ColumnDecision(**base, physical_type="int", unit_hint=unit)
    numeric = _decide_number(name, m, n)
    if numeric is not None:
        if numeric["physical_type"] == "float" and scaled:
            numeric["flags"] = [*numeric.get("flags", []), *scaled]
        elif numeric["physical_type"] != "float" and scaled:
            unit = None  # kept as text: nothing is scaled
        return ColumnDecision(**base, **numeric, unit_hint=unit)
    temporal = _decide_temporal(name, m, n)
    if temporal is not None:
        return ColumnDecision(**base, **temporal)
    pairs_both = sum(m[f"both{i}"] for i in range(len(ORDER_PAIRS)))
    dates = sum(m[f] for f in _ALL_DATES) - pairs_both
    best = max(m["int"], m["dot"], m["comma"], m["bool"], dates, sum(m[f] for f in _ALL_DATETIMES))
    if best / n >= 0.5:
        base["parse_success_ratio"] = min(best / n, 1.0)
        return ColumnDecision(
            **base, physical_type="mixed", flags=[f"MIXED_TYPES_IN_COLUMN:{name}"]
        )
    return ColumnDecision(**base, physical_type="string")


def _decide_number(name: str, m: dict[str, int], n: int) -> dict[str, Any] | None:
    dot_ok, comma_ok = m["dot"] == n, m["comma"] == n
    if not (dot_ok or comma_ok):
        return None
    if m["lead0"]:
        return {"physical_type": "string"}  # leading-zero codes (zip codes, ids) stay verbatim
    if dot_ok and comma_ok:
        if not (m["has_dot"] or m["has_comma"]):
            return {"physical_type": "string"}  # digit strings too long for int64: identifiers
        return {"physical_type": "string", "flags": [f"AMBIGUOUS_NUMBER_LOCALE:{name}"]}
    if dot_ok:
        flags = [f"THOUSANDS_SEPARATOR:{name}"] if m["has_comma"] else []
        return {"physical_type": "float", "decimal": ".", "flags": flags}
    flags = [f"DECIMAL_COMMA:{name}"] + ([f"THOUSANDS_SEPARATOR:{name}"] if m["has_dot"] else [])
    return {"physical_type": "float", "decimal": ",", "flags": flags}


def _decide_temporal(name: str, m: dict[str, int], n: int) -> dict[str, Any] | None:
    chosen = [f for f in DATE_FORMATS if m[f]]
    flags: list[str] = []
    ambiguous = 0
    for i, (dmy, mdy) in enumerate(ORDER_PAIRS):
        union = m[dmy] + m[mdy] - m[f"both{i}"]
        if union == 0:
            continue
        if m[dmy] == union and m[mdy] < union:
            chosen.append(dmy)
            flags.append(f"DATE_FORMAT_DMY:{name}")
        elif m[mdy] == union and m[dmy] < union:
            chosen.append(mdy)
            flags.append(f"DATE_FORMAT_MDY:{name}")
        else:
            ambiguous += union
    if sum(m[f] for f in chosen) + ambiguous == n:
        if len(chosen) + (1 if ambiguous else 0) > 1:
            flags.append(f"MIXED_DATE_FORMATS:{name}")
        if ambiguous:
            return {"physical_type": "string", "flags": [*flags, f"DATE_ORDER_AMBIGUOUS:{name}"]}
        return {"physical_type": "date", "formats": chosen, "flags": flags}
    aware = [f for f in AWARE_FORMATS if m[f]]
    naive = [f for f in NAIVE_FORMATS if m[f]]
    if not (aware or naive) or sum(m[f] for f in aware + naive) != n:
        return None
    return {
        "physical_type": "datetime",
        "formats": aware + naive,
        "tz_mixed": bool(aware and naive),
        "flags": [f"TIMEZONE_ASSUMED_UTC:{name}"] if naive else [],
    }


def convert_expr(key: str, d: ColumnDecision) -> pl.Expr:
    """Staging string column → stored typed column; `mixed`/`string` stay verbatim text."""
    match d.physical_type:
        case "int":
            expr = _cleaned(key).cast(pl.Int64)
        case "float" if d.decimal == ",":
            text = _cleaned(key).str.replace_all(".", "", literal=True)
            expr = text.str.replace(",", ".", literal=True).cast(pl.Float64)
        case "float":
            expr = _cleaned(key).str.replace_all(",", "", literal=True).cast(pl.Float64)
        case "bool":
            expr = pl.col(key).str.to_lowercase().is_in(["true", "yes"])
        case "date":
            expr = pl.coalesce([_date(key, f) for f in d.formats])
        case "datetime":
            expr = pl.coalesce([_datetime(key, f) for f in d.formats])
        case _:
            expr = pl.col(key)
    if d.unit_hint == "percent" and d.physical_type == "float":
        expr = expr / 100  # unit pct is a fraction: 12% -> 0.12
    return pl.when(pl.col(key).is_null()).then(None).otherwise(expr).alias(d.name)
