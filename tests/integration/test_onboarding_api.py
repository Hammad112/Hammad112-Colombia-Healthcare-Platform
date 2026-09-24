"""The onboarding API, driven the way a reviewer drives it from Swagger UI.

The guarantee these tests exist for is the milestone's exit criterion: **no
import commits data that fails validation**. Everything else here supports it —
that a file is read, that its columns are proposed, that a person can correct
them — but the tests that must never be weakened are the ones asserting a
refusal.
"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.core.tenancy import ClinicScope, apply_clinic_scope
from tests.integration.factories import create_graph, scope_client

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "onboarding"


@pytest.fixture(scope="module", autouse=True)
def fixtures_exist() -> None:
    if not (FIXTURES / "1_clean_ips.xlsx").exists():
        subprocess.run(
            [sys.executable, "-m", "tests.fixtures.onboarding.generate"],
            check=True,
            capture_output=True,
        )


@pytest.fixture
async def scoped(session, client: TestClient) -> Iterator[TestClient]:  # type: ignore[no-untyped-def]
    """A client already scoped to a real clinic, as every route requires.

    The clinic scope is re-applied after the commit: `set_config(..., true)` is
    transaction-local, so committing clears it and a test that then queries
    directly would see nothing — row-level security working exactly as intended.
    """
    graph = await create_graph(session)
    await session.commit()
    await apply_clinic_scope(session, ClinicScope(clinic_id=graph.clinic_id))
    yield scope_client(client, graph)


def _clinic_of(client: TestClient) -> uuid.UUID:
    """The clinic the scoped client sends on every request."""
    return uuid.UUID(str(client.params["clinic_id"]))


def _upload(client: TestClient, name: str) -> dict:  # type: ignore[type-arg]
    with (FIXTURES / name).open("rb") as handle:
        response = client.post("/onboarding/uploads", files={"file": (name, handle)})
    assert response.status_code == 201, response.text
    return response.json()


# ------------------------------------------------------------------- reading
async def test_a_spanish_export_is_mapped_without_any_correction(scoped: TestClient) -> None:
    """The control: ordinary Spanish headings resolve with no model and no edits."""
    body = _upload(scoped, "1_clean_ips.xlsx")
    sheets = {s["sheet"]: s for s in body["sheets"]}

    assert sheets["Pacientes"]["entity"] == "patient"
    assert all(column["auto"] for column in sheets["Pacientes"]["columns"])
    assert sheets["Pacientes"]["missing_required"] == []
    assert sheets["Medicos"]["entity"] == "doctor"
    assert sheets["Citas"]["entity"] == "appointment"


async def test_structural_findings_are_reported_to_the_reviewer(scoped: TestClient) -> None:
    """Hidden rows and a header below title rows change what was imported."""
    body = _upload(scoped, "2_receptionist.xlsx")
    warnings = " ".join(w for s in body["sheets"] for w in s["warnings"])
    assert "hidden row" in warnings
    assert "Header found on row" in warnings


async def test_a_spanish_csv_is_read_with_the_right_encoding(scoped: TestClient) -> None:
    body = _upload(scoped, "3_excel_csv_es.csv")
    assert body["encoding"] == "cp1252"
    assert body["delimiter"] == ";"


# ------------------------------------------------------------------ refusals
@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("5_zip_bomb.xlsx", 413),  # rejected on size before it is parsed
        ("5_not_really_csv.csv", 422),  # an executable renamed .csv
        ("5_xxe.xlsx", 422),  # an external entity in the workbook XML
    ],
)
def test_hostile_files_are_refused_at_upload(
    scoped: TestClient, fixture: str, expected: int
) -> None:
    with (FIXTURES / fixture).open("rb") as handle:
        response = scoped.post("/onboarding/uploads", files={"file": (fixture, handle)})
    assert response.status_code == expected, response.text


# --------------------------------------------------------------- the pipeline
async def test_validation_counts_every_row_not_a_sample(scoped: TestClient) -> None:
    """ADR-08a: the screen shows percent valid per column, not a preview.

    A preview of the first rows looks fine while row 100 is broken, which is how
    a bad transform reaches production.
    """
    body = _upload(scoped, "1_clean_ips.xlsx")
    response = scoped.post(f"/onboarding/uploads/{body['session_id']}/validate")
    assert response.status_code == 200, response.text

    patients = next(s for s in response.json()["sheets"] if s["sheet"] == "Pacientes")
    assert patients["total_rows"] == 10
    assert patients["valid_rows"] + patients["review_rows"] + patients["invalid_rows"] == 10

    phone = next(c for c in patients["columns"] if c["target_field"] == "phone_e164")
    assert phone["total"] == 10
    # Two of the fixture's numbers use unassigned prefixes and cannot be reached.
    assert phone["review"] == 2
    assert phone["percent_valid"] == 80.0


async def test_the_transform_log_names_the_rule_for_every_cell(scoped: TestClient) -> None:
    """The evidence that no transform was inferred from the file's contents."""
    body = _upload(scoped, "1_clean_ips.xlsx")
    scoped.post(f"/onboarding/uploads/{body['session_id']}/validate")

    response = scoped.get(
        f"/onboarding/uploads/{body['session_id']}/transform-log",
        params={"sheet": "Pacientes", "limit": 50},
    )
    assert response.status_code == 200
    cells = response.json()
    assert cells
    assert all(cell["rule"] for cell in cells)
    assert any(cell["rule"] == "document_number.digits" for cell in cells)


async def test_rows_needing_review_explain_themselves(scoped: TestClient) -> None:
    """A reviewer needs the row number and the reason, not a count."""
    body = _upload(scoped, "1_clean_ips.xlsx")
    scoped.post(f"/onboarding/uploads/{body['session_id']}/validate")

    response = scoped.get(
        f"/onboarding/uploads/{body['session_id']}/rows",
        params={"sheet": "Pacientes", "status": "review"},
    )
    rows = response.json()
    assert len(rows) == 2
    assert all(row["row_number"] for row in rows)
    assert any("cannot be reached" in reason for row in rows for reason in row["reviews"])


# ------------------------------------------------- the guarantee that matters
async def test_commit_refuses_while_a_question_is_unanswered(scoped: TestClient) -> None:
    """The corrupted fixture's birth dates are genuinely ambiguous.

    An earlier version counted invalid rows only, so this file — blocked on an
    unanswered date-format question — committed anyway. `commit` now refuses on
    exactly what `validate` reported, so the two cannot disagree.
    """
    body = _upload(scoped, "4_corrupted.xlsx")
    validation = scoped.post(f"/onboarding/uploads/{body['session_id']}/validate").json()
    assert validation["can_commit"] is False
    assert validation["blocking"]

    response = scoped.post(f"/onboarding/uploads/{body['session_id']}/commit")
    assert response.status_code == 409
    assert "Refusing to commit" in response.json()["detail"]


async def test_commit_refuses_before_validation_has_run(scoped: TestClient) -> None:
    body = _upload(scoped, "1_clean_ips.xlsx")
    response = scoped.post(f"/onboarding/uploads/{body['session_id']}/commit")
    assert response.status_code == 409


async def test_a_clean_file_reaches_commit(scoped: TestClient) -> None:
    body = _upload(scoped, "1_clean_ips.xlsx")
    validation = scoped.post(f"/onboarding/uploads/{body['session_id']}/validate").json()
    assert validation["can_commit"] is True

    response = scoped.post(f"/onboarding/uploads/{body['session_id']}/commit")
    assert response.status_code == 200
    assert response.json()["committed"]["Pacientes"] == 8


# ------------------------------------------------------------------- mapping
async def test_a_reviewer_can_correct_the_proposed_mapping(scoped: TestClient) -> None:
    body = _upload(scoped, "1_clean_ips.xlsx")
    response = scoped.put(
        f"/onboarding/uploads/{body['session_id']}/mapping",
        json={
            "sheet": "Pacientes",
            "mapping": {"Celular": "phone_fixed", "Correo Electrónico": None},
            "decisions": {},
        },
    )
    assert response.status_code == 200, response.text


async def test_two_columns_cannot_be_mapped_to_one_field(scoped: TestClient) -> None:
    """One would silently overwrite the other."""
    body = _upload(scoped, "1_clean_ips.xlsx")
    response = scoped.put(
        f"/onboarding/uploads/{body['session_id']}/mapping",
        json={
            "sheet": "Pacientes",
            "mapping": {"Celular": "phone_e164", "Correo Electrónico": "phone_e164"},
        },
    )
    assert response.status_code == 422
    assert "overwrite" in response.json()["detail"]


async def test_a_column_the_sheet_does_not_have_is_rejected(scoped: TestClient) -> None:
    body = _upload(scoped, "1_clean_ips.xlsx")
    response = scoped.put(
        f"/onboarding/uploads/{body['session_id']}/mapping",
        json={"sheet": "Pacientes", "mapping": {"No Such Column": "eps"}},
    )
    assert response.status_code == 422


# ----------------------------------------------------------------- isolation
async def test_another_clinic_cannot_see_the_session(scoped: TestClient, session) -> None:  # type: ignore[no-untyped-def]
    """A session is clinic data: its existence is not disclosed either."""
    body = _upload(scoped, "1_clean_ips.xlsx")
    other = await create_graph(session, name="Clínica Otra")
    await session.commit()

    response = scoped.get(
        f"/onboarding/uploads/{body['session_id']}",
        params={"clinic_id": str(other.clinic_id)},
    )
    assert response.status_code == 404


def test_the_canonical_fields_are_published_for_the_screen(scoped: TestClient) -> None:
    """The confirmation screen's dropdowns come from here."""
    response = scoped.get("/onboarding/canonical-fields")
    assert response.status_code == 200
    fields = response.json()
    assert {"patient", "doctor", "appointment"} <= set(fields)
    names = {f["name"] for f in fields["patient"]}
    assert {"document_number", "birth_date", "phone_e164"} <= names


# ------------------------------------------------- persistence and profiles
async def test_the_import_is_stored_not_held_in_memory(scoped: TestClient, session) -> None:  # type: ignore[no-untyped-def]
    """Staged rows and the transform log live in the database.

    An import that existed only in the API process would vanish on restart, and
    could not be audited afterwards.
    """
    from sqlalchemy import func, select

    from src.onboarding.models import ImportSession, StagingRow, TransformLogEntry

    body = _upload(scoped, "1_clean_ips.xlsx")
    scoped.post(f"/onboarding/uploads/{body['session_id']}/validate")

    session_id = uuid.UUID(body["session_id"])
    stored = await session.get(ImportSession, session_id)
    assert stored is not None
    assert stored.status == "validated"
    assert stored.total_rows == 20
    assert stored.valid_rows and stored.review_rows is not None

    staged = await session.scalar(
        select(func.count()).select_from(StagingRow).where(StagingRow.session_id == session_id)
    )
    cells = await session.scalar(
        select(func.count())
        .select_from(TransformLogEntry)
        .where(TransformLogEntry.session_id == session_id)
    )
    assert staged == 20
    # One entry per mapped cell of every row: the ADR-08a evidence.
    assert cells > staged


async def test_committing_writes_patients_into_the_clinic(scoped: TestClient, session) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import func, select

    from src.registry.models import Doctor, Patient

    # The clinic fixture already has one patient and one doctor, so the import
    # is measured as a delta rather than a total.
    before_patients = await session.scalar(select(func.count()).select_from(Patient))
    before_doctors = await session.scalar(select(func.count()).select_from(Doctor))

    body = _upload(scoped, "1_clean_ips.xlsx")
    assert scoped.post(f"/onboarding/uploads/{body['session_id']}/validate").json()["can_commit"]

    response = scoped.post(f"/onboarding/uploads/{body['session_id']}/commit")
    assert response.status_code == 200, response.text
    assert response.json()["committed"]["Pacientes"] == 8

    await session.commit()
    await apply_clinic_scope(session, ClinicScope(clinic_id=_clinic_of(scoped)))
    patients = await session.scalar(select(func.count()).select_from(Patient))
    doctors = await session.scalar(select(func.count()).select_from(Doctor))
    # 8 of the 10 rows converted; two carry unreachable phone numbers and wait
    # for review rather than being written with a number that reaches nobody.
    assert patients - before_patients == 8
    assert doctors - before_doctors == 4


async def test_every_imported_patient_is_audited(scoped: TestClient, session) -> None:  # type: ignore[no-untyped-def]
    """A write of patient data is an audited event, exactly like a read."""
    from sqlalchemy import func, select

    from src.audit.models import AccessLogEntry

    body = _upload(scoped, "1_clean_ips.xlsx")
    scoped.post(f"/onboarding/uploads/{body['session_id']}/validate")
    scoped.post(f"/onboarding/uploads/{body['session_id']}/commit")
    await session.commit()
    await apply_clinic_scope(session, ClinicScope(clinic_id=_clinic_of(scoped)))

    created = await session.scalar(
        select(func.count())
        .select_from(AccessLogEntry)
        .where(AccessLogEntry.resource == "patients", AccessLogEntry.action == "create")
    )
    assert created == 8


async def test_a_second_import_reuses_the_confirmed_mapping(scoped: TestClient) -> None:
    """The PDF's exit criterion for repeat imports.

    The mapping a person confirmed is matched by the file's shape, so the same
    export next month needs no review — and no model call.
    """
    first = _upload(scoped, "1_clean_ips.xlsx")
    scoped.post(f"/onboarding/uploads/{first['session_id']}/validate")
    scoped.post(f"/onboarding/uploads/{first['session_id']}/commit")

    profiles = scoped.get("/onboarding/profiles").json()
    assert {p["name"].split(" - ")[-1] for p in profiles} >= {"Pacientes", "Medicos", "Citas"}

    second = _upload(scoped, "1_clean_ips.xlsx")
    assert set(second["reused_profiles"]) >= {"Pacientes", "Medicos", "Citas"}
    # Every column arrives already confirmed, so there is nothing to review.
    patients = next(s for s in second["sheets"] if s["sheet"] == "Pacientes")
    assert all(c["confidence"] in {"confirmed", "not imported"} for c in patients["columns"])
    assert patients["missing_required"] == []


async def test_re_uploading_the_same_file_says_so(scoped: TestClient) -> None:
    """Uploading the same export twice by accident is common; silence is not kind."""
    first = _upload(scoped, "1_clean_ips.xlsx")
    scoped.post(f"/onboarding/uploads/{first['session_id']}/validate")
    scoped.post(f"/onboarding/uploads/{first['session_id']}/commit")

    again = _upload(scoped, "1_clean_ips.xlsx")
    assert again["duplicate_of"] == first["session_id"]


async def test_a_committed_import_cannot_be_committed_twice(scoped: TestClient) -> None:
    body = _upload(scoped, "1_clean_ips.xlsx")
    scoped.post(f"/onboarding/uploads/{body['session_id']}/validate")
    assert scoped.post(f"/onboarding/uploads/{body['session_id']}/commit").status_code == 200

    repeated = scoped.post(f"/onboarding/uploads/{body['session_id']}/commit")
    assert repeated.status_code == 409
    assert "already committed" in repeated.json()["detail"]


# ------------------------------------------------------- confirmation screen
async def test_the_review_screen_shows_the_mapping_and_its_reasons(
    scoped: TestClient,
) -> None:
    """The PDF's confirmation screen deliverable.

    A person must be able to see what each column was taken to mean, and change
    it, before anything is written.
    """
    body = _upload(scoped, "1_clean_ips.xlsx")
    response = scoped.get(f"/onboarding/uploads/{body['session_id']}/review")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")

    page = response.text
    assert "Confirm this import" in page
    # Every column of every sheet is listed, with a dropdown to correct it.
    for column in ("Tipo Documento", "Número Documento", "Celular", "EPS"):
        assert column in page
    assert page.count("<select") >= 8
    # And the reason each proposal was made, so a reviewer can judge it.
    assert "known name" in page


async def test_the_screen_shows_percent_valid_after_validation(scoped: TestClient) -> None:
    """ADR-08a: percent over every row, not a preview of the first few."""
    body = _upload(scoped, "1_clean_ips.xlsx")
    scoped.post(f"/onboarding/uploads/{body['session_id']}/validate")

    page = scoped.get(f"/onboarding/uploads/{body['session_id']}/review").text
    assert "80%" in page  # the phone column: 2 of 10 unreachable
    assert "to review" in page


async def test_the_screen_refuses_to_hide_a_blocked_import(scoped: TestClient) -> None:
    """A reviewer must see why it cannot be imported, on the page itself."""
    body = _upload(scoped, "4_corrupted.xlsx")
    scoped.post(f"/onboarding/uploads/{body['session_id']}/validate")

    page = scoped.get(f"/onboarding/uploads/{body['session_id']}/review").text
    assert "cannot be committed yet" in page
    assert "Needs your decision" in page


async def test_a_reviewer_can_correct_the_mapping_from_the_screen(
    scoped: TestClient,
) -> None:
    body = _upload(scoped, "1_clean_ips.xlsx")
    response = scoped.post(
        f"/onboarding/uploads/{body['session_id']}/review/Pacientes",
        data={"col::Celular": "phone_fixed", "col::EPS": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303

    updated = scoped.get(f"/onboarding/uploads/{body['session_id']}").json()
    patients = next(s for s in updated["sheets"] if s["sheet"] == "Pacientes")
    columns = {c["column"]: c["target_field"] for c in patients["columns"]}
    assert columns["Celular"] == "phone_fixed"
    assert columns["EPS"] is None


async def test_the_screen_can_validate_and_import(scoped: TestClient) -> None:
    """The whole flow without touching Swagger: upload, validate, import."""
    body = _upload(scoped, "1_clean_ips.xlsx")
    session_id = body["session_id"]

    assert (
        scoped.post(
            f"/onboarding/uploads/{session_id}/review/validate", follow_redirects=False
        ).status_code
        == 303
    )
    assert (
        scoped.post(
            f"/onboarding/uploads/{session_id}/review/commit", follow_redirects=False
        ).status_code
        == 303
    )

    page = scoped.get(f"/onboarding/uploads/{session_id}/review").text
    assert "has been committed" in page


async def test_the_screen_does_not_leak_another_clinics_import(scoped: TestClient, session) -> None:  # type: ignore[no-untyped-def]
    body = _upload(scoped, "1_clean_ips.xlsx")
    other = await create_graph(session, name="Clínica Otra")
    await session.commit()

    response = scoped.get(
        f"/onboarding/uploads/{body['session_id']}/review",
        params={"clinic_id": str(other.clinic_id)},
    )
    assert response.status_code == 404


async def test_columns_keep_the_order_the_file_uses(scoped: TestClient) -> None:
    """The reviewer is comparing the screen to the spreadsheet in front of them.

    Rebuilding the report from the mapping reordered the columns and dropped the
    unmapped ones, so a column nobody mapped could not be mapped at all.
    """
    expected = [
        "Tipo Documento",
        "Número Documento",
        "Nombres",
        "Apellidos",
        "Fecha Nacimiento",
        "Celular",
        "Correo Electrónico",
        "EPS",
    ]
    body = _upload(scoped, "1_clean_ips.xlsx")
    before = next(s for s in body["sheets"] if s["sheet"] == "Pacientes")
    assert [c["column"] for c in before["columns"]] == expected

    # Clearing a column must not remove it from the screen, nor move the others.
    scoped.put(
        f"/onboarding/uploads/{body['session_id']}/mapping",
        json={"sheet": "Pacientes", "mapping": {"EPS": None}},
    )
    scoped.post(f"/onboarding/uploads/{body['session_id']}/validate")
    after = scoped.get(f"/onboarding/uploads/{body['session_id']}").json()
    patients = next(s for s in after["sheets"] if s["sheet"] == "Pacientes")
    assert [c["column"] for c in patients["columns"]] == expected
    assert patients["columns"][-1]["target_field"] is None


async def test_an_unmapped_column_stays_visible_after_validation(
    scoped: TestClient,
) -> None:
    """Otherwise a reviewer cannot change their mind about it."""
    body = _upload(scoped, "1_clean_ips.xlsx")
    scoped.put(
        f"/onboarding/uploads/{body['session_id']}/mapping",
        json={"sheet": "Pacientes", "mapping": {"EPS": None}},
    )
    scoped.post(f"/onboarding/uploads/{body['session_id']}/validate")

    page = scoped.get(f"/onboarding/uploads/{body['session_id']}/review").text
    assert "EPS" in page


async def test_a_deleted_patient_is_never_silently_restored(scoped: TestClient, session) -> None:  # type: ignore[no-untyped-def]
    """Deletion is usually a privacy request, so reversing it needs a person.

    A stale export still listing someone who asked to be removed must not put
    them back. The row is skipped and the conflict is reported by name.
    """
    from sqlalchemy import select

    from src.registry.models import Patient

    # Import once, then delete one of the patients it created.
    first = _upload(scoped, "1_clean_ips.xlsx")
    scoped.post(f"/onboarding/uploads/{first['session_id']}/validate")
    scoped.post(f"/onboarding/uploads/{first['session_id']}/commit")
    await session.commit()
    await apply_clinic_scope(session, ClinicScope(clinic_id=_clinic_of(scoped)))

    imported = (
        await session.scalars(
            select(Patient).where(Patient.clinic_id == _clinic_of(scoped)).limit(20)
        )
    ).all()
    victim = next(p for p in imported if p.external_ref is None and p.eps)
    victim.deleted_at = dt.datetime(2026, 3, 1, tzinfo=dt.UTC)
    await session.commit()
    await apply_clinic_scope(session, ClinicScope(clinic_id=_clinic_of(scoped)))

    # The same file again: the deleted patient must not come back.
    again = _upload(scoped, "1_clean_ips.xlsx")
    scoped.post(f"/onboarding/uploads/{again['session_id']}/validate")
    response = scoped.post(f"/onboarding/uploads/{again['session_id']}/commit")
    assert response.status_code == 200, response.text
    assert any("deleted" in c for c in response.json()["conflicts"])

    await session.commit()
    await apply_clinic_scope(session, ClinicScope(clinic_id=_clinic_of(scoped)))
    still_deleted = await session.get(Patient, victim.id)
    assert still_deleted is not None
    assert still_deleted.deleted_at is not None


# ------------------------------------- the messy file the exit criterion turns on
async def test_a_reviewer_can_say_what_an_ambiguous_sheet_holds(scoped: TestClient) -> None:
    """A sheet named for a diary but holding patient columns is genuinely both.

    "AGENDA" means a schedule, so the name reads as appointments, while its
    columns are TIPO DOC, IDENTIFICACION, NOMBRE COMPLETO, CELULAR and EPS.
    Guessing harder would be guessing; the reviewer says which it is, and the
    columns are then matched against that entity's fields.
    """
    body = _upload(scoped, "2_receptionist.xlsx")
    agenda = next(s for s in body["sheets"] if s["sheet"] == "AGENDA")
    assert agenda["entity"] == "appointment"  # what the name alone suggests

    response = scoped.put(
        f"/onboarding/uploads/{body['session_id']}/mapping",
        json={"sheet": "AGENDA", "mapping": {}, "entity": "patient"},
    )
    assert response.status_code == 200
    corrected = next(s for s in response.json()["sheets"] if s["sheet"] == "AGENDA")
    assert corrected["entity"] == "patient"
    # Re-matched, not merely relabelled: the patient fields are now mapped.
    targets = {c["target_field"] for c in corrected["columns"]}
    assert {"document_number", "full_name", "phone_e164"} <= targets


async def test_an_entity_the_system_does_not_have_is_refused(scoped: TestClient) -> None:
    body = _upload(scoped, "2_receptionist.xlsx")
    response = scoped.put(
        f"/onboarding/uploads/{body['session_id']}/mapping",
        json={"sheet": "AGENDA", "mapping": {}, "entity": "invoice"},
    )
    assert response.status_code == 422


async def test_a_stale_sheet_can_be_left_out_of_the_import(scoped: TestClient) -> None:
    """Workbooks carry last month's sheet. Skipping it must be one action.

    "AGOSTO (viejo)" is a superseded copy with no document-type column, so it
    blocks the whole import. Clearing its columns one at a time is not a
    workflow a receptionist would perform, so the sheet is excluded outright.
    """
    body = _upload(scoped, "2_receptionist.xlsx")
    session_id = body["session_id"]

    blocked = scoped.post(f"/onboarding/uploads/{session_id}/validate").json()
    assert any("AGOSTO" in reason for reason in blocked["blocking"])

    scoped.put(
        f"/onboarding/uploads/{session_id}/mapping",
        json={"sheet": "AGOSTO (viejo)", "mapping": {}, "entity": "skip"},
    )
    after = scoped.post(f"/onboarding/uploads/{session_id}/validate").json()
    assert not any("AGOSTO" in reason for reason in after["blocking"])
    assert not any(s["sheet"] == "AGOSTO (viejo)" for s in after["sheets"])


async def test_rows_below_the_table_can_be_excluded(scoped: TestClient) -> None:
    """A hand-kept sheet ends in a total line and a second pasted table.

    The importer refuses to convert those into patients, which is right, but a
    reviewer must be able to say they are not data rather than being stuck.
    """
    body = _upload(scoped, "2_receptionist.xlsx")
    session_id = body["session_id"]
    scoped.put(
        f"/onboarding/uploads/{session_id}/mapping",
        json={"sheet": "AGOSTO (viejo)", "mapping": {}, "entity": "skip"},
    )
    scoped.put(
        f"/onboarding/uploads/{session_id}/mapping",
        json={"sheet": "AGENDA", "mapping": {}, "entity": "patient"},
    )

    with_junk = scoped.post(f"/onboarding/uploads/{session_id}/validate").json()
    assert any("could not be converted" in reason for reason in with_junk["blocking"])

    scoped.put(
        f"/onboarding/uploads/{session_id}/mapping",
        json={"sheet": "AGENDA", "mapping": {}, "excluded_rows": [17, 18, 19, 20]},
    )
    cleaned = scoped.post(f"/onboarding/uploads/{session_id}/validate").json()
    assert cleaned["can_commit"] is True, cleaned["blocking"]


async def test_a_third_differently_structured_file_imports(scoped: TestClient) -> None:
    """The milestone's exit criterion, run end to end as a reviewer would.

    Three files of unrelated shapes — a clean IPS export, a receptionist's
    hand-kept workbook, and a Spanish Excel CSV with cp1252 and semicolons —
    each reach a committed import. The middle one takes two corrections, which
    is what "minor mapping confirmation" means.
    """
    committed = {}
    for name in ("1_clean_ips.xlsx", "3_excel_csv_es.csv"):
        body = _upload(scoped, name)
        scoped.post(f"/onboarding/uploads/{body['session_id']}/validate")
        response = scoped.post(f"/onboarding/uploads/{body['session_id']}/commit")
        assert response.status_code == 200, response.text
        committed[name] = sum(response.json()["committed"].values())

    body = _upload(scoped, "2_receptionist.xlsx")
    session_id = body["session_id"]
    scoped.put(
        f"/onboarding/uploads/{session_id}/mapping",
        json={"sheet": "AGOSTO (viejo)", "mapping": {}, "entity": "skip"},
    )
    scoped.put(
        f"/onboarding/uploads/{session_id}/mapping",
        json={
            "sheet": "AGENDA",
            "mapping": {},
            "entity": "patient",
            "excluded_rows": [17, 18, 19, 20],
        },
    )
    scoped.post(f"/onboarding/uploads/{session_id}/validate")
    response = scoped.post(f"/onboarding/uploads/{session_id}/commit")
    assert response.status_code == 200, response.text
    committed["2_receptionist.xlsx"] = response.json()["committed"]["AGENDA"]

    assert all(count > 0 for count in committed.values()), committed


async def test_the_messy_workbook_is_fixed_entirely_from_the_screen(
    scoped: TestClient,
) -> None:
    """The same two corrections, made the way a receptionist would make them.

    The API having a control is not the same as a person being able to reach it,
    and this screen is the milestone's "mapping confirmation" deliverable. The
    row numbers are typed the way a person types them, commas and all.
    """
    body = _upload(scoped, "2_receptionist.xlsx")
    session_id = body["session_id"]

    scoped.post(
        f"/onboarding/uploads/{session_id}/review/AGOSTO (viejo)",
        data={"entity": "skip"},
        follow_redirects=False,
    )
    scoped.post(
        f"/onboarding/uploads/{session_id}/review/AGENDA",
        data={"entity": "patient", "exclude": "17, 18, 19 y 20"},
        follow_redirects=False,
    )
    scoped.post(f"/onboarding/uploads/{session_id}/review/validate", follow_redirects=False)

    page = scoped.get(f"/onboarding/uploads/{session_id}/review").text
    assert "cannot be committed yet" not in page

    scoped.post(f"/onboarding/uploads/{session_id}/review/commit", follow_redirects=False)
    final = scoped.get(f"/onboarding/uploads/{session_id}").json()
    assert final["status"] == "committed"


async def test_a_reviewer_can_answer_a_row_the_file_cannot_decide(
    scoped: TestClient,
) -> None:
    """The last step of the workflow: a refusal a person can actually resolve.

    Three of AGENDA's rows are correctly refused — two unassigned phone
    prefixes and a three-word name that splits two ways. Refusing is right, but
    without this endpoint those rows could never import, and "correct refusal"
    would just mean "permanently stuck".
    """
    body = _upload(scoped, "2_receptionist.xlsx")
    session_id = body["session_id"]
    scoped.put(
        f"/onboarding/uploads/{session_id}/mapping",
        json={"sheet": "AGOSTO (viejo)", "mapping": {}, "entity": "skip"},
    )
    scoped.put(
        f"/onboarding/uploads/{session_id}/mapping",
        json={
            "sheet": "AGENDA",
            "mapping": {},
            "entity": "patient",
            "excluded_rows": [17, 18, 19, 20],
        },
    )
    scoped.post(f"/onboarding/uploads/{session_id}/validate")

    review = scoped.get(
        f"/onboarding/uploads/{session_id}/rows", params={"status": "review"}
    ).json()
    assert len(review) == 3

    for row in review:
        cells = {}
        for reason in row["reviews"]:
            if reason.startswith("CELULAR"):
                cells["CELULAR"] = "3101234567"  # a reachable prefix
            elif reason.startswith("NOMBRE COMPLETO"):
                cells["NOMBRE COMPLETO"] = "Zzyzx Sentinelensen Marcadorez Prueba"
        response = scoped.post(
            f"/onboarding/uploads/{session_id}/rows/correct",
            json={"sheet": "AGENDA", "row_number": row["row_number"], "cells": cells},
        )
        assert response.status_code == 200, response.text

    still_open = scoped.get(
        f"/onboarding/uploads/{session_id}/rows", params={"status": "review"}
    ).json()
    assert still_open == []

    committed = scoped.post(f"/onboarding/uploads/{session_id}/commit")
    assert committed.status_code == 200, committed.text
    assert committed.json()["committed"]["AGENDA"] == 11
    assert committed.json()["skipped"]["AGENDA"] == 0


async def test_a_correction_is_converted_by_the_same_rules(scoped: TestClient) -> None:
    """A reviewer supplies text, not a verdict.

    If a correction were trusted as given, this endpoint would be a hole
    straight through the normalizers: a person could answer a refused phone
    with anything at all. The corrected value is converted like every other
    cell, so an unreachable number is refused a second time.
    """
    body = _upload(scoped, "2_receptionist.xlsx")
    session_id = body["session_id"]
    scoped.put(
        f"/onboarding/uploads/{session_id}/mapping",
        json={"sheet": "AGOSTO (viejo)", "mapping": {}, "entity": "skip"},
    )
    scoped.put(
        f"/onboarding/uploads/{session_id}/mapping",
        json={
            "sheet": "AGENDA",
            "mapping": {},
            "entity": "patient",
            "excluded_rows": [17, 18, 19, 20],
        },
    )
    scoped.post(f"/onboarding/uploads/{session_id}/validate")

    phone_row = next(
        r
        for r in scoped.get(
            f"/onboarding/uploads/{session_id}/rows", params={"status": "review"}
        ).json()
        if any(reason.startswith("CELULAR") for reason in r["reviews"])
    )
    scoped.post(
        f"/onboarding/uploads/{session_id}/rows/correct",
        json={
            "sheet": "AGENDA",
            "row_number": phone_row["row_number"],
            "cells": {"CELULAR": "3067891235"},  # still an unassigned prefix
        },
    )

    after = scoped.get(f"/onboarding/uploads/{session_id}/rows", params={"status": "review"}).json()
    assert any(r["row_number"] == phone_row["row_number"] for r in after)


async def test_a_correction_to_a_row_that_does_not_exist_is_refused(
    scoped: TestClient,
) -> None:
    body = _upload(scoped, "1_clean_ips.xlsx")
    response = scoped.post(
        f"/onboarding/uploads/{body['session_id']}/rows/correct",
        json={"sheet": "Pacientes", "row_number": 9999, "cells": {"Celular": "3101234567"}},
    )
    assert response.status_code == 422


async def test_a_refused_row_can_be_answered_from_the_screen(scoped: TestClient) -> None:
    """The correction step has to exist where a receptionist actually works.

    The screen reported a count of rows to review without ever showing them, so
    the only way to resolve one was the API. This covers the rendered form and
    its handler together.
    """
    body = _upload(scoped, "2_receptionist.xlsx")
    session_id = body["session_id"]
    scoped.post(
        f"/onboarding/uploads/{session_id}/review/AGOSTO (viejo)",
        data={"entity": "skip"},
        follow_redirects=False,
    )
    scoped.post(
        f"/onboarding/uploads/{session_id}/review/AGENDA",
        data={"entity": "patient", "exclude": "17, 18, 19 y 20"},
        follow_redirects=False,
    )
    scoped.post(f"/onboarding/uploads/{session_id}/review/validate", follow_redirects=False)

    page = scoped.get(f"/onboarding/uploads/{session_id}/review").text
    assert "Rows needing an answer" in page
    assert "cannot be reached" in page

    rows = scoped.get(f"/onboarding/uploads/{session_id}/rows", params={"status": "review"}).json()
    for row in rows:
        data = {"sheet": "AGENDA"}
        for reason in row["reviews"]:
            if reason.startswith("CELULAR"):
                data["cell::CELULAR"] = "3101234567"
            elif reason.startswith("NOMBRE COMPLETO"):
                data["cell::NOMBRE COMPLETO"] = "Zzyzx Sentinelensen Marcadorez Prueba"
        scoped.post(
            f"/onboarding/uploads/{session_id}/review/rows/{row['row_number']}",
            data=data,
            follow_redirects=False,
        )

    page = scoped.get(f"/onboarding/uploads/{session_id}/review").text
    assert "Rows needing an answer" not in page

    scoped.post(f"/onboarding/uploads/{session_id}/review/commit", follow_redirects=False)
    final = scoped.get(f"/onboarding/uploads/{session_id}").json()
    assert final["status"] == "committed"
