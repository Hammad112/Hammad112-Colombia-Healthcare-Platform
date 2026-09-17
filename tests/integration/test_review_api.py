"""Review API against a real database.

`test_every_patient_data_route_records_an_audit_entry` reads the route list from
the OpenAPI document. A GET route added later under one of the
`PATIENT_DATA_ROUTES` prefixes without auditing fails there.
Patient-data routes under a new prefix must be added to that pattern.
"""

from __future__ import annotations

import re
import uuid
from datetime import time

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.audit.models import AccessLogEntry
from src.core.config import Settings
from src.registry.models import Patient
from src.scheduling.models import AppointmentStatus, AvailabilityRule
from tests.integration.factories import (
    DOCUMENT,
    PHONE,
    Graph,
    add_patient_records,
    appointment,
    at,
    create_graph,
)

PATIENT_DATA_ROUTES = re.compile(r"^/review/(patients|appointments|consents|phone-bindings)")


async def _prepare(session: AsyncSession) -> tuple[Graph, uuid.UUID]:
    graph = await create_graph(session)
    appointment_id = await add_patient_records(session, graph)
    return graph, appointment_id


async def _audit_count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(AccessLogEntry)) or 0)


def _records_returned(body: object) -> int:
    if isinstance(body, list):
        return len(body)
    if isinstance(body, dict):
        return int(body["total"]) if "total" in body else 1
    raise AssertionError(f"unexpected response body: {body!r}")


async def test_every_patient_data_route_records_an_audit_entry(
    session: AsyncSession, client: TestClient
) -> None:
    graph, appointment_id = await _prepare(session)
    paths = [
        path
        for path, operations in client.get("/openapi.json").json()["paths"].items()
        if "get" in operations and PATIENT_DATA_ROUTES.match(path)
    ]
    assert len(paths) >= 6, paths

    for template in paths:
        path = template.format(patient_id=graph.patient_id, appointment_id=appointment_id)
        before = await _audit_count(session)
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)
        assert _records_returned(response.json()) > 0, (
            f"{path} returned no records, so this check would prove nothing"
        )
        assert await _audit_count(session) > before, f"{path} returned patient data unaudited"


async def test_patient_detail_masks_identifiers_and_includes_related_records(
    session: AsyncSession, client: TestClient
) -> None:
    graph, _ = await _prepare(session)
    body = client.get(f"/review/patients/{graph.patient_id}").json()

    assert body["given_names"] == "Ana"
    assert body["document_number_masked"] == "******" + DOCUMENT[-4:]
    assert body["phone_masked"] == "*********" + PHONE[-4:]
    assert body["email_masked"] == "a***@example.com"
    assert [c["active"] for c in body["consents"]] == [True]
    assert [b["relationship_kind"] for b in body["phone_bindings"]] == ["self"]
    assert body["appointments"][0]["start"] == "2026-10-05T09:00:00-05:00"


async def test_identifiers_are_stored_as_ciphertext(
    session: AsyncSession, settings: Settings
) -> None:
    graph = await create_graph(session)
    await session.commit()
    with psycopg.connect(settings.conninfo(admin=True)) as conn:
        row = conn.execute(
            "SELECT document_number, given_names, family_names, phone_e164, email"
            " FROM app.patients WHERE id = %s",
            (graph.patient_id,),
        ).fetchone()
    assert row is not None
    for stored, plaintext in zip(
        row, (DOCUMENT, "Ana", "Gómez Ruiz", PHONE, "ana.gomez@example.com"), strict=True
    ):
        assert isinstance(stored, bytes)
        assert stored[0] == 1  # key version
        assert plaintext.encode("utf-8") not in stored


async def test_blind_index_lookup(session: AsyncSession, client: TestClient) -> None:
    await _prepare(session)
    assert client.get("/review/patients", params={"phone": PHONE}).json()["total"] == 1
    assert client.get("/review/patients", params={"phone": "+573009999999"}).json()["total"] == 0
    assert client.get("/review/patients", params={"document_number": DOCUMENT}).json()["total"] == 1


async def test_soft_deleted_patient_disappears_from_every_patient_query(
    session: AsyncSession, client: TestClient
) -> None:
    graph, appointment_id = await _prepare(session)
    patient = await session.get(Patient, graph.patient_id)
    assert patient is not None
    patient.deleted_at = at(1, 0)
    await session.commit()

    assert client.get(f"/review/patients/{graph.patient_id}").status_code == 404
    assert client.get("/review/patients", params={"phone": PHONE}).json()["total"] == 0
    assert client.get(f"/review/appointments/{appointment_id}").status_code == 404
    assert client.get("/review/appointments").json()["total"] == 0
    assert client.get("/review/consents").json()["total"] == 0
    # The remaining patient on the shared handset no longer shares it with anyone.
    assert client.get("/review/phone-bindings/shared").json() == []


async def test_shared_handset_lists_both_patients_without_the_number(
    session: AsyncSession, client: TestClient
) -> None:
    await _prepare(session)
    [handset] = client.get("/review/phone-bindings/shared").json()
    assert handset["patient_count"] == 2
    assert {m["relationship_kind"] for m in handset["members"]} == {"self", "guardian"}
    assert PHONE not in str(handset)


async def test_appointment_filters(session: AsyncSession, client: TestClient) -> None:
    graph, _ = await _prepare(session)
    session.add(appointment(graph, at(6, 9), status=AppointmentStatus.CANCELLED))
    await session.commit()

    def total(**params: str) -> int:
        return int(client.get("/review/appointments", params=params).json()["total"])

    assert total() == 2
    assert total(status="cancelled") == 1
    assert total(date_from="2026-10-06") == 1
    assert total(date_to="2026-10-05") == 1
    assert total(doctor_id=str(graph.doctor_id)) == 2
    assert client.get("/review/appointments", params={"status": "bogus"}).status_code == 422


async def test_doctor_detail_lists_availability(session: AsyncSession, client: TestClient) -> None:
    graph = await create_graph(session)
    session.add(
        AvailabilityRule(
            clinic_id=graph.clinic_id,
            doctor_id=graph.doctor_id,
            location_id=graph.location_id,
            weekday=2,
            start_time=time(8, 0),
            end_time=time(12, 0),
            valid_from=at(1, 0).date(),
        )
    )
    await session.commit()
    body = client.get(f"/review/doctors/{graph.doctor_id}").json()
    assert [(r["weekday_name"], r["start_time"]) for r in body["availability_rules"]] == [
        ("miércoles", "08:00:00")
    ]


@pytest.mark.parametrize(
    "path", ["/review/patients/{id}", "/review/appointments/{id}", "/review/doctors/{id}"]
)
def test_unknown_ids_return_404(client: TestClient, path: str) -> None:
    assert client.get(path.format(id=uuid.uuid4())).status_code == 404


def test_page_size_is_bounded(client: TestClient) -> None:
    assert client.get("/review/patients", params={"limit": 201}).status_code == 422
    assert client.get("/review/patients", params={"offset": -1}).status_code == 422


async def test_summary_and_audit_chain(session: AsyncSession, client: TestClient) -> None:
    await _prepare(session)
    summary = client.get("/review/summary").json()
    assert summary["row_counts"]["patients"] == 2
    assert summary["appointments_by_status"] == {"scheduled": 1}

    client.get("/review/patients")
    log = client.get("/review/audit-log").json()
    assert log["total"] >= 2
    assert client.get("/review/audit-log/verify").json()["intact"] is True
