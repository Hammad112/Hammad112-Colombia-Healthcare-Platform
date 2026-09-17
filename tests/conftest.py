from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest

# Test-only keys. Set before any import that reads settings.
os.environ.setdefault("APP_ENV", "ci")
os.environ.setdefault("PHI_ENCRYPTION_KEY", "placeholder-test-phi-encryption-value")
os.environ.setdefault("PHI_BLIND_INDEX_KEY", "placeholder-test-phi-blind-index-value")
os.environ.setdefault("ALLOW_REAL_PATIENT_DATA", "false")

# Tests create and DROP tables. They must never touch the database the app runs
# on, so they always use a separate one (default: clinic_test), created on demand.
# Environment variables override .env, so this wins over POSTGRES_DB in .env.
os.environ["POSTGRES_DB"] = os.environ.get("TEST_POSTGRES_DB", "clinic_test")
assert os.environ["POSTGRES_DB"].endswith("_test"), (
    "Refusing to run tests against a non-test database"
)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.api.main import create_app  # noqa: E402
from src.core.config import get_settings  # noqa: E402
from src.core.db import configure_event_loop_policy  # noqa: E402
from src.models import AuditBase, Base  # noqa: E402

configure_event_loop_policy()


def database_available() -> bool:
    """True when a PostgreSQL instance is reachable for integration tests."""
    import socket

    settings = get_settings()
    try:
        with socket.create_connection((settings.postgres_host, settings.postgres_port), timeout=1):
            pass
    except OSError:
        return False
    return _ensure_test_database()


def _ensure_test_database() -> bool:
    import psycopg
    from psycopg import sql

    s = get_settings()
    try:
        with psycopg.connect(
            host=s.postgres_host,
            port=s.postgres_port,
            user=s.postgres_user,
            password=s.postgres_password.get_secret_value(),
            dbname="postgres",
            connect_timeout=5,
            autocommit=True,
        ) as conn:
            if not conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (s.postgres_db,)
            ).fetchone():
                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(s.postgres_db)))
        return True
    except psycopg.OperationalError:
        return False


requires_db = pytest.mark.skipif(
    not database_available(), reason="PostgreSQL not reachable; integration test skipped"
)


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session against a freshly created schema."""
    engine = create_async_engine(get_settings().database_url, poolclass=None)
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS app")
        await conn.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS audit")
        await conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS btree_gist")
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(AuditBase.metadata.create_all)
        await conn.exec_driver_sql(
            """
            ALTER TABLE app.appointments
            DROP CONSTRAINT IF EXISTS no_overlapping_active_appointments
            """
        )
        await conn.exec_driver_sql(
            """
            ALTER TABLE app.appointments
            ADD CONSTRAINT no_overlapping_active_appointments
            EXCLUDE USING gist (doctor_id WITH =, during WITH &&)
            WHERE (status IN ('hold','scheduled','confirmed','checked_in'))
            """
        )
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    async with engine.begin() as conn:
        await conn.run_sync(AuditBase.metadata.drop_all)
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()
