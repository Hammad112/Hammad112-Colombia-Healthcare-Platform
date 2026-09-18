"""Review routes for consents and shared handsets.

Every read is clinic-scoped and audited by the repositories.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from src.api.dependencies import ClinicScopeDep, PageDep, SessionDep
from src.api.review.schemas import ConsentOut, Page, SharedHandsetOut, to_page
from src.identity import repository as identity
from src.identity.models import ConsentPurpose

router = APIRouter()


@router.get("/consents", response_model=Page[ConsentOut])
async def list_consents(
    session: SessionDep,
    scope: ClinicScopeDep,
    page: PageDep,
    patient_id: uuid.UUID | None = None,
    purpose: ConsentPurpose | None = None,
    active_only: bool = False,
) -> Page[ConsentOut]:
    result = await identity.list_consents(
        session,
        clinic_id=scope.clinic_id,
        page=page,
        patient_id=patient_id,
        purpose=purpose,
        active_only=active_only,
    )
    return to_page(result, [ConsentOut.build(consent) for consent in result.items])


@router.get("/phone-bindings/shared", response_model=list[SharedHandsetOut])
async def list_shared_handsets(
    session: SessionDep, scope: ClinicScopeDep
) -> list[SharedHandsetOut]:
    """Phone numbers bound to more than one patient. The numbers themselves are not returned."""
    handsets = await identity.list_shared_handsets(session, clinic_id=scope.clinic_id)
    return [SharedHandsetOut.build(handset) for handset in handsets]
