from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from src.core.config import get_settings
from src.core.db import configure_event_loop_policy
from src.models import AuditBase, Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Credentials come from settings (environment / secrets manager), never alembic.ini.
config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = [Base.metadata, AuditBase.metadata]


def include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:  # noqa: ARG001
    # Exclusion constraints are hand-written (autogenerate cannot detect them).
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        include_schemas=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: object) -> None:
    context.configure(
        connection=connection,  # type: ignore[arg-type]
        target_metadata=target_metadata,
        include_schemas=True,
        include_object=include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    configure_event_loop_policy()
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
