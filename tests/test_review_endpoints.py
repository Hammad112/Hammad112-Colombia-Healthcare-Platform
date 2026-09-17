"""Review API: gating, masking, and the rule that every patient-data read is audited.

`test_every_patient_data_route_writes_an_audit_row` walks the router itself, so a
new patient-data route added without an audit call fails here automatically.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.main import create_app
from src.api.routers import review
from src.api.routers.review import _mask, _mask_email
from src.core.config import Settings, get_settings
from src.core.crypto import blind_index
from src.models import AccessLog, Consent, Patient, PhoneBinding
from tests.conftest import requires_db
from tests.test_schema_constraints import _appointment, _fixture_graph

BOGOTA = timezone(timedelta(hours=-5))

# Routes that return patient data and therefore must audit.
PATIENT_DATA = re.compile(r"/review/(patients|appointments|consents|phone-bindings)")


# ---------------------------------------------------------------- no database needed


@pytest.mark.parametrize(
    "overrides",
    [
        {"app_env": "staging"},
        {"app_env": "production"},
        {"app_env": "local", "allow_real_patient_data": True},
    ],
)
def test_review_api_does_not_exist_outside_local_synthetic(overrides: dict[str, object]) -> None:
    app = create_app()
    settings = Settings(
        phi_encryption_key="prod-looking-key-abcdefghijklmnop",
        phi_blind_index_key="prod-looking-bidx-abcdefghijklmnop",
        **overrides,  # type: ignore[arg-type]
    )
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as client:
        for path in ("/review/summary", "/review/patients", f"/review/patients/{uuid.uuid4()}"):
            assert client.get(path).status_code == 404, path


def test_masking_keeps_only_the_tail() -> None:
    assert _mask("1020304050") == "******4050"
    assert _mask("+573001112233") == "*********2233"
    assert _mask(None) is None
    assert _mask_email("ana.gomez@example.com") == "a***@example.com"


# ---------------------------------------------------------------- database


async def _seed(session: AsyncSession) -> dict[str, uuid.UUID]:
    ids = await _fixture_graph(session)
    appt = _appointment(ids, datetime(2026, 10, 5, 9, 0, tzinfo=BOGOTA), 20, "scheduled")
    session.add(appt)
    session.add(
        Consent(
            id=uuid.uuid4(),
            clinic_id=ids["clinic"],
            patient_id=ids["patient"],
            purpose="appointment_messaging",
            channel="whatsapp",
            granted_at=datetime.now(tz=BOGOTA),
            evidence_kind="imported_declaration",
            policy_version="v1",
        )
    )
    session.add(
        PhoneBinding(
            id=uuid.uuid4(),
            clinic_id=ids["clinic"],
            phone_e164_bidx=blind_index("+573001112233"),
            patient_id=ids["patient"],
            relationship_kind="self",
        )
    )
    # A second patient on the same handset, so the shared-phone route has data (ADR-18).
    sibling = Patient(
        id=uuid.uuid4(),
        clinic_id=ids["clinic"],
        document_type="TI",
        document_number="1122334455",
        document_number_bidx=blind_index("1122334455"),
        given_names="Tomás",
        family_names="Gómez Ruiz",
    )
    session.add(sibling)
    await session.flush()
    session.add(
        PhoneBinding(
            id=uuid.uuid4(),
            clinic_id=ids["clinic"],
            phone_e164_bidx=blind_index("+573001112233"),
            patient_id=sibling.id,
            relationship_kind="guardian",
        )
    )
    await session.commit()
    return {**ids, "appointment": appt.id}


async def _audit_count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(AccessLog)) or 0)


@requires_db
async def test_every_patient_data_route_writes_an_audit_row(session: AsyncSession) -> None:
    ids = await _seed(session)
    app = create_app()
    # Walk the router that defines the routes: newer FastAPI versions nest included
    # routers inside app.routes rather than flattening them.
    routes = [
        r
        for r in review.router.routes
        if isinstance(r, APIRoute) and "GET" in r.methods and PATIENT_DATA.match(r.path)
    ]
    assert len(routes) >= 5, "patient-data routes not found; did the prefix change?"

    with TestClient(app) as client:
        for route in routes:
            path = route.path.format(patient_id=ids["patient"], appointment_id=ids["appointment"])
            before = await _audit_count(session)
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
            body = response.json()
            returned = body.get("total", 1) if isinstance(body, dict) else len(body)
            assert returned, f"{path} returned no data, so this test would prove nothing"
            after = await _audit_count(session)
            assert after > before, f"{path} returned patient data without writing an audit row"


@requires_db
async def test_patient_detail_masks_identifiers_and_includes_related_records(
    session: AsyncSession,
) -> None:
    ids = await _seed(session)
    with TestClient(create_app()) as client:
        body = client.get(f"/review/patients/{ids['patient']}").json()

    assert body["given_names"] == "Ana"
    assert body["document_number_masked"] == "******4050"
    assert body["phone_masked"].endswith("2233") and "300111" not in body["phone_masked"]
    assert len(body["consents"]) == 1 and body["consents"][0]["active"] is True
    assert len(body["phone_bindings"]) == 1
    assert len(body["appointments"]) == 1
    assert body["appointments"][0]["start"].startswith("2026-10-05T09:00:00-05:00")


@requires_db
async def test_blind_index_lookup_finds_encrypted_patient(session: AsyncSession) -> None:
    await _seed(session)
    with TestClient(create_app()) as client:
        hit = client.get("/review/patients", params={"phone": "+573001112233"}).json()
        miss = client.get("/review/patients", params={"phone": "+573009999999"}).json()
        by_doc = client.get("/review/patients", params={"document_number": "1020304050"}).json()
    assert hit["total"] == 1
    assert miss["total"] == 0
    assert by_doc["total"] == 1


@requires_db
async def test_audit_chain_verifies_after_reads(session: AsyncSession) -> None:
    ids = await _seed(session)
    with TestClient(create_app()) as client:
        client.get("/review/patients")
        client.get(f"/review/appointments/{ids['appointment']}")
        result = client.get("/review/audit-log/verify").json()
    assert result["intact"] is True
    assert result["rows_checked"] >= 2
