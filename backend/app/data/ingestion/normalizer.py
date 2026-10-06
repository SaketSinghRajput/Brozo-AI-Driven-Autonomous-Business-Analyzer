"""Normalizer, streaming half (TASK-DATA-006, TRD section 10 "Normalize").

Per batch: headers are normalized once (source names kept), cells become trimmed strings, null
tokens become NULL, and cells that a spreadsheet would execute (`=`, `+`, `-`, `@`, tab, CR) are
counted as FORMULA_INJECTION_RISK. Values are stored verbatim: nothing is ever evaluated, and
export neutralization is TASK-SEC-017. Locale numbers, currency, percent and dates are decided per
column over the whole file in `inference.py` (a column-level decision cannot be made per batch).
"""

import re
from collections import Counter
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict

from app.data.domain import Cell, ParsedBatch

NULL_TOKENS = frozenset({"", "na", "n/a", "null", "none", "nan", "-", "--", "#n/a", "nil"})
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
_NUMBER_LIKE = re.compile(r"^[+-]?[\d.,\s]+%?$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f﻿]")
_SPACES = re.compile(r"\s+")


class ColumnName(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    source_name: str


@dataclass(frozen=True)
class NormalizationRules:
    version: str = "norm-1.0"
    null_tokens: frozenset[str] = NULL_TOKENS


def normalize_headers(raw: list[str]) -> tuple[list[ColumnName], list[str]]:
    """Trim/unify header names, fill blanks, dedupe; keep the original as `source_name`."""
    names: list[ColumnName] = []
    flags: list[str] = []
    used: set[str] = set()
    for i, source in enumerate(raw):
        base = _SPACES.sub(" ", _CONTROL.sub("", source)).strip()
        if not base:
            base = f"column_{i + 1}"
            flags.append(f"EMPTY_COLUMN_NAME:{base}")
        name, n = base, 1
        while name.casefold() in used:
            n += 1
            name = f"{base}_{n}"
        if n > 1:
            flags.append(f"DUPLICATE_COLUMN_NAMES:{base}")
        used.add(name.casefold())
        names.append(ColumnName(name=name, source_name=source))
    return names, sorted(set(flags))


@dataclass
class Normalizer:
    columns: list[ColumnName]
    rules: NormalizationRules = field(default_factory=NormalizationRules)
    null_token_hits: Counter[str] = field(default_factory=Counter)
    formula_risk_hits: Counter[str] = field(default_factory=Counter)

    def normalize(self, batch: ParsedBatch) -> list[list[str | None]]:
        """Return row-major string cells (NULL for null tokens), counting per-column signals."""
        out: list[list[str | None]] = []
        names = [c.name for c in self.columns]
        for row in batch.rows:
            clean: list[str | None] = []
            for name, cell in zip(names, row, strict=True):
                text = _to_text(cell)
                if text is not None and text.casefold() in self.rules.null_tokens:
                    if text:
                        self.null_token_hits[name] += 1
                    text = None
                elif text is not None and _is_formula_risk(text):
                    self.formula_risk_hits[name] += 1
                clean.append(text)
            out.append(clean)
        return out

    def flags(self) -> list[str]:
        flags = [f"FORMULA_INJECTION_RISK:{c}" for c in sorted(self.formula_risk_hits)]
        if self.null_token_hits:
            flags.append("MISSING_VALUE_TOKENS")
        return flags


def _to_text(cell: Cell) -> str | None:
    if cell is None:
        return None
    if isinstance(cell, bool):
        return "true" if cell else "false"
    if isinstance(cell, float) and cell.is_integer():
        return str(int(cell))
    return str(cell).strip()


def _is_formula_risk(text: str) -> bool:
    return text.startswith(_FORMULA_START) and not _NUMBER_LIKE.match(text)
