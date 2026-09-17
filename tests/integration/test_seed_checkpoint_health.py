"""Synthetic seeding, the LangGraph checkpointer, readiness, and runtime-role provisioning."""

from __future__ import annotations

import operator
from datetime import date, datetime
from typing import Annotated, TypedDict

import psycopg
from fastapi.testclient import TestClient
from langgraph.graph import END, START, StateGraph
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src import bootstrap
from src.audit.context import AuditContext
from src.audit.models import AccessLogEntry
from src.audit.service import verify_chain
from src.conversation.checkpointer import CHECKPOINT_SCHEMA, open_checkpointer
from src.core.config import Settings
from src.core.timezones import BOGOTA
from src.devdata.seed import (
    APPOINTMENTS_PER_DOCTOR,
    SHARED_HANDSET_PATIENTS,
    seed_synthetic_data,
)
from src.identity.models import PhoneBinding
from src.registry.models import Patient
from src.scheduling.models import Appointment


async def test_seed_creates_expected_records_and_audits_them(
    session: AsyncSession, staff_context: AuditContext
) -> None:
    report = await seed_synthetic_data(session, patients=20, doctors=3, today=date(2026, 10, 1))
    await session.commit()

    assert report is not None
    assert (report.patients, report.doctors) == (20, 3)
    assert report.appointments == 3 * APPOINTMENTS_PER_DOCTOR
    assert len((await session.scalars(select(Patient))).all()) == 20
    assert len((await session.scalars(select(PhoneBinding))).all()) == 20 + SHARED_HANDSET_PATIENTS

    created = (
        await session.scalars(select(AccessLogEntry).where(AccessLogEntry.action == "create"))
    ).all()
    assert len(created) == 20 + (20 + SHARED_HANDSET_PATIENTS) + 20 + report.appointments
    assert (await verify_chain(session)).intact

    # 2026-10-01 is a Thursday, so the first appointment day is Friday 2 October.
    first = min(a.during.lower for a in (await session.scalars(select(Appointment))).all())
    assert first == datetime(2026, 10, 2, 8, 0, tzinfo=BOGOTA)


async def test_seed_refuses_a_non_empty_database(
    session: AsyncSession, staff_context: AuditContext
) -> None:
    assert await seed_synthetic_data(session, patients=2, doctors=1) is not None
    await session.commit()
    assert await seed_synthetic_data(session, patients=2, doctors=1) is None


async def test_seeded_appointments_never_overlap(
    session: AsyncSession, staff_context: AuditContext
) -> None:
    # The exclusion constraint would reject overlaps; this documents that the
    # seeder produces a valid schedule rather than relying on luck.
    await seed_synthetic_data(session, patients=10, doctors=2)
    await session.commit()
    rows = (await session.scalars(select(Appointment))).all()
    assert len(rows) == 2 * APPOINTMENTS_PER_DOCTOR


class _Counter(TypedDict):
    visits: Annotated[int, operator.add]


async def test_runtime_role_can_persist_and_resume_graph_state(settings: Settings) -> None:
    graph = StateGraph(_Counter)
    graph.add_node("visit", lambda state: {"visits": 1})
    graph.add_edge(START, "visit")
    graph.add_edge("visit", END)
    config = {"configurable": {"thread_id": "integration-test-thread"}}

    async with open_checkpointer(settings) as checkpointer:
        compiled = graph.compile(checkpointer=checkpointer)
        await compiled.ainvoke({"visits": 0}, config)
        await compiled.ainvoke({"visits": 0}, config)
        state = await compiled.aget_state(config)

    assert state.values["visits"] == 2

    with psycopg.connect(settings.conninfo(admin=True)) as conn:
        schema = conn.execute(
            "SELECT table_schema FROM information_schema.tables WHERE table_name = 'checkpoints'"
        ).fetchall()
    assert schema == [(CHECKPOINT_SCHEMA,)]


def test_readiness_uses_the_runtime_role(client: TestClient) -> None:
    body = client.get("/readyz").json()
    assert body == {
        "status": "ok",
        "database": "ok",
        "app_env": "ci",
        "real_patient_data_allowed": False,
    }


def test_ensure_runtime_role_is_idempotent_and_password_works(settings: Settings) -> None:
    bootstrap.ensure_runtime_role(settings)
    bootstrap.ensure_runtime_role(settings)
    with psycopg.connect(settings.conninfo(admin=False)) as conn:
        assert conn.execute("SELECT current_user").fetchone() == (settings.app_db_user,)
