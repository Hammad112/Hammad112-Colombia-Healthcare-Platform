"""The onboarding API, driven the way a reviewer drives it from Swagger UI.

The guarantee these tests exist for is the milestone's exit criterion: **no
import commits data that fails validation**. Everything else here supports it —
that a file is read, that its columns are proposed, that a person can correct
them — but the tests that must never be weakened are the ones asserting a
refusal.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

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
    """A client already scoped to a real clinic, as every route requires."""
    graph = await create_graph(session)
    await session.commit()
    yield scope_client(client, graph)


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
