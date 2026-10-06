"""FileInspector (TASK-DATA-002, design.md section 10): sniff real type, size and container.

Extension and client MIME are never trusted: the type comes from magic bytes. ZIP containers are
checked from the central directory *before* anything is inflated (entry count, per-entry ratio,
total uncompressed size), and every entry we do read is read through a hard byte cap.
Macro-enabled workbooks (.xlsm / VBA in legacy .xls) are rejected outright.
"""

import codecs
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Literal

import charset_normalizer

from app.core.errors import DataError, SecurityError
from app.data.domain import FileFacts
from app.data.ingestion.errors import UploadTooLargeError, reject

ZIP_MAGIC = b"PK\x03\x04"
OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
PDF_MAGIC = b"%PDF-"
_BOMS = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)
_VBA_MARKER = "_VBA_PROJECT".encode("utf-16-le")
_CHUNK = 1 << 20

HeadKind = Literal["zip", "ole2", "pdf", "binary", "text"]


@dataclass(frozen=True)
class InspectionLimits:
    max_bytes: int
    max_zip_entries: int = 1_000
    max_uncompressed_bytes: int = 1 << 30
    max_compression_ratio: float = 200.0
    sample_bytes: int = 64 * 1024


def sniff_head(head: bytes) -> HeadKind:
    """Classify from the first bytes only (used by the upload precheck and the inspector)."""
    if head.startswith(ZIP_MAGIC):
        return "zip"
    if head.startswith(OLE2_MAGIC):
        return "ole2"
    if head.startswith(PDF_MAGIC):
        return "pdf"
    if any(head.startswith(bom) for bom, _ in _BOMS):
        return "text"
    return "binary" if b"\x00" in head else "text"


def detect_encoding(head: bytes) -> tuple[str, bool]:
    """BOM first, then strict UTF-8 on the sample, then charset-normalizer (RES-014 R6)."""
    for bom, encoding in _BOMS:
        if head.startswith(bom):
            return encoding, True
    try:
        codecs.getincrementaldecoder("utf-8")().decode(head, final=False)
    except UnicodeDecodeError:
        return _legacy_encoding(head), False
    return "utf-8", False


def _legacy_encoding(head: bytes) -> str:
    """Non-UTF-8 text. On short tabular samples charset-normalizer often prefers exotic Latin
    code pages (e.g. iso8859_14 for French cp1252 text), so a Latin-script guess resolves to
    cp1252, the Windows/Excel Western default; non-Latin scripts keep the detector's answer.
    ponytail: Central-European cp1250 files then decode as cp1252 (flagged ENCODING_NON_UTF8);
    add a per-workspace encoding override if that matters.
    """
    best = charset_normalizer.from_bytes(head).best()
    if best is None:
        return "cp1252"
    latin = all("Latin" in a or a == "Control character" for a in best.alphabets)
    try:
        head.decode("cp1252")
    except UnicodeDecodeError:
        return best.encoding
    return "cp1252" if latin else best.encoding


class FileInspector:
    def __init__(self, limits: InspectionLimits) -> None:
        self._limits = limits

    def inspect(self, path: Path) -> FileFacts:
        size = path.stat().st_size
        if size > self._limits.max_bytes:
            raise reject(
                UploadTooLargeError,
                "OVERSIZED_FILE",
                "The file exceeds the upload size limit.",
                limit_bytes=self._limits.max_bytes,
            )
        if size == 0:
            raise reject(DataError, "EMPTY_FILE", "The file is empty.")
        with path.open("rb") as f:
            head = f.read(self._limits.sample_bytes)
        kind = sniff_head(head)
        if kind == "zip":
            detected, container = self._inspect_zip(path)
            return _facts(path, size, detected, container=container)
        if kind == "ole2":
            self._reject_legacy_office(path)
        if kind == "pdf":
            return _facts(path, size, "pdf")
        if kind != "text":
            return _facts(path, size, "unknown")
        encoding, bom = detect_encoding(head)
        first = head.decode(encoding, errors="replace").lstrip("﻿ \t\r\n")[:1]
        detected = "json" if first in ("{", "[") else "csv"
        return _facts(path, size, detected, encoding=encoding, bom=bom)

    def _inspect_zip(self, path: Path) -> tuple[str, dict[str, int]]:
        try:
            archive = zipfile.ZipFile(path)
        except zipfile.BadZipFile:
            raise reject(DataError, "CORRUPT_ARCHIVE", "The archive could not be read.") from None
        with archive:
            infos = archive.infolist()
            total = self._check_zip_directory(infos)
            names = {info.filename for info in infos}
            content_types = self._read_capped(archive, "[Content_Types].xml", 1 << 20)
            if b"macroEnabled" in content_types or any(
                n.lower().endswith("vbaproject.bin") for n in names
            ):
                raise reject(
                    SecurityError,
                    "MACRO_ENABLED",
                    "Macro-enabled workbooks are not accepted.",
                    hint="Save the workbook as .xlsx (without macros) and upload it again.",
                )
            container = {"entries": len(infos), "uncompressed_bytes": total}
            if "xl/workbook.xml" in names:
                sheets = [n for n in names if n.startswith("xl/worksheets/") and n.endswith(".xml")]
                container["formula_sheets"] = sum(self._has_formula(archive, n) for n in sheets)
                return "xlsx", container
            if "word/document.xml" in names:
                return "docx", container
            return "unknown", container

    def _check_zip_directory(self, infos: list[zipfile.ZipInfo]) -> int:
        lim = self._limits
        if len(infos) > lim.max_zip_entries:
            raise _zip_bomb(f"too many archive entries ({len(infos)})")
        total = 0
        for info in infos:
            parts = info.filename.replace("\\", "/").split("/")
            if info.filename.startswith(("/", "\\")) or ".." in parts or ":" in info.filename:
                raise reject(SecurityError, "UNSAFE_ARCHIVE_PATH", "Archive entry path is unsafe.")
            total += info.file_size
            ratio = info.file_size / max(info.compress_size, 1)
            if info.file_size > _CHUNK and ratio > lim.max_compression_ratio:
                raise _zip_bomb("an archive entry has an extreme compression ratio")
        if total > lim.max_uncompressed_bytes:
            raise _zip_bomb("the archive expands beyond the uncompressed size limit")
        return total

    def _read_capped(self, archive: zipfile.ZipFile, name: str, cap: int) -> bytes:
        try:
            with archive.open(name) as member:
                data = member.read(cap + 1)
        except KeyError:
            return b""
        if len(data) > cap:
            raise _zip_bomb(f"{name} is larger than expected")
        return data

    def _has_formula(self, archive: zipfile.ZipFile, name: str) -> bool:
        # ZipExtFile stops at the declared (already capped) size, so this scan is bounded.
        with archive.open(name) as member:
            return _stream_contains(member, (b"<f>", b"<f "))

    def _reject_legacy_office(self, path: Path) -> None:
        with path.open("rb") as f:
            if _stream_contains(f, (_VBA_MARKER,)):
                raise reject(
                    SecurityError,
                    "MACRO_ENABLED",
                    "The legacy Office file contains macros.",
                    hint="Save the workbook as .xlsx (without macros) and upload it again.",
                )
        raise reject(
            DataError,
            "UNSUPPORTED_FORMAT",
            "Legacy .xls workbooks are not supported.",
            hint="Save the workbook as .xlsx and upload it again.",
        )


def _facts(
    path: Path,
    size: int,
    detected: str,
    *,
    encoding: str | None = None,
    bom: bool = False,
    container: dict[str, int] | None = None,
) -> FileFacts:
    return FileFacts.model_validate(
        {
            "path": path,
            "size_bytes": size,
            "detected_type": detected,
            "encoding": encoding,
            "bom": bom,
            "container": container,
        }
    )


def _stream_contains(stream: IO[bytes], needles: tuple[bytes, ...]) -> bool:
    overlap = max(len(n) for n in needles) - 1
    tail = b""
    while chunk := stream.read(_CHUNK):
        window = tail + chunk
        if any(n in window for n in needles):
            return True
        tail = window[-overlap:]
    return False


def _zip_bomb(message: str) -> SecurityError:
    return reject(SecurityError, "ZIP_BOMB", f"Rejected archive: {message}.")
