"""Generate synthetic Colombian clinic data (ADR-11).

Refuses to run against an environment that permits real patient data, so it can
never overwrite production records. All names, document numbers and phone numbers
are generated; none correspond to real people.

Usage:
    python -m scripts.seed_synthetic [--patients 200] [--doctors 8]
"""

from __future__ import annotations

import argparse
import asyncio
import random
import uuid
from datetime import date, datetime, time, timedelta, timezone

from faker import Faker
from sqlalchemy import select

from src.audit.context import AuditContext, set_context
from src.core.config import get_settings
from src.core.crypto import blind_index
from src.core.db import configure_event_loop_policy, dispose_engine, get_sessionmaker
from src.models import (
    Appointment,
    AppointmentType,
    AvailabilityRule,
    Clinic,
    Consent,
    Doctor,
    Location,
    Patient,
    PhoneBinding,
)

BOGOTA = timezone(timedelta(hours=-5))  # America/Bogota, no DST

SPECIALTIES = [
    "Medicina General",
    "Pediatría",
    "Ginecología",
    "Ortopedia",
    "Dermatología",
    "Cardiología",
    "Oftalmología",
    "Urología",
]
DOCUMENT_TYPES = ["CC", "CC", "CC", "TI", "CE", "PPT"]  # weighted toward cédula


def colombian_mobile(fake: Faker) -> str:
    """Colombian mobiles are 10 digits starting with 3, in E.164 as +57 3XXXXXXXXX."""
    return "+573" + "".join(str(random.randint(0, 9)) for _ in range(9))


async def seed(n_patients: int, n_doctors: int) -> None:
    settings = get_settings()
    if settings.allow_real_patient_data:
        raise SystemExit(
            "Refusing to seed: ALLOW_REAL_PATIENT_DATA is true. "
            "Synthetic seeding is for non-production environments only (ADR-11)."
        )

    fake = Faker("es_CO")
    Faker.seed(20260917)
    random.seed(20260917)

    set_context(AuditContext(actor_kind="system", purpose="synthetic_seed"))

    async with get_sessionmaker()() as session:
        existing = (await session.execute(select(Clinic).limit(1))).scalar_one_or_none()
        if existing is not None:
            print("Data already present; nothing seeded. Drop the volume to reseed.")
            return

        clinic = Clinic(id=uuid.uuid4(), name="Clínica Demo Bogotá")
        session.add(clinic)
        # Flush the parent before its children: these models carry foreign keys
        # without ORM relationships (deliberately — see lazy="raise"), so the
        # unit of work cannot infer the insert order on its own.
        await session.flush()

        location = Location(
            id=uuid.uuid4(),
            clinic_id=clinic.id,
            name="Sede Principal",
            address="Calle 100 # 15-20, Bogotá",
        )
        session.add(location)

        appt_type = AppointmentType(
            id=uuid.uuid4(),
            clinic_id=clinic.id,
            name="Consulta general",
            duration_minutes=20,
            buffer_minutes=0,
        )
        session.add(appt_type)
        await session.flush()  # location and appointment type exist

        doctors = []
        for i in range(n_doctors):
            doctors.append(
                Doctor(
                    id=uuid.uuid4(),
                    clinic_id=clinic.id,
                    full_name=f"Dr(a). {fake.first_name()} {fake.last_name()} {fake.last_name()}",
                    specialty=SPECIALTIES[i % len(SPECIALTIES)],
                )
            )
        session.add_all(doctors)
        await session.flush()  # doctors exist before anything references them

        for doctor in doctors:
            for weekday in range(5):  # Monday to Friday mornings
                session.add(
                    AvailabilityRule(
                        id=uuid.uuid4(),
                        clinic_id=clinic.id,
                        doctor_id=doctor.id,
                        location_id=location.id,
                        weekday=weekday,
                        start_time=time(8, 0),
                        end_time=time(12, 0),
                        valid_from=date(2026, 1, 1),
                    )
                )

        await session.flush()

        patients = []
        for _ in range(n_patients):
            phone = colombian_mobile(fake)
            doc_number = str(random.randint(10_000_000, 1_999_999_999))
            patient = Patient(
                id=uuid.uuid4(),
                clinic_id=clinic.id,
                document_type=random.choice(DOCUMENT_TYPES),
                document_number=doc_number,
                document_number_bidx=blind_index(doc_number),
                given_names=fake.first_name(),
                # Two surnames, as is the Colombian norm.
                family_names=f"{fake.last_name()} {fake.last_name()}",
                birth_date=fake.date_of_birth(minimum_age=1, maximum_age=92),
                phone_e164=phone,
                phone_e164_bidx=blind_index(phone),
                email=fake.email(),
            )
            patients.append(patient)
            session.add(patient)
        await session.flush()  # patients exist before bindings/consents

        for patient in patients:
            phone = patient.phone_e164
            assert phone is not None
            session.add(
                PhoneBinding(
                    id=uuid.uuid4(),
                    clinic_id=clinic.id,
                    phone_e164_bidx=blind_index(phone),
                    patient_id=patient.id,
                    relationship_kind="self",
                    verified_at=datetime.now(tz=BOGOTA),
                )
            )
            session.add(
                Consent(
                    id=uuid.uuid4(),
                    clinic_id=clinic.id,
                    patient_id=patient.id,
                    purpose="appointment_messaging",
                    channel="whatsapp",
                    granted_at=datetime.now(tz=BOGOTA),
                    evidence_kind="imported_declaration",
                    policy_version="v1-2026-09",
                )
            )

        await session.flush()

        # A shared handset: one number, several patients (ADR-18 test fixture).
        shared_phone = colombian_mobile(fake)
        for patient in patients[:3]:
            session.add(
                PhoneBinding(
                    id=uuid.uuid4(),
                    clinic_id=clinic.id,
                    phone_e164_bidx=blind_index(shared_phone),
                    patient_id=patient.id,
                    relationship_kind="caregiver",
                    verified_at=datetime.now(tz=BOGOTA),
                )
            )

        # Appointments on a non-overlapping grid, so the exclusion constraint
        # is satisfied by construction rather than by luck.
        base = datetime.now(tz=BOGOTA).replace(hour=8, minute=0, second=0, microsecond=0)
        base += timedelta(days=1)
        created = 0
        for doctor in doctors:
            for slot_index in range(12):
                start = base + timedelta(minutes=20 * slot_index)
                end = start + timedelta(minutes=20)
                session.add(
                    Appointment(
                        id=uuid.uuid4(),
                        clinic_id=clinic.id,
                        patient_id=random.choice(patients).id,
                        doctor_id=doctor.id,
                        location_id=location.id,
                        appointment_type_id=appt_type.id,
                        during=f"[{start.isoformat()},{end.isoformat()})",
                        status="scheduled",
                    )
                )
                created += 1

        await session.commit()

    print(
        f"Seeded: 1 clinic, {n_doctors} doctors, {n_patients} patients, "
        f"{created} appointments, 1 shared-handset fixture. All synthetic."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed synthetic clinic data")
    parser.add_argument("--patients", type=int, default=200)
    parser.add_argument("--doctors", type=int, default=8)
    args = parser.parse_args()
    configure_event_loop_policy()
    try:
        asyncio.run(seed(args.patients, args.doctors))
    finally:
        asyncio.run(dispose_engine())


if __name__ == "__main__":
    main()
