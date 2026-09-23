"""The reader turns a clinic file into rows of strings, or refuses it.

These tests run against the generated fixtures (tests/fixtures/onboarding), so
they exercise real workbooks and a real Spanish-Excel CSV rather than mocks.
Each one guards a failure that loses or corrupts patient data silently, which is
the only kind of failure that matters here: a loud refusal costs a re-export,
while a quiet mis-read writes a wrong patient record nothing downstream detects.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from src.onboarding.reader import (
    UnreadableFile,
    decode,
    detect_kind,
    inspect_archive,
    read,
    sniff_delimiter,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "onboarding"


@pytest.fixture(scope="module", autouse=True)
def fixtures_exist() -> None:
    """Generate the fixtures if they are missing; they are not in version control."""
    if not (FIXTURES / "1_clean_ips.xlsx").exists():
        subprocess.run(
            [sys.executable, "-m", "tests.fixtures.onboarding.generate"],
            check=True,
            capture_output=True,
        )


# --------------------------------------------------------------- file identity
def test_an_executable_renamed_csv_is_refused() -> None:
    """The extension is a claim by the uploader; the bytes are the evidence."""
    with pytest.raises(UnreadableFile, match="executable"):
        read(FIXTURES / "5_not_really_csv.csv")


def test_kind_comes_from_the_bytes(tmp_path: Path) -> None:
    xlsx = tmp_path / "claims_to_be.csv"
    xlsx.write_bytes(b"PK\x03\x04rest-of-an-archive")
    assert detect_kind(xlsx) == "xlsx"


# ------------------------------------------------------------------- archives
def test_a_zip_bomb_is_refused_before_it_is_parsed() -> None:
    with pytest.raises(UnreadableFile, match=r"uncompressed|expands"):
        inspect_archive(FIXTURES / "5_zip_bomb.xlsx")


def test_an_xxe_payload_is_refused() -> None:
    """defusedxml blocks the entity; the reader turns that into a clean refusal."""
    with pytest.raises(UnreadableFile):
        read(FIXTURES / "5_xxe.xlsx")


# ------------------------------------------------------------------- encoding
def test_the_encoding_ladder_prefers_utf8_and_falls_back_to_cp1252() -> None:
    # utf-8-sig leads the ladder and decodes plain UTF-8 too, so the encoding
    # label matters less than the text being right and the BOM being gone.
    text, encoding = decode("Pérez".encode())
    assert text == "Pérez"
    assert encoding in {"utf-8", "utf-8-sig"}

    # A BOM must be consumed, or the first header becomes "﻿Tipo" and the
    # column mapping misses it with no error anywhere.
    assert decode("Tipo".encode("utf-8-sig")) == ("Tipo", "utf-8-sig")

    # Windows-1252 is not valid UTF-8, so the ladder must fall through to it
    # rather than mangling every accented name in a Spanish export.
    assert decode("Pérez".encode("cp1252")) == ("Pérez", "cp1252")


def test_spanish_excel_csv_is_read_with_the_right_encoding_and_delimiter() -> None:
    result = read(FIXTURES / "3_excel_csv_es.csv")
    assert result.encoding == "cp1252"
    assert result.delimiter == ";"

    sheet = result.sheets[0]
    assert "NOMBRE COMPLETO" in sheet.headers
    # 7 columns, not 1: a comma-assuming reader collapses this file into a
    # single column and every row becomes one unusable string.
    assert len(sheet.headers) == 7
    # Accented names survived the decoding.
    assert any("Pérez" in cell for row in sheet.rows for cell in row)


def test_a_semicolon_file_with_comma_decimals_is_not_split_on_the_comma() -> None:
    """The comma is the decimal separator in Colombia, not the delimiter.

    Every line here splits plausibly on BOTH characters, and the comma is the
    more frequent one, so a scorer that counts raw frequency the way
    `csv.Sniffer` does picks it. That silently cuts `1.250,50` into two fields
    and shifts every later column by one, which still reads as valid data.
    Consistency is the discriminator: the semicolon gives every line the same
    width, the comma does not.
    """
    sample = (
        "NOMBRE;CELULAR;VALOR;EPS\n"
        "Carlos Pérez;3001234567;1.250,50;Sura\n"
        "María López;3119876543;15.300,75;Nueva EPS\n"
        "Luis Castro;3204567890;900;Sanitas\n"
    )
    assert sniff_delimiter(sample) == ";"

    assert sniff_delimiter("A,B\n1,2\n") == ","  # a real comma file still works


def test_the_delimiter_is_chosen_by_consistency_not_by_field_count() -> None:
    """Addresses contain commas, so the wrong delimiter can look just as good.

    Here both characters yield a most-common width of 3, so field count alone
    cannot choose between them: only the semicolon gives *every* line that
    width. Picking the comma would split "Calle 10 # 5-20, Apto 301" across
    columns and shift the EPS into the address field, which still looks like
    data and passes every later shape check.
    """
    sample = (
        "NOMBRE COMPLETO;DIRECCION;EPS\n"
        "Carlos Pérez Gómez;Calle 10 # 5-20, Apto 301, Torre B;Sura\n"
        "María López Torres;Cra 7 # 12-34, Of. 502;Nueva EPS\n"
        "Luis Castro Vargas;Av 68 # 24-15, Casa 2, Barrio Nuevo;Sanitas\n"
    )
    assert sniff_delimiter(sample) == ";"


def test_a_wider_split_does_not_beat_a_consistent_one() -> None:
    """The wrong delimiter can produce more columns, and still be wrong.

    Commas inside every address split these lines into 4 fields while the real
    delimiter gives 2, so ranking on field count picks the comma and destroys
    the address column. Only the header row disagrees with it, and that single
    disagreement is the whole signal: the semicolon splits every line the same
    way, the comma does not.
    """
    sample = (
        "NOMBRE;DIRECCION\n"
        "Carlos Pérez;Calle 10, Apto 301, Torre B, Bogotá\n"
        "María López;Cra 7, Of. 502, Chapinero, Bogotá\n"
        "Luis Castro;Av 68, Casa 2, Barrio Nuevo, Cali\n"
    )
    assert sniff_delimiter(sample) == ";"


def test_an_embedded_newline_stays_inside_one_field() -> None:
    result = read(FIXTURES / "3_excel_csv_es.csv")
    addresses = [row[-1] for row in result.sheets[0].rows]
    assert any("\n" in address for address in addresses)


def test_a_ragged_row_is_padded_and_reported_not_dropped() -> None:
    """A row missing its trailing fields is usually still a real patient."""
    result = read(FIXTURES / "3_excel_csv_es.csv")
    sheet = result.sheets[0]
    assert all(len(row) == len(sheet.headers) for row in sheet.rows)
    assert any("fields, expected" in warning for warning in sheet.warnings)


# ------------------------------------------------------------------ structure
def test_a_clean_export_reads_every_sheet() -> None:
    result = read(FIXTURES / "1_clean_ips.xlsx")
    assert {sheet.name for sheet in result.sheets} == {"Pacientes", "Medicos", "Citas"}
    patients = next(sheet for sheet in result.sheets if sheet.name == "Pacientes")
    assert patients.header_row == 1
    assert len(patients.rows) == 10


def test_a_header_below_title_rows_is_found() -> None:
    """Hand-made workbooks put a clinic name and a print date above the table."""
    sheet = read(FIXTURES / "2_receptionist.xlsx").sheets[0]
    assert sheet.header_row == 5
    assert sheet.headers[:3] == ("TIPO DOC", "IDENTIFICACION", "NOMBRE COMPLETO")


def test_the_first_table_wins_not_the_tidiest_one() -> None:
    """A second table pasted below scores better precisely because it is smaller.

    Taking the best-scoring row anywhere on the sheet skips the real data: this
    fixture's waiting-list table would be read instead of the 12-patient agenda,
    losing every patient without an error.
    """
    sheet = read(FIXTURES / "2_receptionist.xlsx").sheets[0]
    assert len(sheet.rows) >= 12
    assert "NOMBRE COMPLETO" in sheet.headers  # the agenda's header, not "NOMBRE"


def test_hidden_rows_are_imported_and_flagged() -> None:
    """Hiding a row is not deleting it; dropping it silently loses a patient."""
    sheet = read(FIXTURES / "2_receptionist.xlsx").sheets[0]
    assert sheet.hidden_row_numbers == (15, 16)
    assert any("hidden row" in warning for warning in sheet.warnings)


def test_hidden_columns_and_hidden_sheets_are_flagged() -> None:
    result = read(FIXTURES / "2_receptionist.xlsx")
    agenda = result.sheets[0]
    assert "J" in agenda.hidden_columns
    assert "COD_EPS_INTERNO" in agenda.headers
    assert any("hidden" in warning.lower() for warning in result.warnings)


def test_a_lying_dimension_does_not_truncate_the_import() -> None:
    """The declared dimension is a claim by the file, and this one is false.

    openpyxl's read-only mode trusts `<dimension ref="A1:A1"/>` and yields one
    cell for a sheet holding 5,000 rows. Without `reset_dimensions()` the import
    would quietly drop 4,999 patients.
    """
    sheet = read(FIXTURES / "5_lying_dimension.xlsx").sheets[0]
    assert len(sheet.rows) == 5000


def test_an_uncalculated_formula_is_kept_not_read_as_empty() -> None:
    """`data_only=True` returns None for a formula the file never calculated.

    A clinic workbook built on formulas would otherwise import as blank columns
    with no error at all. Keeping the formula text means validation can reject
    the cell, and the export layer can neutralise an injected payload.
    """
    sheet = read(FIXTURES / "5_formula_injection.xlsx").sheets[0]
    assert sheet.rows[0][1].startswith("=")
    assert any("formula" in warning for warning in sheet.warnings)


# ------------------------------------------------------------------- no typing
def test_values_are_returned_as_text_without_inference() -> None:
    """Conversion belongs to the confirmed mapping, never to the reader.

    The damage in this fixture must still be visible downstream: a document
    number already reduced to scientific notation has to reach validation as
    such so the row can be rejected, not be rounded into a plausible number.
    """
    sheet = read(FIXTURES / "4_corrupted.xlsx").sheets[0]
    assert all(isinstance(cell, str) for row in sheet.rows for cell in row)

    columns = {name: index for index, name in enumerate(sheet.headers)}
    first = sheet.rows[0]
    # A cédula whose leading zero Excel already destroyed stays as stored.
    assert first[columns["IDENTIFICACION"]] == "123456"
    # The Spanish 12-hour form keeps its non-breaking space for the normalizer.
    assert " " in first[columns["HORA"]]


# ----------------------------------------------------------- refusal paths
# Coverage showed these branches untested. They are the ones that decide whether
# a malformed upload stops with a message a receptionist can act on, or crashes
# with a stack trace the API cannot report.
def test_an_empty_file_is_refused(tmp_path: Path) -> None:
    empty = tmp_path / "empty.csv"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(UnreadableFile, match="empty"):
        read(empty)


def test_a_workbook_with_no_rows_is_refused(tmp_path: Path) -> None:
    from openpyxl import Workbook

    path = tmp_path / "blank.xlsx"
    Workbook().save(path)
    with pytest.raises(UnreadableFile, match="no readable sheet"):
        read(path)


def test_a_truncated_archive_is_refused_not_crashed(tmp_path: Path) -> None:
    """The magic bytes claim an archive; the contents are not one.

    Without this the reader raises BadZipFile, which the upload endpoint cannot
    turn into an explanation for the person who uploaded the file.
    """
    path = tmp_path / "truncated.xlsx"
    path.write_bytes(b"PK\x03\x04" + b"nonsense" * 20)
    with pytest.raises(UnreadableFile, match="not a valid archive"):
        read(path)


def test_a_single_column_file_is_read_not_refused(tmp_path: Path) -> None:
    """A list of document numbers is a legitimate import with no delimiter."""
    path = tmp_path / "one.csv"
    path.write_text("DOCUMENTO\n1045678901\n1023456789\n", encoding="utf-8")
    sheet = read(path).sheets[0]
    assert sheet.headers == ("DOCUMENTO",)
    assert len(sheet.rows) == 2


def test_legacy_xls_is_refused_with_advice(tmp_path: Path) -> None:
    """The OLE2 format needs a different parser; say so rather than failing oddly."""
    path = tmp_path / "old.xls"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)
    with pytest.raises(UnreadableFile, match="xlsx"):
        read(path)


def test_a_header_with_an_unlabelled_column_keeps_every_row(tmp_path: Path) -> None:
    """A blank header cell made the header row look narrower than the data.

    The first patient was then promoted to column names and lost, and every
    column was mislabelled, with no error anywhere.
    """
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append(["NOMBRE", "", None, "EPS"])
    sheet.append(["Ana Gómez", "x", "y", "Sura"])
    sheet.append(["Luis Castro", "p", "q", "Nueva EPS"])
    path = tmp_path / "gap.xlsx"
    workbook.save(path)

    result = read(path).sheets[0]
    assert result.header_row == 1
    assert result.headers[0] == "NOMBRE"
    assert len(result.rows) == 2
