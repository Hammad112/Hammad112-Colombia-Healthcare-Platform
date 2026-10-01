"""Validation that is a property of the whole file, not of one cell.

The scope's validation layer is "types, duplicates, referential integrity".
Types are per cell and live in `test_onboarding_normalizers.py`. These two
cannot be: whether a row duplicates another, or points at a doctor that exists,
is only answerable once every row of every sheet has been converted.

Both are guards against an import that looks successful and is wrong — a patient
written twice under one cédula, or an appointment with nobody attending it.
"""

from __future__ import annotations

from src.onboarding import service
from src.onboarding.canonical import Entity


def _patient(number: int, **values: object) -> service.RowResult:
    return service.RowResult(row_number=number, entity=Entity.PATIENT, raw={}, values=values)


def _appointment(number: int, **values: object) -> service.RowResult:
    return service.RowResult(row_number=number, entity=Entity.APPOINTMENT, raw={}, values=values)


# ------------------------------------------------------------------ duplicates
def test_a_repeated_document_is_reported_against_the_row_it_repeats() -> None:
    """Naming the earlier row is what makes the message actionable.

    "3 duplicates" sends a receptionist hunting; "the same cédula as row 2" is
    something they can look at. The first occurrence is the record, not a
    duplicate.
    """
    rows = [
        _patient(2, document_type="CC", document_number="1020304050"),
        _patient(3, document_type="CC", document_number="1020304051"),
        _patient(4, document_type="CC", document_number="1020304050"),
    ]
    found = service.find_duplicates(rows, Entity.PATIENT)

    assert set(found) == {4}
    assert "row 2" in found[4]


def test_the_same_number_under_a_different_document_type_is_not_a_duplicate() -> None:
    """A cédula and a tarjeta de identidad may share digits legitimately.

    Identity is the pair, which is what `_apply_patients` matches on. Keying on
    the number alone would refuse two real, different people.
    """
    rows = [
        _patient(2, document_type="CC", document_number="1020304050"),
        _patient(3, document_type="TI", document_number="1020304050"),
    ]
    assert service.find_duplicates(rows, Entity.PATIENT) == {}


def test_duplicate_detection_ignores_case_and_surrounding_space() -> None:
    """Excel leaves trailing spaces, and clinics write both cc and CC."""
    rows = [
        _patient(2, document_type="CC", document_number="1020304050"),
        _patient(3, document_type="cc", document_number=" 1020304050 "),
    ]
    assert set(service.find_duplicates(rows, Entity.PATIENT)) == {3}


def test_a_row_missing_its_identity_is_not_called_a_duplicate() -> None:
    """An empty cédula is already a per-cell refusal.

    Reporting it again as a duplicate would make the screen noisier without
    saying anything new, and two rows with no cédula are not evidence that they
    are the same person.
    """
    rows = [
        _patient(2, document_type="CC", document_number=""),
        _patient(3, document_type="CC", document_number=""),
    ]
    assert service.find_duplicates(rows, Entity.PATIENT) == {}


# ------------------------------------------------------- referential integrity
def test_an_appointment_naming_an_unknown_doctor_is_reported() -> None:
    """Otherwise it imports as an appointment with nobody attending it."""
    rows = [_appointment(2, doctor_ref="D01"), _appointment(3, doctor_ref="D99")]
    known = {"doctor_ref": {"d01"}, "patient_document": set[str]()}
    found = service.find_dangling_references(rows, Entity.APPOINTMENT, known=known)

    assert set(found) == {3}
    assert "D99" in found[3]


def test_a_reference_satisfied_by_another_sheet_is_accepted() -> None:
    """A workbook that defines its own doctors must not reject its own rows."""
    rows = [_appointment(2, doctor_ref="Dra. Ana Perez")]
    known = {"doctor_ref": {"dra. ana perez"}, "patient_document": set[str]()}
    assert service.find_dangling_references(rows, Entity.APPOINTMENT, known=known) == {}


def test_an_absent_reference_is_left_to_the_per_cell_rules() -> None:
    """A missing required field is already refused by the normalizers.

    This stage answers "does it point at something real", not "is it there".
    """
    rows = [_appointment(2, doctor_ref="")]
    known = {"doctor_ref": set[str](), "patient_document": set[str]()}
    assert service.find_dangling_references(rows, Entity.APPOINTMENT, known=known) == {}


def test_an_entity_with_no_references_is_not_checked() -> None:
    rows = [_patient(2, document_type="CC", document_number="1020304050")]
    known = {"doctor_ref": set[str](), "patient_document": set[str]()}
    assert service.find_dangling_references(rows, Entity.PATIENT, known=known) == {}


def test_two_columns_for_one_name_field_join_in_the_files_order() -> None:
    """The mapping arrives from JSONB, which does not preserve insertion order.

    Iterating the mapping joined "primerApellido" and "segundoApellido" in
    whatever order the database handed back, so a patient whose file says
    "Perez Gomez" was stored as "Gomez Perez" -- the surnames reversed, marked
    valid. The file's own column order is the only order that means anything.
    """
    from src.onboarding.reader import Sheet

    sheet = Sheet(
        name="Pacientes",
        headers=("TIPO DOC", "IDENTIFICACION", "NOMBRES", "PRIMER APELLIDO", "SEGUNDO APELLIDO"),
        rows=(("CC", "1020304050", "Ana", "Perez", "Gomez"),),
        header_row=1,
    )
    # Deliberately scrambled, as a JSONB round trip would return it.
    mapping = {
        "IDENTIFICACION": "document_number",
        "TIPO DOC": "document_type",
        "SEGUNDO APELLIDO": "family_names",
        "NOMBRES": "given_names",
        "PRIMER APELLIDO": "family_names",
    }

    rows, _ = service.validate(sheet, Entity.PATIENT, mapping, decisions={})
    assert rows[0].values["family_names"] == "Perez Gomez"
