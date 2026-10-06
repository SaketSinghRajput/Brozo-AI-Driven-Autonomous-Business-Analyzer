"""SemanticSignalExtractor (TASK-PROFILE-007) and PII masking (TASK-PROFILE-008).

Signals, not decisions: name tokens, regex hit ratios, unit hints and date formats that the
SchemaAnalyzer (another module) turns into semantic types. PII-likeness is decided here only to
mask samples: a column is PII-like when its name has a personal/identifier token or most values
look like emails or phone numbers. Masked values keep the first character only ("C***"), so no
persisted profile holds a raw email, phone number, name or identifier.
"""

import re
from typing import Any

import polars as pl
from pydantic import BaseModel, ConfigDict

from app.data.profiling.domain import ProfileResult, TextStats

REGEXES = {
    "email": r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    "url": r"(?i)^(https?://|www\.)",
    "currency": r"^[$€£₹¥]|[$€£₹¥]$",
    "percent": r"%$",
    "date": r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|^\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}$",
    "phone": r"^\+?[\d\s().-]{7,20}$",
}
PII_TOKENS = frozenset(
    {
        "email", "mail", "phone", "mobile", "tel", "telephone", "name", "firstname", "lastname",
        "surname", "address", "street", "zip", "postcode", "ssn", "passport", "dob", "birth",
        "birthday", "id", "uuid", "account", "iban", "card",
    }
)  # fmt: skip
PII_VALUE_RATIO = 0.3
_TOKEN = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")


class ColumnSignals(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name_tokens: list[str]
    regex_hit_ratios: dict[str, float]
    unit_hint: str | None
    date_formats: list[str]
    pii_like: bool


def name_tokens(name: str) -> list[str]:
    """`customerEmail_2` → ["customer", "email", "2"]."""
    return [t.lower() for t in _TOKEN.findall(name)]


def text_stats(series: pl.Series) -> TextStats:
    s = series.drop_nulls().cast(pl.String)
    n = s.len()
    if n == 0:
        return TextStats(
            min_length=0, max_length=0, mean_length=0.0, pattern_classes={}, regex_hit_ratios={}
        )
    lengths: Any = s.str.len_chars()
    digits = int(s.str.contains(r"^\d+$").sum())
    alpha = int(s.str.contains(r"^\p{L}+$").sum())
    return TextStats(
        min_length=int(lengths.min()),
        max_length=int(lengths.max()),
        mean_length=float(lengths.mean()),
        pattern_classes={
            "digits": digits / n,
            "alpha": alpha / n,
            "mixed": (n - digits - alpha) / n,
        },
        regex_hit_ratios={k: _hits(s, k) / n for k in REGEXES},
    )


def _hits(s: pl.Series, kind: str) -> int:
    hit = s.str.contains(REGEXES[kind])
    if kind == "phone":  # "2025-01-05" is digits and dashes too; a date is not a phone
        hit = hit & ~s.str.contains(REGEXES["date"])
    return int(hit.sum())


def is_pii_like(name: str, text: TextStats | None) -> bool:
    if PII_TOKENS.intersection(name_tokens(name)):
        return True
    hits = text.regex_hit_ratios if text else {}
    return any(hits.get(k, 0.0) >= PII_VALUE_RATIO for k in ("email", "phone"))


def mask(value: str) -> str:
    return f"{value[:1]}***" if len(value) > 1 else "***"


class SemanticSignalExtractor:
    """Combines profile text stats with ingest-time unit/date-format evidence per column."""

    def extract(
        self, profile: ProfileResult, ingest_columns: list[dict[str, Any]]
    ) -> dict[str, ColumnSignals]:
        ingest = {str(c["name"]): c for c in ingest_columns}
        out: dict[str, ColumnSignals] = {}
        for column in profile.columns:
            meta = ingest.get(column.name, {})
            out[column.name] = ColumnSignals(
                name_tokens=name_tokens(column.name),
                regex_hit_ratios=column.text.regex_hit_ratios if column.text else {},
                unit_hint=meta.get("unit_hint"),
                date_formats=list(meta.get("formats") or []),
                pii_like=is_pii_like(column.name, column.text),
            )
        return out
