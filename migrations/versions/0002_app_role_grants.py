"""Least-privilege application role: INSERT-only on the audit schema.

Append-only, mechanism 2. The trigger in 0001 refuses mutation; this removes
the privilege to attempt it. Two independent mechanisms, because an audit log
that the application can rewrite is not an audit log.

Revision ID: 0002
Revises: 0001
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


_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _checked_role(name: str) -> str:
    """Role names are interpolated into DDL, so validate rather than trust config."""
    if not _SAFE_IDENTIFIER.match(name):
        raise ValueError(f"Unsafe role name in APP_DB_USER: {name!r}")
    return name


def upgrade() -> None:
    settings = get_settings()
    role = _checked_role(settings.app_db_user)
    password = settings.app_db_password.get_secret_value()

    # Quoting: role name is an identifier, password is a literal.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                CREATE ROLE "{role}" LOGIN PASSWORD '{password}';
            END IF;
        END
        $$;
        """
    )

    op.execute(f'GRANT USAGE ON SCHEMA app TO "{role}"')
    op.execute(
        f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA app TO "{role}"'
    )
    op.execute(f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA app TO "{role}"')
    op.execute(
        f'ALTER DEFAULT PRIVILEGES IN SCHEMA app GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{role}"'
    )

    # Audit: read and append only. No UPDATE, no DELETE, ever.
    op.execute(f'GRANT USAGE ON SCHEMA audit TO "{role}"')
    op.execute(f'GRANT SELECT, INSERT ON audit.access_log TO "{role}"')
    op.execute(f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA audit TO "{role}"')
    op.execute(f'REVOKE UPDATE, DELETE, TRUNCATE ON audit.access_log FROM "{role}"')
    op.execute(
        f'ALTER DEFAULT PRIVILEGES IN SCHEMA audit GRANT SELECT, INSERT ON TABLES TO "{role}"'
    )


def downgrade() -> None:
    role = _checked_role(get_settings().app_db_user)
    op.execute(f'REVOKE ALL ON ALL TABLES IN SCHEMA app FROM "{role}"')
    op.execute(f'REVOKE ALL ON audit.access_log FROM "{role}"')
    op.execute(f'REVOKE ALL ON SCHEMA app FROM "{role}"')
    op.execute(f'REVOKE ALL ON SCHEMA audit FROM "{role}"')
