"""Add the audit chain anchor table.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-18

An anchor records the newest access-log entry at the moment it is taken. The
hash chain cannot detect removal of its own newest entries, because what remains
verifies cleanly; an anchor can, because the entry it names is then missing or
carries a different hash.

The table is append-only on the same terms as `audit.access_log`: the runtime
role gets SELECT and INSERT only, and the shared trigger function rejects UPDATE
and DELETE from every role. That function's message named `access_log`
literally, so this revision rewrites it to report the table it fired on.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from src.core.config import get_settings

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE_NAMED_MESSAGE = """
    CREATE OR REPLACE FUNCTION audit.reject_mutation() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'audit.% is append-only (attempted %)', TG_TABLE_NAME, TG_OP;
    END;
    $$
"""

_ACCESS_LOG_MESSAGE = """
    CREATE OR REPLACE FUNCTION audit.reject_mutation() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'audit.access_log is append-only (attempted %)', TG_OP;
    END;
    $$
"""


def _runtime_role() -> str:
    role = get_settings().app_db_user
    if not role.isidentifier():
        raise ValueError(f"Unsafe runtime role name: {role!r}")
    return role


def upgrade() -> None:
    op.create_table(
        "chain_anchor",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("anchored_at", sa.DateTime(timezone=True), nullable=False),
        # Null when the log was empty at the time of the anchor.
        sa.Column("entry_id", sa.BigInteger(), nullable=True),
        sa.Column("row_hash", sa.LargeBinary(), nullable=True),
        sa.Column("entry_count", sa.BigInteger(), nullable=False),
        schema="audit",
    )
    op.execute(_TABLE_NAMED_MESSAGE)
    op.execute(
        """
        CREATE TRIGGER chain_anchor_append_only
        BEFORE UPDATE OR DELETE ON audit.chain_anchor
        FOR EACH ROW EXECUTE FUNCTION audit.reject_mutation()
        """
    )
    op.execute(f'GRANT SELECT, INSERT ON audit.chain_anchor TO "{_runtime_role()}"')


def downgrade() -> None:
    op.execute(f'REVOKE ALL ON audit.chain_anchor FROM "{_runtime_role()}"')
    op.execute("DROP TRIGGER IF EXISTS chain_anchor_append_only ON audit.chain_anchor")
    op.drop_table("chain_anchor", schema="audit")
    op.execute(_ACCESS_LOG_MESSAGE)
