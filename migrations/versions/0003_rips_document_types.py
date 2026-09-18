"""Align patient document types with the RIPS table.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-18

Resolución 948 de 2026 repealed Resolución 2275 de 2023 and moved the code
tables into MinSalud's Documento Técnico 1. Against that table the previous
list was wrong in two ways: `PPT` is the document's popular name, not its code,
which is `PT`; and `CD`, `SC` and `DE` were missing.

`PT` replaces `PPT` and `PPT` is no longer accepted, so any existing row is
rewritten before the constraint is applied. With every code now two letters the
column narrows from VARCHAR(3) to VARCHAR(2), which matches the field width
RIPS specifies and keeps the schema equal to the model.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW = ("RC", "TI", "CC", "CE", "CD", "PA", "SC", "PE", "DE", "PT", "CN", "AS", "MS")
_OLD = ("CC", "CE", "TI", "RC", "PA", "PE", "PPT", "CN", "AS", "MS")


def _check(values: Sequence[str]) -> str:
    return "document_type IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    op.drop_constraint("document_type_valid", "patients", schema="app", type_="check")
    op.execute("UPDATE app.patients SET document_type = 'PT' WHERE document_type = 'PPT'")
    op.alter_column(
        "patients",
        "document_type",
        schema="app",
        existing_type=sa.String(3),
        type_=sa.String(2),
        existing_nullable=False,
    )
    op.create_check_constraint("document_type_valid", "patients", _check(_NEW), schema="app")


def downgrade() -> None:
    op.drop_constraint("document_type_valid", "patients", schema="app", type_="check")
    op.alter_column(
        "patients",
        "document_type",
        schema="app",
        existing_type=sa.String(2),
        type_=sa.String(3),
        existing_nullable=False,
    )
    # Codes this revision introduced have no equivalent in the older list, so
    # they would violate the constraint being restored.
    op.execute("UPDATE app.patients SET document_type = 'PPT' WHERE document_type = 'PT'")
    op.execute("DELETE FROM app.patients WHERE document_type IN ('CD', 'SC', 'DE')")
    op.create_check_constraint("document_type_valid", "patients", _check(_OLD), schema="app")
