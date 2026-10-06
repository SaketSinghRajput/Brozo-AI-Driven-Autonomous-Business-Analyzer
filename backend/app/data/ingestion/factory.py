"""ParserFactory (TASK-DATA-005, design.md section 11): first `can_parse` by ascending priority."""

from collections.abc import Sequence
from typing import Protocol

from app.data.domain import DataParser, FileFacts
from app.data.ingestion.errors import UnsupportedFormatError, reject

_HINTS = {
    "json": "JSON import is not supported yet; export the data as CSV or .xlsx.",
    "pdf": "PDF files are documents, not tables; export the table as CSV or .xlsx.",
    "docx": "Word files are documents, not tables; export the table as CSV or .xlsx.",
}


class ReportingParser(DataParser, Protocol):
    """Parsers also expose what they detected, for the ingest report and review flags."""

    flags: list[str]
    notes: dict[str, str]


class ParserFactory:
    def __init__(self, parsers: Sequence[ReportingParser]) -> None:
        self._parsers = sorted(parsers, key=lambda p: p.priority)

    def for_(self, facts: FileFacts) -> ReportingParser:
        for parser in self._parsers:
            if parser.can_parse(facts):
                return parser
        raise reject(
            UnsupportedFormatError,
            "UNSUPPORTED_FORMAT",
            f"Files of type '{facts.detected_type}' cannot be ingested as a table.",
            hint=_HINTS.get(facts.detected_type, "Upload a CSV or .xlsx file."),
        )
