"""Enforce clinic isolation with row-level security.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-18

Every table holding clinic data gets a policy comparing its `clinic_id` with the
`app.clinic_id` setting of the current transaction (see src/core/tenancy.py).
The policies apply to the runtime role. The owner account, which runs
migrations, is the table owner and is therefore not subject to them.

Default deny: with no setting, `app.current_clinic()` returns NULL, every
comparison is NULL, and no row is visible or writable.

`clinics` is the exception: any clinic row may be read, so that an operator can
discover which clinics exist and pick one, and a new clinic may be inserted.
Changing or deleting a clinic still requires that clinic to be in scope.

`audit.access_log` deliberately has no policy. It records actions across
clinics, including system actions with no clinic at all, and reading it is an
administrative action rather than clinical data access.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from alembic import op

from src.core.config import get_settings

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

# Every table with a clinic_id column.
SCOPED_TABLES = (
    "patients",
    "phone_bindings",
    "consents",
    "doctors",
    "locations",
    "appointment_types",
    "availability_rules",
    "availability_exceptions",
    "appointments",
)


def _runtime_role() -> str:
    role = get_settings().app_db_user
    if not _IDENTIFIER.match(role):
        raise ValueError(f"APP_DB_USER is not a plain SQL identifier: {role!r}")
    return role


def upgrade() -> None:
    role = _runtime_role()

    # STABLE so the planner can call it once per query rather than per row.
    op.execute(
        """
        CREATE FUNCTION app.current_clinic() RETURNS uuid
        LANGUAGE sql STABLE AS $$
            SELECT nullif(current_setting('app.clinic_id', true), '')::uuid
        $$
        """
    )

    for table in SCOPED_TABLES:
        op.execute(f"ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY clinic_isolation ON app.{table}
            FOR ALL TO "{role}"
            USING (clinic_id = app.current_clinic())
            WITH CHECK (clinic_id = app.current_clinic())
            """
        )

    # Clinics: readable so an operator can choose one; insertable so a clinic can
    # be created before any scope exists. Updates and deletes need it in scope.
    op.execute("ALTER TABLE app.clinics ENABLE ROW LEVEL SECURITY")
    op.execute(f'CREATE POLICY clinic_readable ON app.clinics FOR SELECT TO "{role}" USING (true)')
    op.execute(
        f'CREATE POLICY clinic_insertable ON app.clinics FOR INSERT TO "{role}" WITH CHECK (true)'
    )
    op.execute(
        f"""
        CREATE POLICY clinic_updatable ON app.clinics
        FOR UPDATE TO "{role}"
        USING (id = app.current_clinic())
        WITH CHECK (id = app.current_clinic())
        """
    )
    op.execute(
        f'CREATE POLICY clinic_deletable ON app.clinics FOR DELETE TO "{role}" '
        "USING (id = app.current_clinic())"
    )


def downgrade() -> None:
    for table in SCOPED_TABLES:
        op.execute(f"DROP POLICY IF EXISTS clinic_isolation ON app.{table}")
        op.execute(f"ALTER TABLE app.{table} DISABLE ROW LEVEL SECURITY")
    for policy in ("clinic_readable", "clinic_insertable", "clinic_updatable", "clinic_deletable"):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON app.clinics")
    op.execute("ALTER TABLE app.clinics DISABLE ROW LEVEL SECURITY")
    op.execute("DROP FUNCTION IF EXISTS app.current_clinic()")
