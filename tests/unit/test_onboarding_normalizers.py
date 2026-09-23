"""Deterministic conversion of clinic values.

Two rules are being guarded, and they are opposites:

    ambiguous  -> REVIEW   a person decides, because the data cannot
    damaged    -> INVALID  the row is rejected, because the value is unrecoverable

Most of these tests assert a *refusal*. That is deliberate: every silent
conversion of an ambiguous value writes a plausible wrong record that nothing
downstream can detect, which is the failure the client was burned by before.
"""

from __future__ import annotations

import datetime as dt

import pytest

from src.onboarding import normalizers as n
from src.registry.models import DocumentType


# ------------------------------------------------------------- document type
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("CC", DocumentType.CC),
        ("C.C.", DocumentType.CC),
        ("cédula de ciudadanía", DocumentType.CC),
        ("ID Card", DocumentType.CC),  # the client's own file writes it in English
        ("Minor ID", DocumentType.TI),
        ("T.I.", DocumentType.TI),
        # PPT is what the card is called; PT is the RIPS code. Clinics write both.
        ("PPT", DocumentType.PT),
        ("PT", DocumentType.PT),
        ("PEP", DocumentType.PE),
    ],
)
def test_document_types_are_recognised_however_they_are_written(
    raw: str, expected: DocumentType
) -> None:
    outcome = n.document_type(raw)
    assert outcome.ok
    assert outcome.value is expected


def test_an_unknown_document_type_is_reviewed_never_defaulted() -> None:
    """Defaulting to CC pairs a real number with the wrong legal identity."""
    outcome = n.document_type("Carné")
    assert outcome.status is n.Status.REVIEW
    assert outcome.value is None


def test_de_is_not_matched_inside_another_label() -> None:
    """`DE` is a document code and also the preposition in every other label."""
    assert n.document_type("cedula de ciudadania").value is DocumentType.CC
    assert n.document_type("DE").value is DocumentType.DE


# ----------------------------------------------------------- document number
def test_a_document_number_keeps_its_digits() -> None:
    assert n.document_number("1045678901").value == "1045678901"
    # The Spanish thousands form loses nothing, so it is accepted.
    assert n.document_number("1.045.678.901").value == "1045678901"
    # Excel writing a whole number as a float loses nothing either.
    assert n.document_number("1045678901.0").value == "1045678901"


def test_scientific_notation_is_rejected_not_repaired() -> None:
    """`1.23457E+11` has lost its low-order digits; there is nothing to recover.

    Rounding it back produces a number that belongs to somebody else, so the row
    is rejected and the clinic is asked to re-export the column as text.
    """
    outcome = n.document_number("1.23457E+11")
    assert outcome.status is n.Status.INVALID
    assert "text" in outcome.message


def test_a_passport_keeps_its_letters() -> None:
    assert n.document_number("E0456789").value == "E0456789"


def test_a_number_too_short_to_be_a_document_is_rejected() -> None:
    """A cédula that lost its leading zeros is a different person's number."""
    assert n.document_number("123").status is n.Status.INVALID


# ------------------------------------------------------------------- phone
def test_colombian_mobiles_become_e164() -> None:
    assert n.phone("3001234567").value == "+573001234567"
    assert n.phone("+57 311 987 6543").value == "+573119876543"
    assert n.phone("(311) 987-6543").value == "+573119876543"


@pytest.mark.parametrize("raw", ["3067891234", "3098765432", "30987654"])
def test_an_unreachable_number_is_flagged_never_corrected(raw: str) -> None:
    """These three prefixes are unassigned, so the number reaches nobody.

    It is not repaired: the nearest valid number belongs to a stranger, and a
    reminder sent there discloses an appointment to the wrong person. The
    patient is still imported; only the number is flagged.
    """
    outcome = n.phone(raw)
    assert outcome.status is n.Status.REVIEW
    assert outcome.value is None


# ----------------------------------------------------------------- boolean
@pytest.mark.parametrize("raw", ["SI", "Sí", "sí", "X", "1", "TRUE", "yes"])
def test_affirmatives_are_read_as_true(raw: str) -> None:
    assert n.boolean(raw).value is True


@pytest.mark.parametrize("raw", ["NO", "no", "0", "FALSE"])
def test_negatives_are_read_as_false(raw: str) -> None:
    assert n.boolean(raw).value is False


@pytest.mark.parametrize("raw", ["", "N/A", "NA", "?"])
def test_an_unclear_boolean_is_reviewed_not_assumed_false(raw: str) -> None:
    """An empty attendance cell means "not recorded", not "did not attend".

    `NA` is worse: in Spanish clinic files it is either "no aplica" or
    "no asistió", which are opposite facts about the same patient.
    """
    assert n.boolean(raw).status is n.Status.REVIEW


# -------------------------------------------------------------------- names
@pytest.mark.parametrize(
    ("raw", "given", "family"),
    [
        ("Carlos Andrés Pérez Gómez", "Carlos Andrés", "Pérez Gómez"),
        ("María José Pérez Gómez", "María José", "Pérez Gómez"),
        ("Ana Gómez", "Ana", "Gómez"),
        # A particle belongs to the surname it introduces.
        ("Juan de la Cruz Pérez Gómez", "Juan de la Cruz", "Pérez Gómez"),
        # Three parts are decidable when the first two are a known pair.
        ("María José Pérez", "María José", "Pérez"),
    ],
)
def test_decidable_names_are_split(raw: str, given: str, family: str) -> None:
    outcome = n.split_full_name(raw)
    assert outcome.ok, outcome.message
    assert outcome.value == n.SplitName(given, family)


def test_a_three_part_name_is_refused_because_it_is_genuinely_ambiguous() -> None:
    """ "Carlos Pérez Gómez" and "Juan Carlos Pérez" have the same shape.

    One is a given name and two surnames; the other is two given names and one
    surname. Nothing in the text separates them, and Ley 2129 de 2021 lets
    parents choose the order of surnames, so no positional rule helps either.
    """
    outcome = n.split_full_name("Carlos Pérez Gómez")
    assert outcome.status is n.Status.REVIEW
    assert "confirm" in outcome.message.lower()


@pytest.mark.parametrize("raw", ["Ana", "Luis Carlos Vélez de Uribe Restrepo", ""])
def test_names_of_unsupported_shape_are_refused(raw: str) -> None:
    assert n.split_full_name(raw).status is n.Status.REVIEW


# -------------------------------------------------------------------- dates
def test_a_column_with_a_day_above_twelve_decides_the_whole_column() -> None:
    """The decision is made once per column, then applied strictly to every row."""
    assert n.detect_day_first(["03/04/1991", "15/10/2026"]) is n.DayFirst.DAY_FIRST
    assert n.detect_day_first(["03/04/1991", "10/15/2026"]) is n.DayFirst.MONTH_FIRST


def test_a_column_that_contradicts_itself_is_undecided() -> None:
    assert n.detect_day_first(["15/10/2026", "10/15/2026"]) is n.DayFirst.UNDECIDED


def test_a_fully_ambiguous_column_is_undecided_and_its_values_are_reviewed() -> None:
    """`03/04/1991` is 3 April or 4 March, and a birth date must not be guessed.

    Falling back to a Colombian locale would be a guess too: the file was
    written by an Excel whose locale we do not know.
    """
    assert n.detect_day_first(["03/04/1991", "05/06/1985"]) is n.DayFirst.UNDECIDED
    outcome = n.date("03/04/1991", order=n.DayFirst.UNDECIDED)
    assert outcome.status is n.Status.REVIEW


def test_dates_convert_under_a_decided_order() -> None:
    assert n.date("15/10/2026", order=n.DayFirst.DAY_FIRST).value == dt.date(2026, 10, 15)
    assert n.date("10/15/2026", order=n.DayFirst.MONTH_FIRST).value == dt.date(2026, 10, 15)
    assert n.date("2026-10-15", order=n.DayFirst.DAY_FIRST).value == dt.date(2026, 10, 15)
    # openpyxl hands back real date cells already converted, with a time part.
    assert n.date("1990-03-04 00:00:00", order=n.DayFirst.DAY_FIRST).value == dt.date(1990, 3, 4)


def test_an_impossible_date_is_rejected() -> None:
    assert n.date("31/02/2026", order=n.DayFirst.DAY_FIRST).status is n.Status.INVALID


def test_excel_serials_before_march_1900_are_rejected() -> None:
    """Excel's 1900 system contains a 29 February that never existed.

    Verified against openpyxl: serials 59 and 60 both convert to 1900-02-28, so
    two different dates collapse onto one. Anything in that range is refused
    rather than silently mapped to the wrong day.
    """
    assert n.date("60", order=n.DayFirst.DAY_FIRST).status is n.Status.INVALID
    assert n.date("59", order=n.DayFirst.DAY_FIRST).status is n.Status.INVALID
    # Past the broken range the 1900 system is reliable.
    assert n.date("61", order=n.DayFirst.DAY_FIRST).value == dt.date(1900, 3, 1)


def test_the_1904_epoch_is_supported() -> None:
    """Workbooks saved on a Mac count from 1904; mixing the two shifts by 1462 days."""
    assert n.date("0", order=n.DayFirst.DAY_FIRST, epoch_1904=True).value == dt.date(1904, 1, 1)


# -------------------------------------------------------------------- times
def test_twenty_four_hour_times_convert() -> None:
    assert n.time_of_day("07:00").value == dt.time(7, 0)
    assert n.time_of_day("16:45").value == dt.time(16, 45)


def test_the_spanish_twelve_hour_form_converts_including_odd_spaces() -> None:
    """es-CO Excel separates "a. m." with U+00A0 or U+202F, not a plain space.

    A parser that splits on an ordinary space fails on every time in the file,
    silently, because the value still looks like text.
    """
    assert n.time_of_day("8:30 a. m.").value == dt.time(8, 30)
    assert n.time_of_day("2:30 p. m.").value == dt.time(14, 30)
    assert n.time_of_day("8:30 a. m.").value == dt.time(8, 30)
    assert n.time_of_day("2:30 PM").value == dt.time(14, 30)


def test_midnight_and_noon_are_not_confused() -> None:
    assert n.time_of_day("12:00 a. m.").value == dt.time(0, 0)
    assert n.time_of_day("12:00 p. m.").value == dt.time(12, 0)


def test_a_time_stored_as_a_fraction_is_rounded_not_truncated() -> None:
    """0.354166666 of a day is 30599.99994 seconds.

    Truncating gives 08:29:59, a minute earlier than the clinic wrote, and the
    error is invisible because the result is still a valid time.
    """
    assert n.time_of_day("0.354166666").value == dt.time(8, 30)
    assert n.time_of_day("0.604166666").value == dt.time(14, 30)


def test_an_impossible_time_is_rejected() -> None:
    assert n.time_of_day("25:00").status is n.Status.INVALID


# ------------------------------------------------------------------- status
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Confirmada", "confirmed"),
        ("Atendida", "completed"),
        ("asistió", "completed"),
        ("no asistió", "no_show"),
        ("Inasistencia", "no_show"),
        ("Cancelada", "cancelled"),
        ("Reprogramada", "rescheduled"),
        ("Agendada", "scheduled"),
    ],
)
def test_spanish_statuses_map_to_the_canonical_set(raw: str, expected: str) -> None:
    outcome = n.appointment_status(raw)
    assert outcome.ok
    assert outcome.value == expected


@pytest.mark.parametrize("raw", ["Pendiente", "NA", "N/A"])
def test_a_status_with_two_meanings_is_reviewed(raw: str) -> None:
    """`pendiente` is both "not yet confirmed" and "on the waiting list".

    `NA` is either "no aplica" or "no asistió" — one says nothing about
    attendance, the other says the patient did not come.
    """
    assert n.appointment_status(raw).status is n.Status.REVIEW


# ------------------------------------------------------------------ general
def test_accents_are_stripped_for_matching_but_never_for_storage() -> None:
    """Unaccented spelling is the norm in real files, so matching must ignore it.

    Storage must not: "Muñoz" and "Munoz" are different names, and restoring an
    accent a file does not carry would be inventing data.
    """
    assert n.strip_accents("TELÉFONO") == "telefono"
    assert n.strip_accents("Muñoz") == "munoz"
    # The value itself survives untouched through a normalizer that stores it.
    assert n.split_full_name("Diana Muñoz").value.family_names == "Muñoz"
