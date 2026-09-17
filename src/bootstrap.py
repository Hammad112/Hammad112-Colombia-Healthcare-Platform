"""Database provisioning run by the owner account before the application starts.

Used by `main.py` and by the integration test suite. Every step is idempotent.

1. `ensure_database`       create POSTGRES_DB if it does not exist.
2. `ensure_runtime_role`   create APP_DB_USER, or set its password to the configured value.
3. `run_migrations`        apply Alembic migrations (grants the runtime role its privileges).
4. `ensure_checkpoint_schema` (src/conversation/checkpointer.py) create LangGraph tables.

`reset_database` drops and recreates the database. It is refused unless the
settings are in synthetic-data mode.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql

from src.core.config import Settings

_ROOT = Path(__file__).resolve().parent.parent


class ProvisioningError(RuntimeError):
    """Raised with an operator-facing message when provisioning cannot proceed."""


def _connect_maintenance(settings: Settings) -> psycopg.Connection:
    """Connect as the owner to the always-present `postgres` database."""
    try:
        return psycopg.connect(
            settings.conninfo(admin=True, dbname="postgres"),
            connect_timeout=5,
            autocommit=True,  # CREATE/DROP DATABASE cannot run in a transaction block
        )
    except psycopg.OperationalError as exc:
        reason = str(exc).strip().splitlines()[-1] if str(exc).strip() else type(exc).__name__
        raise ProvisioningError(
            f"Cannot connect to PostgreSQL at {settings.postgres_host}:{settings.postgres_port} "
            f"as '{settings.postgres_user}': {reason}\n"
            "Check that PostgreSQL is running and that POSTGRES_HOST, POSTGRES_PORT, "
            "POSTGRES_USER and POSTGRES_PASSWORD are correct."
        ) from None


def ensure_database(settings: Settings) -> bool:
    """Create the database if missing. Returns True if it was created."""
    with _connect_maintenance(settings) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (settings.postgres_db,)
        ).fetchone()
        if exists:
            return False
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(settings.postgres_db)))
        return True


def reset_database(settings: Settings) -> None:
    """Drop and recreate the database. Synthetic-data mode only."""
    if not settings.synthetic_data_mode:
        raise ProvisioningError(
            "Refusing to reset the database: allowed only when APP_ENV is local or ci "
            "and ALLOW_REAL_PATIENT_DATA is false."
        )
    with _connect_maintenance(settings) as conn:
        # FORCE (PostgreSQL 13+) terminates other sessions connected to the database.
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(settings.postgres_db)
            )
        )
    ensure_database(settings)


def ensure_runtime_role(settings: Settings) -> None:
    """Create the runtime login role, or align its password with configuration.

    Roles are cluster-wide, so every database on the server shares this role.
    The password is sent inside the SQL statement; a server configured with
    `log_statement = 'ddl'` or `'all'` will write it to the server log.
    """
    role = settings.app_db_user
    password = settings.app_db_password.get_secret_value()
    if not password:
        raise ProvisioningError("APP_DB_PASSWORD is empty; the runtime role needs a password.")
    with _connect_maintenance(settings) as conn:
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
        verb = "ALTER" if exists else "CREATE"
        conn.execute(
            sql.SQL(verb + " ROLE {} WITH LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )


def run_migrations() -> None:
    config = Config(str(_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_ROOT / "migrations"))
    command.upgrade(config, "head")
