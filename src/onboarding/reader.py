"""Turn an uploaded clinic file into rows of strings, or refuse it.

Three rules govern this module:

**Nothing is inferred.** Values are read as text exactly as stored. Type
inference is what silently turns a cédula into a float and a Colombian date into
an American one, so conversion happens later, in `normalizers`, driven by the
mapping a human confirmed. CLAUDE.md §11 names inferring transforms from sampled
rows as the failure that sank the client's previous project.

**Refusal beats a guess.** Where a file is genuinely ambiguous, the reader
raises rather than picking. A loud failure costs a re-export; a wrong guess
writes a wrong patient record that nothing downstream can detect.

**The file is hostile until proven otherwise.** It arrives from outside, so the
archive is inspected before it is opened, and `run_isolated` exists to run the
whole parse in a process that can be killed.
"""

from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

# defusedxml must be imported before openpyxl parses anything: openpyxl's own
# documentation states it does not defend against billion-laughs or quadratic
# blowup unless defusedxml is installed. Importing it patches the XML stack.
import defusedxml  # noqa: F401  (imported for the side effect, not the name)
import openpyxl
from openpyxl.worksheet.worksheet import Worksheet

# Signatures we accept, checked against the bytes rather than the extension.
_ZIP_MAGIC: Final = b"PK\x03\x04"
_OLE2_MAGIC: Final = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # legacy .xls

# Archive limits. A crafted .xlsx can be a few kilobytes and expand to gigabytes.
MAX_UNCOMPRESSED_BYTES: Final = 512 * 1024 * 1024
MAX_COMPRESSION_RATIO: Final = 200
MAX_ARCHIVE_MEMBERS: Final = 2_000

# Encoding ladder, strictest first. A probabilistic detector is deliberately not
# used: guessing wrong corrupts every accented name in the file, and Spanish
# clinic data is nothing but accented names.
ENCODING_LADDER: Final = ("utf-8-sig", "utf-8", "cp1252", "latin-1")

# Delimiters Excel actually writes. Semicolon is the default in Spanish locales,
# where the comma is the decimal separator.
_CANDIDATE_DELIMITERS: Final = (";", ",", "\t", "|")

# Returned when no candidate delimiter splits the file. A one-column list of
# document numbers is a legitimate import, so it is read as a single column
# rather than refused. ASCII record separator does not occur in spreadsheet
# text, so no line is ever split on it.
SINGLE_COLUMN: Final = chr(30)


class UnreadableFile(Exception):
    """The file cannot be read safely, or cannot be read without guessing."""


@dataclass(frozen=True, slots=True)
class Sheet:
    """One table of strings, with the structure findings that produced it."""

    name: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    #: 1-based row in the source file where `headers` was found.
    header_row: int
    #: Rows the file marked hidden. Imported, but flagged: hiding a row is not
    #: deleting it, and dropping it silently would lose a real patient.
    hidden_row_numbers: tuple[int, ...] = ()
    #: Columns the file marked hidden; often the clinic's real internal code.
    hidden_columns: tuple[str, ...] = ()
    #: Findings a human should see before confirming the mapping.
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReadResult:
    sheets: tuple[Sheet, ...]
    encoding: str | None = None
    delimiter: str | None = None
    warnings: tuple[str, ...] = field(default=())


# --------------------------------------------------------------------- guards
def inspect_archive(path: Path) -> None:
    """Refuse an archive that would expand far beyond its size on disk.

    The declared `file_size` in a ZIP's central directory is attacker-controlled,
    so it is treated as a claim to check rather than a fact, and the real
    expansion is measured by decompressing with a cap.
    """
    try:
        archive_context = zipfile.ZipFile(path)
    except zipfile.BadZipFile as error:
        raise UnreadableFile(
            "File starts like a spreadsheet but is not a valid archive; it may be truncated."
        ) from error

    with archive_context as archive:
        members = archive.infolist()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise UnreadableFile(
                f"Archive declares {len(members)} members, more than the {MAX_ARCHIVE_MEMBERS} allowed."
            )

        declared = sum(member.file_size for member in members)
        compressed = sum(member.compress_size for member in members) or 1
        if declared > MAX_UNCOMPRESSED_BYTES:
            raise UnreadableFile(
                f"Archive declares {declared:,} bytes uncompressed, "
                f"more than the {MAX_UNCOMPRESSED_BYTES:,} allowed."
            )
        if declared / compressed > MAX_COMPRESSION_RATIO:
            raise UnreadableFile(
                f"Archive expands {declared / compressed:.0f}x, "
                f"more than the {MAX_COMPRESSION_RATIO}x allowed."
            )

        # The declaration may lie, so read the members and count what arrives.
        total = 0
        for member in members:
            with archive.open(member) as stream:
                while chunk := stream.read(64 * 1024):
                    total += len(chunk)
                    if total > MAX_UNCOMPRESSED_BYTES:
                        raise UnreadableFile(
                            f"Archive expands past the {MAX_UNCOMPRESSED_BYTES:,} byte limit; "
                            "its declared sizes understate it."
                        )


def detect_kind(path: Path) -> str:
    """Return 'xlsx', 'xls' or 'csv' from the file's bytes, not its name."""
    head = path.read_bytes()[:8]
    if head.startswith(_ZIP_MAGIC):
        return "xlsx"
    if head.startswith(_OLE2_MAGIC):
        return "xls"
    # Anything else must be text to be a CSV. An executable renamed .csv fails here.
    if head[:2] == b"MZ" or head[:4] == b"\x7fELF":
        raise UnreadableFile("File is an executable, not a spreadsheet.")
    return "csv"


# ----------------------------------------------------------------------- CSV
def decode(raw: bytes) -> tuple[str, str]:
    """Decode with the strictest encoding that accepts the whole file.

    Returns the text and the encoding used. `latin-1` cannot fail, so it is the
    floor rather than a real detection; when it is reached the caller is warned,
    because a wrong decoding shows up as mojibake in a patient's name.
    """
    for encoding in ENCODING_LADDER:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise UnreadableFile("File is not decodable as text in any supported encoding.")


def sniff_delimiter(sample: str) -> str:
    """Score the candidate delimiters instead of asking `csv.Sniffer`.

    The rule is consistency: the right delimiter splits every line into the same
    number of fields. `csv.Sniffer` guesses from character frequency and reads
    `1.250,50` as a comma-separated pair, which silently splits a Spanish
    currency column in two.
    """
    lines = [line for line in sample.splitlines() if line.strip()][:20]
    if not lines:
        raise UnreadableFile("File is empty.")

    scored: list[tuple[float, int, str]] = []  # (agreement, width, delimiter)
    for delimiter in _CANDIDATE_DELIMITERS:
        try:
            rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter))
        except csv.Error:
            continue
        widths = [len(row) for row in rows if row]
        if not widths:
            continue
        # The most frequent width, and among ties the widest: a delimiter must
        # not be rewarded for leaving most lines unsplit.
        top = max(widths.count(width) for width in set(widths))
        width = max(w for w in set(widths) if widths.count(w) == top)
        if width < 2:
            continue  # this delimiter does not split the file at all
        # Agreement, not frequency. An address column full of commas splits some
        # lines and not others, and that disagreement is what rules the comma
        # out even where it yields as many fields as the real delimiter.
        scored.append((top / len(widths), width, delimiter))

    if not scored:
        # A one-column file is a legitimate import (a list of document numbers,
        # say), so it is read as a single column rather than refused. The
        # delimiter is one that cannot occur in text, so nothing is split.
        return SINGLE_COLUMN

    # Ranked on agreement first, then width. Both are properties of the file, so
    # the answer does not depend on the order the candidates were tried in.
    return max(scored)[2]


def read_csv(path: Path) -> ReadResult:
    raw = path.read_bytes()
    if raw[:2] == b"MZ":
        raise UnreadableFile("File is an executable, not a spreadsheet.")

    text, encoding = decode(raw)
    warnings: list[str] = []
    if encoding == "latin-1":
        warnings.append("Encoding could not be determined; read as latin-1. Check accented names.")
    elif encoding == "cp1252":
        warnings.append("Read as Windows-1252 (a Spanish Excel export). Check accented names.")

    delimiter = sniff_delimiter(text[:64_000])
    rows = [
        tuple(cell.strip() for cell in row)
        for row in csv.reader(io.StringIO(text), delimiter=delimiter)
    ]
    rows = [row for row in rows if any(cell for cell in row)]
    if not rows:
        raise UnreadableFile("File contains no rows.")

    headers = rows[0]
    width = len(headers)
    body: list[tuple[str, ...]] = []
    for number, row in enumerate(rows[1:], start=2):
        if len(row) != width:
            # Ragged rows are padded rather than dropped, and reported: a row
            # with a missing trailing field is usually still a real patient.
            warnings.append(f"Row {number} has {len(row)} fields, expected {width}.")
            row = row[:width] + ("",) * max(0, width - len(row))
        body.append(row)

    return ReadResult(
        sheets=(
            Sheet(
                name=path.stem,
                headers=headers,
                rows=tuple(body),
                header_row=1,
                warnings=tuple(warnings),
            ),
        ),
        encoding=encoding,
        delimiter=delimiter,
        warnings=tuple(warnings),
    )


# --------------------------------------------------------------------- Excel
def _cell_text(value: object) -> str:
    """Render a cell as text without inventing a format.

    Dates and times are rendered ISO-first so a later normalizer can tell a real
    date cell from a string that merely looks like one. Floats keep full
    precision: `1.23457e+11` must stay visibly damaged rather than be rounded
    into a plausible document number.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        # 1045678901.0 is a document number Excel stored as a float.
        return str(int(value))
    return str(value).strip()


def _header_score(cells: list[str]) -> float:
    """How much a row looks like column labels rather than data."""
    filled = [cell for cell in cells if cell]
    if len(filled) < 2:
        return 0.0
    distinct = len(set(filled)) / len(filled)
    textual = sum(1 for cell in filled if not cell.replace(".", "", 1).isdigit()) / len(filled)
    short = sum(1 for cell in filled if len(cell) <= 40) / len(filled)
    density = len(filled) / max(len(cells), 1)
    return distinct + textual + short + density


def _find_header_row(worksheet: Worksheet, probe: int = 25) -> int:
    """Return the 1-based row that begins the FIRST table on the sheet.

    Hand-made workbooks put a clinic name and a print date above the table, so
    row 1 is often not the header. They also paste a second table below the
    first, and that second table frequently scores higher because it is smaller
    and tidier. Taking the best-scoring row anywhere on the sheet therefore
    skips the real data entirely, so the first row that scores well enough wins,
    not the best row overall.
    """
    scores: list[tuple[int, float, int]] = []  # (row, score, filled cells)
    for row_index, row in enumerate(
        worksheet.iter_rows(min_row=1, max_row=probe, values_only=True), start=1
    ):
        cells = [_cell_text(value) for value in row]
        filled = sum(1 for cell in cells if cell)
        scores.append((row_index, _header_score(cells), filled))

    if not scores:
        return 1
    best_score = max(score for _, score, _ in scores)
    if best_score <= 0:
        return 1

    # A header must span most of the table, which rules out a title row: merged
    # across the sheet it still reads as one filled cell. Width is measured
    # against the widest row on the sheet rather than against other candidates,
    # because a header with an unlabelled column ("NOMBRE", "", "", "EPS") is
    # narrower than the data beneath it and would otherwise lose to row 2 —
    # promoting a patient row to the header and dropping that patient.
    table_width = max(filled for _, _, filled in scores)
    for row_index, score, filled in scores:
        if score >= best_score * 0.85 and filled >= table_width * 0.5:
            return row_index
    return 1


def _formula_text(path: Path) -> dict[tuple[str, int, int], str]:
    """Formula text for cells that carry a formula but no cached result.

    `data_only=True` returns the value Excel last calculated. A workbook written
    by a script, or never opened since editing, has no cached value, so every
    formula cell reads as empty — a whole column of patient data silently blank
    with no error anywhere. Falling back to the formula's own text keeps the
    cell visible so validation can reject it, and keeps an injected payload
    intact so the export layer can neutralise it rather than lose it.
    """
    workbook = openpyxl.load_workbook(path, read_only=False, data_only=False)
    try:
        formulas: dict[tuple[str, int, int], str] = {}
        for worksheet in workbook.worksheets:
            for row in worksheet.iter_rows():
                for cell in row:
                    if isinstance(cell.value, str) and cell.value.startswith("="):
                        formulas[(worksheet.title, cell.row, cell.column)] = cell.value
        return formulas
    finally:
        workbook.close()


@dataclass(frozen=True, slots=True)
class _SheetStructure:
    hidden_rows: frozenset[int]
    hidden_columns: tuple[str, ...]
    state: str


def _structure(path: Path) -> dict[str, _SheetStructure]:
    """Hidden rows, hidden columns and visibility per sheet.

    Read-only worksheets do not carry row or column dimensions, so this costs a
    second pass with a normal load. It is worth it: a hidden row holds a real
    patient, and a hidden column often holds the code the clinic actually keys
    on, so neither may be discovered only by accident.
    """
    workbook = openpyxl.load_workbook(path, read_only=False, data_only=True)
    try:
        structure: dict[str, _SheetStructure] = {}
        for worksheet in workbook.worksheets:
            hidden_rows = frozenset(
                number for number, dimension in worksheet.row_dimensions.items() if dimension.hidden
            )
            hidden_columns = tuple(
                letter
                for letter, dimension in worksheet.column_dimensions.items()
                if dimension.hidden
            )
            structure[worksheet.title] = _SheetStructure(
                hidden_rows=hidden_rows,
                hidden_columns=hidden_columns,
                state=str(worksheet.sheet_state),
            )
        return structure
    finally:
        workbook.close()


def read_excel(path: Path) -> ReadResult:
    inspect_archive(path)
    try:
        structure = _structure(path)
    except UnreadableFile:
        raise
    except Exception as error:  # openpyxl raises OSError, KeyError, zipfile errors...
        raise UnreadableFile(f"File is not a readable workbook: {error}") from error

    # data_only=True returns the cached result of a formula rather than its text.
    try:
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
        formulas = _formula_text(path)
    except Exception as error:
        raise UnreadableFile(f"File is not a readable workbook: {error}") from error
    try:
        sheets: list[Sheet] = []
        warnings: list[str] = []

        for worksheet in workbook.worksheets:
            # The declared dimension is a claim by the file. openpyxl trusts it
            # in read-only mode, so a sheet declaring A1:A1 over 5,000 real rows
            # silently yields one cell. Recomputing it from the data is the
            # documented remedy.
            worksheet.reset_dimensions()

            found = structure.get(worksheet.title, _SheetStructure(frozenset(), (), "visible"))
            hidden_rows_all, hidden_columns = found.hidden_rows, found.hidden_columns
            sheet_warnings: list[str] = []
            if found.state != "visible":
                sheet_warnings.append(
                    f"Sheet '{worksheet.title}' is hidden; it may hold stale data."
                )

            header_row = _find_header_row(worksheet)
            if header_row > 1:
                sheet_warnings.append(
                    f"Header found on row {header_row}; rows above it were skipped."
                )

            rows = list(worksheet.iter_rows(min_row=header_row, values_only=True))
            if not rows:
                continue

            headers = tuple(_cell_text(value) for value in rows[0])
            width = len(headers)
            body: list[tuple[str, ...]] = []
            uncalculated = 0
            for row_number, row in enumerate(rows[1:], start=header_row + 1):
                cells: list[str] = []
                for column_index, value in enumerate(row[:width], start=1):
                    text = _cell_text(value)
                    if not text:
                        formula = formulas.get((worksheet.title, row_number, column_index))
                        if formula:
                            # Keep the formula visible: validation rejects it,
                            # and the export layer neutralises it.
                            text = formula
                            uncalculated += 1
                    cells.append(text)
                cells.extend("" for _ in range(width - len(cells)))
                if any(cells):
                    body.append(tuple(cells))
            if uncalculated:
                sheet_warnings.append(
                    f"{uncalculated} cell(s) hold a formula with no calculated value; "
                    "open the file in Excel, recalculate and save before importing."
                )

            hidden_rows = tuple(sorted(n for n in hidden_rows_all if n > header_row))
            if hidden_rows:
                sheet_warnings.append(
                    f"{len(hidden_rows)} hidden row(s) were imported and flagged for review."
                )
            if hidden_columns:
                sheet_warnings.append(f"Hidden column(s) {', '.join(hidden_columns)} contain data.")

            sheets.append(
                Sheet(
                    name=worksheet.title,
                    headers=headers,
                    rows=tuple(body),
                    header_row=header_row,
                    hidden_row_numbers=hidden_rows,
                    hidden_columns=hidden_columns,
                    warnings=tuple(sheet_warnings),
                )
            )
            warnings.extend(sheet_warnings)

        if not sheets:
            raise UnreadableFile("Workbook contains no readable sheet.")
        return ReadResult(sheets=tuple(sheets), warnings=tuple(warnings))
    finally:
        workbook.close()


def read(path: Path) -> ReadResult:
    """Read any supported file into sheets of strings, or refuse it."""
    kind = detect_kind(path)
    if kind == "csv":
        return read_csv(path)
    if kind == "xls":
        raise UnreadableFile(
            "Legacy .xls is not supported. Save the file as .xlsx and upload it again."
        )
    return read_excel(path)
