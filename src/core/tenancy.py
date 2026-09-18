"""Clinic scoping for every request, job and script that touches clinic data.

Isolation between clinics is enforced by PostgreSQL row-level security, not by
remembering to add a `WHERE clinic_id = ...` clause. Policies on every clinic
table compare `clinic_id` against the `app.clinic_id` setting, which
`apply_clinic_scope` sets for the current transaction.

The default is deny: when the setting is absent the comparison is NULL, no row
matches, and reads return nothing while writes are rejected. Forgetting to set a
scope therefore produces an obvious empty result instead of another clinic's
data.

The setting is transaction-local, so it cannot leak to the next request through
a pooled connection. Repositories still filter on `clinic_id` as well: the
filter keeps queries efficient and intent explicit, and the policy is what makes
it safe.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

CLINIC_SETTING = "app.clinic_id"


@dataclass(frozen=True, slots=True)
class ClinicScope:
    """The one clinic whose data the current transaction may read or write."""

    clinic_id: uuid.UUID


async def apply_clinic_scope(session: AsyncSession, scope: ClinicScope) -> None:
    """Bind the scope to the current transaction.

    Must run inside the transaction that performs the queries. A commit ends the
    transaction and clears the setting, so a caller that commits and continues
    must apply the scope again.
    """
    await session.execute(select(func.set_config(CLINIC_SETTING, str(scope.clinic_id), True)))


async def current_clinic_scope(session: AsyncSession) -> uuid.UUID | None:
    """The clinic bound to this transaction, or None if no scope is set."""
    value = await session.scalar(select(func.current_setting(CLINIC_SETTING, True)))
    return uuid.UUID(value) if value else None
