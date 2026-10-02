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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.tenancy import ClinicScope, apply_clinic_scope
from src.identity.models import Consent, ConsentChannel, ConsentPurpose, EvidenceKind
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
        # Refused by the archive guard, which names the real problem: the
        # declared uncompressed size. It used to be refused by the global body
        # limit instead -- a 413 "Payload too large" that was an accident of the
        # limit being 1 MB, and which said nothing about why the file is hostile.
        ("5_zip_bomb.xlsx", 422),
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


async def test_a_correction_never_overwrites_what_the_file_held(
    scoped: TestClient,
) -> None:
    """The transform log must keep saying what the clinic actually exported.

    In-flight editing is not the audit problem; editing *silently* is. HL7
    Provenance expects a changed value to name who changed it, and DAMA-DMBOK
    treats downstream correction as acceptable only when the original stays
    recoverable. An earlier version of the correction path replaced the cell
    before conversion, so the log claimed the file had contained the reviewer's
    value and the exported one was gone.
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

    row = next(
        r
        for r in scoped.get(
            f"/onboarding/uploads/{session_id}/rows", params={"status": "review"}
        ).json()
        if any(reason.startswith("CELULAR") for reason in r["reviews"])
    )
    exported = next(
        entry
        for entry in scoped.get(f"/onboarding/uploads/{session_id}/transform-log").json()
        if entry["row_number"] == row["row_number"] and entry["column"] == "CELULAR"
    )["raw"]

    scoped.post(
        f"/onboarding/uploads/{session_id}/rows/correct",
        json={
            "sheet": "AGENDA",
            "row_number": row["row_number"],
            "cells": {"CELULAR": "3101234567"},
        },
    )

    entry = next(
        e
        for e in scoped.get(f"/onboarding/uploads/{session_id}/transform-log").json()
        if e["row_number"] == row["row_number"] and e["column"] == "CELULAR"
    )
    # What the file held survives the correction, ...
    assert entry["raw"] == exported
    # ... the reviewer's answer is recorded as theirs, ...
    assert entry["corrected_from_review"] == "3101234567"
    # ... and the value that will be written came from the normalizer.
    assert entry["normalized"] == "+573101234567"
    assert entry["status"] == "valid"


async def test_an_untouched_cell_is_not_marked_as_corrected(scoped: TestClient) -> None:
    """Only an answered cell carries an answer, or the field means nothing."""
    body = _upload(scoped, "1_clean_ips.xlsx")
    scoped.post(f"/onboarding/uploads/{body['session_id']}/validate")
    entries = scoped.get(f"/onboarding/uploads/{body['session_id']}/transform-log").json()
    assert entries
    assert all(e["corrected_from_review"] is None for e in entries)


# ------------------------------------- the file's shape, confirmed by a person
def _upload_bytes(client: TestClient, name: str, raw: bytes) -> dict:  # type: ignore[type-arg]
    response = client.post("/onboarding/uploads", files={"file": (name, raw)})
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


PREAMBLE_CSV = (
    b"LISTADO DE PACIENTES CLINICA X\n\n"
    b"TIPO DOC;IDENTIFICACION;NOMBRES;APELLIDOS;CELULAR\n"
    b"CC;1020304050;Ana;Perez Gomez;3101234567\n"
    b"CC;1020304051;Luis;Gomez Diaz;3109876543\n"
)


async def test_a_file_shape_question_blocks_the_import_until_answered(
    scoped: TestClient,
) -> None:
    """An unanswered shape question decides which rows exist, so it must block.

    Reading the clinic name as the only heading yields a one-column table and no
    patients. Committing that would import nothing and report success, which is
    the failure mode this whole milestone exists to prevent.
    """
    body = _upload_bytes(scoped, "preamble.csv", PREAMBLE_CSV)
    session_id = body["session_id"]

    assert [q["id"] for q in body["structure_questions"]] == ["csv.header_row"]
    question = body["structure_questions"][0]
    assert question["answered"] is None
    assert "LISTADO DE PACIENTES" in question["finding"]
    assert question["if_approved"] and question["if_declined"]

    validation = scoped.post(f"/onboarding/uploads/{session_id}/validate").json()
    assert validation["can_commit"] is False
    assert any("csv.header_row" in reason for reason in validation["blocking"])

    refused = scoped.post(f"/onboarding/uploads/{session_id}/commit")
    assert refused.status_code == 409


async def test_approving_a_shape_question_rereads_the_file(scoped: TestClient) -> None:
    """The answer changes how the bytes are read, so the file is parsed again."""
    body = _upload_bytes(scoped, "preamble.csv", PREAMBLE_CSV)
    session_id = body["session_id"]
    assert body["sheets"][0]["columns"][0]["column"] == "LISTADO DE PACIENTES CLINICA X"

    answered = scoped.post(
        f"/onboarding/uploads/{session_id}/structure",
        json={"id": "csv.header_row", "approved": True},
    )
    assert answered.status_code == 200, answered.text

    columns = {c["column"] for c in answered.json()["sheets"][0]["columns"]}
    assert {"TIPO DOC", "IDENTIFICACION", "NOMBRES", "APELLIDOS", "CELULAR"} == columns
    # The question stays listed, now marked answered: the file still has a
    # preamble, and a reviewer needs to see the decision they made rather than
    # have it disappear. What matters is that nothing is left unanswered.
    questions = answered.json()["structure_questions"]
    assert [q["id"] for q in questions] == ["csv.header_row"]
    assert all(q["answered"] is not None for q in questions)

    scoped.post(f"/onboarding/uploads/{session_id}/validate")
    committed = scoped.post(f"/onboarding/uploads/{session_id}/commit")
    assert committed.status_code == 200, committed.text
    assert sum(committed.json()["committed"].values()) == 2


async def test_declining_a_shape_question_keeps_the_file_as_written(
    scoped: TestClient,
) -> None:
    """Declining discards that one finding rather than blocking forever.

    The import then proceeds on the file exactly as written, and what blocks it
    is the ordinary missing-field refusal rather than the shape question.
    """
    body = _upload_bytes(scoped, "preamble.csv", PREAMBLE_CSV)
    session_id = body["session_id"]

    declined = scoped.post(
        f"/onboarding/uploads/{session_id}/structure",
        json={"id": "csv.header_row", "approved": False},
    )
    assert declined.status_code == 200
    assert declined.json()["structure_questions"][0]["answered"] is False

    validation = scoped.post(f"/onboarding/uploads/{session_id}/validate").json()
    assert not any("csv.header_row" in reason for reason in validation["blocking"])


async def test_declining_one_issue_leaves_the_rest_of_the_file_importable(
    scoped: TestClient,
) -> None:
    """A declined issue is discarded, not the whole import.

    One row has an unquoted delimiter. Declining leaves that row out and imports
    every other patient, rather than refusing the file or silently cutting a
    cell off the bad row.
    """
    raw = (
        b"TIPO DOC;IDENTIFICACION;NOMBRES;APELLIDOS;CELULAR\n"
        b"CC;1020304050;Ana;Perez Gomez;3101234567\n"
        b"CC;1020304051;Luis;Gomez;Diaz;3109876543\n"
        b"CC;1020304052;Sofia;Ruiz Mora;3151234567\n"
    )
    body = _upload_bytes(scoped, "ragged.csv", raw)
    session_id = body["session_id"]
    assert [q["id"] for q in body["structure_questions"]] == ["csv.overlong_rows"]

    scoped.post(
        f"/onboarding/uploads/{session_id}/structure",
        json={"id": "csv.overlong_rows", "approved": False},
    )
    scoped.post(f"/onboarding/uploads/{session_id}/validate")
    committed = scoped.post(f"/onboarding/uploads/{session_id}/commit")
    assert committed.status_code == 200, committed.text
    # Two of the three patients import; the ambiguous row was left out, not cut.
    assert sum(committed.json()["committed"].values()) == 2


async def test_approving_the_ragged_row_imports_it_too(scoped: TestClient) -> None:
    raw = (
        b"TIPO DOC;IDENTIFICACION;NOMBRES;APELLIDOS;CELULAR\n"
        b"CC;1020304050;Ana;Perez Gomez;3101234567\n"
        b"CC;1020304051;Luis;Gomez Diaz;3109876543;extra\n"
    )
    body = _upload_bytes(scoped, "ragged2.csv", raw)
    session_id = body["session_id"]

    scoped.post(
        f"/onboarding/uploads/{session_id}/structure",
        json={"id": "csv.overlong_rows", "approved": True},
    )
    scoped.post(f"/onboarding/uploads/{session_id}/validate")
    committed = scoped.post(f"/onboarding/uploads/{session_id}/commit")
    assert committed.status_code == 200, committed.text
    assert sum(committed.json()["committed"].values()) == 2


async def test_a_question_that_is_not_about_this_file_is_refused(
    scoped: TestClient,
) -> None:
    body = _upload_bytes(scoped, "preamble.csv", PREAMBLE_CSV)
    response = scoped.post(
        f"/onboarding/uploads/{body['session_id']}/structure",
        json={"id": "csv.invented_question", "approved": True},
    )
    assert response.status_code == 422


async def test_the_screen_can_answer_a_shape_question(scoped: TestClient) -> None:
    """The reviewer's own path, not just the API."""
    body = _upload_bytes(scoped, "preamble.csv", PREAMBLE_CSV)
    session_id = body["session_id"]

    page = scoped.get(f"/onboarding/uploads/{session_id}/review").text
    assert "How this file is read" in page
    assert "LISTADO DE PACIENTES" in page

    scoped.post(
        f"/onboarding/uploads/{session_id}/review/structure",
        data={"id": "csv.header_row", "approved": "yes"},
        follow_redirects=False,
    )
    page = scoped.get(f"/onboarding/uploads/{session_id}/review").text
    assert "Answered: applied" in page

    scoped.post(f"/onboarding/uploads/{session_id}/review/validate", follow_redirects=False)
    scoped.post(f"/onboarding/uploads/{session_id}/review/commit", follow_redirects=False)
    assert scoped.get(f"/onboarding/uploads/{session_id}").json()["status"] == "committed"


async def test_utf16_csv_imports_rather_than_being_read_as_garbage(
    scoped: TestClient,
) -> None:
    """Excel's "Unicode Text" export used to arrive as NUL-riddled mojibake."""
    text = (
        "TIPO DOC;IDENTIFICACION;NOMBRES;APELLIDOS;CELULAR\n"
        "CC;1020304050;José;Muñoz Pérez;3101234567\n"
    )
    body = _upload_bytes(scoped, "unicode.csv", text.encode("utf-16"))
    assert body["encoding"] == "utf-16"
    assert {c["column"] for c in body["sheets"][0]["columns"]} == {
        "TIPO DOC",
        "IDENTIFICACION",
        "NOMBRES",
        "APELLIDOS",
        "CELULAR",
    }

    scoped.post(f"/onboarding/uploads/{body['session_id']}/validate")
    committed = scoped.post(f"/onboarding/uploads/{body['session_id']}/commit")
    assert committed.status_code == 200, committed.text
    assert sum(committed.json()["committed"].values()) == 1


# ------------- exit criterion: nothing commits that fails validation
async def test_a_duplicated_patient_blocks_the_commit(scoped: TestClient) -> None:
    """Two rows with one cedula would write the same patient twice.

    `_apply_patients` matches on the document pair, so the second row updates the
    first rather than creating anyone: the import would report two patients
    written and the clinic would have one. That is a silently wrong import, so
    it blocks until a person decides which row is right.
    """
    raw = (
        b"TIPO DOC;IDENTIFICACION;NOMBRES;APELLIDOS;CELULAR\n"
        b"CC;1020304050;Ana;Perez Gomez;3101234567\n"
        b"CC;1020304051;Luis;Gomez Diaz;3109876543\n"
        b"CC;1020304050;Ana Maria;Perez Gomez;3151234567\n"
    )
    body = _upload_bytes(scoped, "dupes.csv", raw)
    session_id = body["session_id"]

    validation = scoped.post(f"/onboarding/uploads/{session_id}/validate").json()
    assert validation["can_commit"] is False
    assert any("row 4" in reason and "row 2" in reason for reason in validation["blocking"]), (
        validation["blocking"]
    )

    refused = scoped.post(f"/onboarding/uploads/{session_id}/commit")
    assert refused.status_code == 409


async def test_the_same_number_under_two_document_types_still_imports(
    scoped: TestClient,
) -> None:
    """The duplicate rule must not refuse two genuinely different people.

    A cedula and a tarjeta de identidad may share digits. Blocking this would
    make the guard worse than not having it.
    """
    raw = (
        b"TIPO DOC;IDENTIFICACION;NOMBRES;APELLIDOS;CELULAR\n"
        b"CC;1020304050;Ana;Perez Gomez;3101234567\n"
        b"TI;1020304050;Luis;Gomez Diaz;3109876543\n"
    )
    body = _upload_bytes(scoped, "two_types.csv", raw)
    session_id = body["session_id"]

    validation = scoped.post(f"/onboarding/uploads/{session_id}/validate").json()
    assert validation["can_commit"] is True, validation["blocking"]

    committed = scoped.post(f"/onboarding/uploads/{session_id}/commit")
    assert committed.status_code == 200, committed.text
    assert sum(committed.json()["committed"].values()) == 2


async def test_a_reference_to_a_doctor_nobody_defines_blocks_the_commit(
    scoped: TestClient,
) -> None:
    """An appointment naming a doctor that does not exist has nobody attending it.

    The clean fixture defines its own doctors, so its appointments pass. This
    file names a doctor no sheet defines and the clinic does not have.
    """
    body = _upload(scoped, "1_clean_ips.xlsx")
    session_id = body["session_id"]
    citas = next(s for s in body["sheets"] if s["sheet"] == "Citas")
    doctor_column = next(
        (c["column"] for c in citas["columns"] if c["target_field"] == "doctor_ref"), None
    )
    if doctor_column is None:
        pytest.skip("the clean fixture does not map a doctor reference")

    # Its own doctors sheet is left out, so the references have nothing to resolve
    # against and nothing in the clinic satisfies them either.
    scoped.put(
        f"/onboarding/uploads/{session_id}/mapping",
        json={"sheet": "Medicos", "mapping": {}, "entity": "skip"},
    )
    validation = scoped.post(f"/onboarding/uploads/{session_id}/validate").json()

    assert validation["can_commit"] is False
    assert any("not in this file" in reason for reason in validation["blocking"]), validation[
        "blocking"
    ]


async def test_a_file_can_be_uploaded_from_a_page(scoped: TestClient) -> None:
    """The scope asks for an upload UI, not only an API endpoint.

    Every other step had a screen while the upload itself was reachable only
    through Swagger or curl, which is not a UI for clinic staff. The page posts
    to the same ingestion route, so the two cannot drift apart.
    """
    page = scoped.get("/onboarding/").text
    assert 'type="file"' in page
    assert 'enctype="multipart/form-data"' in page

    posted = scoped.post(
        "/onboarding/upload",
        files={"file": ("from_page.csv", PREAMBLE_CSV)},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    # Straight to the mapping screen, carrying the session it just created.
    assert "/review?clinic_id=" in posted.headers["location"]

    session_id = posted.headers["location"].split("/uploads/")[1].split("/review")[0]
    assert scoped.get(f"/onboarding/uploads/{session_id}").status_code == 200

    # And the new import is listed for someone to pick up again.
    assert "from_page.csv" in scoped.get("/onboarding/").text


# --------------------------------------------------- consent at import (ADR-19)
CONSENT_CSV = (
    b"TIPO DOC;IDENTIFICACION;NOMBRES;APELLIDOS;CELULAR;"
    b"CONSENTIMIENTO;FECHA CONSENTIMIENTO;EVIDENCIA CONSENTIMIENTO\n"
    b"CC;1055667788;Marta;Rojas Sanin;3151112233;citas;2026-03-15;escrito\n"
)


async def test_consent_in_the_export_is_recorded_against_the_patient(
    scoped: TestClient, session: AsyncSession
) -> None:
    """Under Ley 1581 health data needs explicit, prior, informed authorization.

    There is no "we provide healthcare" exemption in Colombia, so a patient
    imported without consent is a patient the bot may not message. The
    `app.consents` table existed since M0 and nothing mapped to it, which meant a
    clinic could not bring its consent across.
    """
    body = _upload_bytes(scoped, "consent.csv", CONSENT_CSV)
    session_id = body["session_id"]

    columns = {c["column"]: c["target_field"] for s in body["sheets"] for c in s["columns"]}
    assert columns["CONSENTIMIENTO"] == "consent_purpose"
    assert columns["FECHA CONSENTIMIENTO"] == "consent_granted_at"
    assert columns["EVIDENCIA CONSENTIMIENTO"] == "consent_evidence"

    scoped.post(f"/onboarding/uploads/{session_id}/validate")
    committed = scoped.post(f"/onboarding/uploads/{session_id}/commit")
    assert committed.status_code == 200, committed.text

    await apply_clinic_scope(session, ClinicScope(clinic_id=_clinic_of(scoped)))
    consents = (await session.scalars(select(Consent))).all()
    assert len(consents) == 1
    consent = consents[0]
    assert consent.purpose == ConsentPurpose.APPOINTMENT_MESSAGING
    assert consent.evidence_kind == EvidenceKind.WRITTEN
    assert consent.granted_at.date().isoformat() == "2026-03-15"
    # The channel comes from the verified phone binding, not from a spreadsheet.
    assert consent.channel == ConsentChannel.ANY


async def test_a_partial_consent_column_records_no_consent(
    scoped: TestClient, session: AsyncSession
) -> None:
    """A purpose with no date is a claim that consent exists, not a record of it.

    Storing it would let the dispatch gate in M4 treat an unproven consent as
    proven, which is the one outcome this must never produce. The patient still
    imports; only the consent is withheld.
    """
    raw = (
        b"TIPO DOC;IDENTIFICACION;NOMBRES;APELLIDOS;CONSENTIMIENTO\n"
        b"CC;1066778899;Ana;Perez Gomez;citas\n"
    )
    body = _upload_bytes(scoped, "partial.csv", raw)
    scoped.post(f"/onboarding/uploads/{body['session_id']}/validate")
    committed = scoped.post(f"/onboarding/uploads/{body['session_id']}/commit")
    assert committed.status_code == 200, committed.text
    assert sum(committed.json()["committed"].values()) == 1

    await apply_clinic_scope(session, ClinicScope(clinic_id=_clinic_of(scoped)))
    assert (await session.scalars(select(Consent))).all() == []


async def test_an_unrecognised_consent_purpose_is_never_widened(
    scoped: TestClient,
) -> None:
    """Consent to a reminder is not consent to a wellness check-in.

    Mapping an unknown purpose onto the broadest one would manufacture consent
    the patient never gave, so the row goes to review instead.
    """
    raw = (
        b"TIPO DOC;IDENTIFICACION;NOMBRES;APELLIDOS;"
        b"CONSENTIMIENTO;FECHA CONSENTIMIENTO;EVIDENCIA CONSENTIMIENTO\n"
        b"CC;1077889900;Luis;Gomez Diaz;promociones;2026-03-15;escrito\n"
    )
    body = _upload_bytes(scoped, "odd_purpose.csv", raw)
    scoped.post(f"/onboarding/uploads/{body['session_id']}/validate")

    rows = scoped.get(
        f"/onboarding/uploads/{body['session_id']}/rows", params={"status": "review"}
    ).json()
    assert any("promociones" in reason for row in rows for reason in row["reviews"])


async def test_re_importing_the_same_consent_does_not_duplicate_it(
    scoped: TestClient, session: AsyncSession
) -> None:
    """Clinics re-run imports; a consent is not two consents."""
    for name in ("consent.csv", "consent_again.csv"):
        body = _upload_bytes(scoped, name, CONSENT_CSV)
        scoped.post(f"/onboarding/uploads/{body['session_id']}/validate")
        assert scoped.post(f"/onboarding/uploads/{body['session_id']}/commit").status_code == 200

    await apply_clinic_scope(session, ClinicScope(clinic_id=_clinic_of(scoped)))
    assert len((await session.scalars(select(Consent))).all()) == 1


# ------------------------------------- a government-shaped export (RIPS)
# RIPS archivo US and Resolución 1036 de 2022 split a patient name into four
# fields, and every IPS must emit them to get paid. So the shape that imports
# with no corrections is the shape clinics already produce, which is what makes
# "please export separate name columns" a cheap request rather than a burden.

RIPS_CSV = (
    b"tipoDocumentoIdentificacion;numDocumentoIdentificacion;"
    b"primerNombre;segundoNombre;primerApellido;segundoApellido;"
    b"fechaNacimiento;celular\n"
    b"CC;1033445566;Carlos;Andres;Perez;Gomez;1987-04-12;3101234567\n"
    b"CC;1044556677;Maria;Jose;Rojas;Sanin;1992-11-03;3151112233\n"
)


async def test_a_rips_shaped_export_imports_with_no_corrections(
    scoped: TestClient, session: AsyncSession
) -> None:
    """The decision-relevant measurement for the client's name question.

    Nothing is corrected, nothing goes to review, and the four name columns are
    joined into the two canonical fields in the file's own column order.
    """
    body = _upload_bytes(scoped, "rips.csv", RIPS_CSV)
    session_id = body["session_id"]

    assert body["structure_questions"] == []
    columns = {c["column"]: c["target_field"] for s in body["sheets"] for c in s["columns"]}
    assert columns["primerNombre"] == "given_names"
    assert columns["segundoNombre"] == "given_names"
    assert columns["primerApellido"] == "family_names"
    assert columns["segundoApellido"] == "family_names"

    validation = scoped.post(f"/onboarding/uploads/{session_id}/validate").json()
    assert validation["can_commit"] is True, validation["blocking"]
    assert sum(s["review_rows"] for s in validation["sheets"]) == 0

    committed = scoped.post(f"/onboarding/uploads/{session_id}/commit")
    assert committed.status_code == 200, committed.text
    assert sum(committed.json()["committed"].values()) == 2, committed.json()

    # The names that were actually written, not just the row count. Without the
    # join, the second part overwrites the first and this patient is stored as
    # "Andres Gomez" -- same row count, wrong person.
    from src.registry.models import Patient

    await apply_clinic_scope(session, ClinicScope(clinic_id=_clinic_of(scoped)))
    stored = (await session.scalars(select(Patient))).all()
    names = {(p.given_names, p.family_names) for p in stored}
    assert ("Carlos Andres", "Perez Gomez") in names, names
    assert ("Maria Jose", "Rojas Sanin") in names, names


async def test_the_same_patients_in_one_column_need_a_human(
    scoped: TestClient,
) -> None:
    """The cost of the other export shape, measured against the same people.

    Both names are three tokens with no recognised compound given name, which
    is the shape nothing can split: "Carlos Perez Gomez" could be one given
    name and two surnames, or two given names and one. Ley 2129 de 2021 lets
    parents choose surname order, so no positional rule settles it either.

    A compound given name the dictionary knows ("Juan Carlos") does resolve, so
    it is deliberately not used here: this measures the cost of the shape that
    cannot be resolved, not of every three-token name.
    """
    raw = (
        b"tipoDocumentoIdentificacion;numDocumentoIdentificacion;nombreCompleto\n"
        b"CC;1033445566;Carlos Perez Gomez\n"
        b"CC;1044556677;Luis Rojas Sanin\n"
    )
    body = _upload_bytes(scoped, "one_column.csv", raw)
    session_id = body["session_id"]

    validation = scoped.post(f"/onboarding/uploads/{session_id}/validate").json()
    # Every row needs an answer, and the commit is blocked until they get one.
    assert sum(s["review_rows"] for s in validation["sheets"]) == 2

    rows = scoped.get(f"/onboarding/uploads/{session_id}/rows", params={"status": "review"}).json()
    assert len(rows) == 2
    assert all(any("given name" in reason for reason in row["reviews"]) for row in rows)


async def test_the_start_page_lists_clinics_without_needing_one(
    scoped: TestClient,
) -> None:
    """The one page that cannot require a clinic, because it is where one is picked.

    Every other route needs `clinic_id` in its address, because row-level
    security compares it against the transaction. This page exists so a tester
    does not have to paste a UUID for each file, so it is requested here without
    one even though the fixture has created a clinic to list.
    """
    response = scoped.get("/onboarding/start", params={})
    assert response.status_code == 200
    page = response.text

    # It links onward with the clinic already filled in.
    assert f"/onboarding/?clinic_id={_clinic_of(scoped)}" in page
    assert "import a file" in page


async def test_the_start_page_says_so_when_there_are_no_clinics(
    client: TestClient,
) -> None:
    """An empty database should explain itself rather than render an empty table.

    This is the state a developer hits on a fresh checkout, and "No clinics yet"
    with the command to fix it is more use than a page with nothing on it.
    """
    response = client.get("/onboarding/start")
    assert response.status_code == 200
    assert "No clinics yet" in response.text
    assert "python main.py" in response.text


async def test_a_headerless_file_blocks_then_imports_in_full(
    scoped: TestClient,
) -> None:
    """The whole path for a file with no heading row.

    Before the question existed, the first patient became the column names and
    the import reported success with one row fewer than the file had. That is
    the silent-truncation failure the milestone exists to prevent, so the
    question blocks the commit until someone answers it.
    """
    raw = b"CC;1055660001;Ana Perez;3101234567\nCC;1055660002;Luis Gomez;3151112233\n"
    body = _upload_bytes(scoped, "headerless.csv", raw)
    session_id = body["session_id"]

    assert [q["id"] for q in body["structure_questions"]] == ["csv.no_header_row"]
    assert body["structure_questions"][0]["answered"] is None

    blocked = scoped.post(f"/onboarding/uploads/{session_id}/validate").json()
    assert blocked["can_commit"] is False
    assert any("csv.no_header_row" in reason for reason in blocked["blocking"])
    assert scoped.post(f"/onboarding/uploads/{session_id}/commit").status_code == 409

    answered = scoped.post(
        f"/onboarding/uploads/{session_id}/structure",
        json={"id": "csv.no_header_row", "approved": True},
    )
    assert answered.status_code == 200, answered.text

    sheet = answered.json()["sheets"][0]
    assert [c["column"] for c in sheet["columns"]] == [
        "column 1",
        "column 2",
        "column 3",
        "column 4",
    ]

    # The reviewer maps the positional columns, as they would any others.
    mapped = scoped.put(
        f"/onboarding/uploads/{session_id}/mapping",
        json={
            "sheet": sheet["sheet"],
            "mapping": {
                "column 1": "document_type",
                "column 2": "document_number",
                "column 3": "full_name",
                "column 4": "phone_e164",
            },
        },
    )
    assert mapped.status_code == 200, mapped.text

    validation = scoped.post(f"/onboarding/uploads/{session_id}/validate").json()
    assert validation["can_commit"] is True, validation["blocking"]
    # Both rows of the file are data; neither was eaten as a heading.
    assert sum(s["total_rows"] for s in validation["sheets"]) == 2


async def test_a_headerless_file_declined_keeps_the_first_row_as_headings(
    scoped: TestClient,
) -> None:
    """Declining is the reading the importer would have used unasked.

    It loses the first record, which is exactly why the question exists -- but
    it is the reviewer's decision, taken knowingly, and it never blocks forever.
    """
    raw = b"CC;1055660003;Marta Rojas;3151112244\nCC;1055660004;Jorge Diaz;3101239876\n"
    body = _upload_bytes(scoped, "declined.csv", raw)
    session_id = body["session_id"]

    declined = scoped.post(
        f"/onboarding/uploads/{session_id}/structure",
        json={"id": "csv.no_header_row", "approved": False},
    )
    assert declined.status_code == 200
    assert declined.json()["structure_questions"][0]["answered"] is False

    validation = scoped.post(f"/onboarding/uploads/{session_id}/validate").json()
    assert not any("csv.no_header_row" in reason for reason in validation["blocking"])


async def test_a_utf16_export_with_no_bom_imports(scoped: TestClient) -> None:
    """A programmatic UTF-16 export used to arrive as NUL-riddled mojibake."""
    text = (
        "TIPO DOC;IDENTIFICACION;NOMBRES;APELLIDOS;CELULAR\n"
        "CC;1055660005;José;Muñoz Pérez;3101234567\n"
    )
    body = _upload_bytes(scoped, "le.csv", text.encode("utf-16-le"))
    assert body["encoding"] == "utf-16-le"
    assert {c["column"] for c in body["sheets"][0]["columns"]} == {
        "TIPO DOC",
        "IDENTIFICACION",
        "NOMBRES",
        "APELLIDOS",
        "CELULAR",
    }

    scoped.post(f"/onboarding/uploads/{body['session_id']}/validate")
    committed = scoped.post(f"/onboarding/uploads/{body['session_id']}/commit")
    assert committed.status_code == 200, committed.text
    assert sum(committed.json()["committed"].values()) == 1


async def test_the_uploaded_file_is_deleted_once_its_questions_are_answered(
    scoped: TestClient,
) -> None:
    """A clinic's spreadsheet must not sit on disk longer than it is needed.

    It is kept only while an unanswered question could still change how the
    bytes are read, because answering one re-reads the file. A question stays
    listed after it is answered so the reviewer can see what they decided, and
    an earlier version checked "any questions raised" rather than "any still
    open" -- so the file was never deleted at all.
    """
    from src.api.onboarding.routes import _SOURCE

    preamble = (
        b"LISTADO CLINICA X\n\n"
        b"TIPO DOC;IDENTIFICACION;NOMBRES;APELLIDOS\n"
        b"CC;1088880001;Ana;Perez Gomez\n"
    )
    body = _upload_bytes(scoped, "kept.csv", preamble)
    session_id = uuid.UUID(body["session_id"])

    # An open question, so the bytes are still needed.
    assert body["structure_questions"], "expected a question about this file"
    assert session_id in _SOURCE
    on_disk = _SOURCE[session_id]
    assert on_disk.exists()

    answered = scoped.post(
        f"/onboarding/uploads/{session_id}/structure",
        json={"id": "csv.header_row", "approved": True},
    )
    assert answered.status_code == 200, answered.text
    # The question is still reported, now marked answered...
    assert all(q["answered"] is not None for q in answered.json()["structure_questions"])
    # ...and the file it was about is gone.
    assert session_id not in _SOURCE
    assert not on_disk.exists()


async def test_a_file_with_nothing_to_ask_is_deleted_immediately(
    scoped: TestClient,
) -> None:
    """Nothing to re-read means nothing to keep."""
    from src.api.onboarding.routes import _SOURCE

    body = _upload_bytes(
        scoped,
        "clean.csv",
        b"TIPO DOC;IDENTIFICACION;NOMBRES;APELLIDOS\nCC;1088880002;Luis;Gomez Diaz\n",
    )
    assert body["structure_questions"] == []
    assert uuid.UUID(body["session_id"]) not in _SOURCE


async def test_two_commits_of_one_import_admit_exactly_one(
    scoped: TestClient,
) -> None:
    """Pressing Import twice must not write the patients twice.

    The second attempt sees a committed session and refuses, which is the same
    guard a double-click on the confirmation screen hits.
    """
    body = _upload_bytes(
        scoped,
        "once.csv",
        b"TIPO DOC;IDENTIFICACION;NOMBRES;APELLIDOS\nCC;1088880003;Marta;Rojas Sanin\n",
    )
    session_id = body["session_id"]
    scoped.post(f"/onboarding/uploads/{session_id}/validate")

    first = scoped.post(f"/onboarding/uploads/{session_id}/commit")
    second = scoped.post(f"/onboarding/uploads/{session_id}/commit")

    assert first.status_code == 200, first.text
    assert second.status_code == 409
    assert "already committed" in second.json()["detail"]


# ----------------------------------------- rule 8 over the staging tables
async def test_reading_staged_patient_values_is_audited(
    scoped: TestClient, session: AsyncSession
) -> None:
    """Staged rows hold cédulas, names and phones, so reading them is a disclosure.

    These two endpoints recorded nothing, on the reasoning that a row nobody has
    accepted "is not patient data yet". The endpoint returns an unmasked cédula
    and phone number to whoever asks, so rule 8 applies: it is about the values,
    not about whether a reviewer has blessed them.

    Recorded in the repository rather than the route, so a second route reading
    the same rows cannot forget.
    """
    from sqlalchemy import func

    from src.audit.models import AccessLogEntry

    body = _upload_bytes(
        scoped,
        "audited.csv",
        b"TIPO DOC;IDENTIFICACION;NOMBRES;APELLIDOS;CELULAR\n"
        b"CC;1066660001;Ana;Perez Gomez;3101234567\n",
    )
    session_id = body["session_id"]
    scoped.post(f"/onboarding/uploads/{session_id}/validate")

    async def entries() -> int:
        return int(await session.scalar(select(func.count()).select_from(AccessLogEntry)) or 0)

    before = await entries()
    rows = scoped.get(f"/onboarding/uploads/{session_id}/rows")
    assert rows.status_code == 200
    # The values really are unmasked, which is why this must be logged.
    assert "1066660001" in rows.text
    after_rows = await entries()
    assert after_rows > before, "serving staged rows recorded no audit entry"

    log = scoped.get(f"/onboarding/uploads/{session_id}/transform-log")
    assert log.status_code == 200
    assert await entries() > after_rows, "serving the transform log recorded no entry"
