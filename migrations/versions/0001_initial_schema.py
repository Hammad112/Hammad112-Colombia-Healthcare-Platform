"""Initial schema: application tables, append-only audit log, runtime grants.

Revision ID: 0001
Revises:
Create Date: 2026-09-17

Written by hand so the SQL that runs is the SQL that was reviewed. The audit
trigger and the grants are not expressible as model metadata.
tests/integration/test_migrations.py checks this schema against the models:
tables, columns, types, nullability, indexes, unique and foreign keys through
Alembic's comparison, and CHECK and exclusion constraints by name.

Requires the runtime role (APP_DB_USER) to exist already; `main.py` creates it
before running migrations.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from src.core.config import get_settings

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def _runtime_role() -> str:
    role = get_settings().app_db_user
    if not _IDENTIFIER.match(role):
        raise ValueError(f"APP_DB_USER is not a plain SQL identifier: {role!r}")
    return role


def _enum_check(column: str, values: Sequence[str], name: str) -> sa.CheckConstraint:
    quoted = ", ".join(f"'{value}'" for value in values)
    return sa.CheckConstraint(f"{column} IN ({quoted})", name=name)


def _id() -> sa.Column[sa.Uuid]:
    return sa.Column("id", sa.Uuid(), primary_key=True)


def _clinic_fk() -> sa.Column[sa.Uuid]:
    return sa.Column("clinic_id", sa.Uuid(), sa.ForeignKey("app.clinics.id"), nullable=False)


def _created_at() -> sa.Column[sa.DateTime]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS app")
    op.execute("CREATE SCHEMA IF NOT EXISTS audit")
    # Lets a GiST index combine equality on doctor_id with range overlap.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    # ----------------------------------------------------------------- registry
    op.create_table(
        "clinics",
        _id(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("timezone", sa.String(64), server_default="America/Bogota", nullable=False),
        sa.Column("locale", sa.String(16), server_default="es-CO", nullable=False),
        sa.Column(
            "settings",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        _created_at(),
        schema="app",
    )

    op.create_table(
        "locations",
        _id(),
        _clinic_fk(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("address", sa.String(300), nullable=False),
        _created_at(),
        schema="app",
    )
    op.create_index("ix_locations_clinic_id", "locations", ["clinic_id"], schema="app")

    op.create_table(
        "doctors",
        _id(),
        _clinic_fk(),
        sa.Column("full_name", sa.String(200), nullable=False),
        sa.Column("specialty", sa.String(100), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        _created_at(),
        schema="app",
    )
    op.create_index("ix_doctors_clinic_id", "doctors", ["clinic_id"], schema="app")

    op.create_table(
        "patients",
        _id(),
        _clinic_fk(),
        sa.Column("document_type", sa.String(3), nullable=False),
        sa.Column("document_number", sa.LargeBinary(), nullable=False),
        sa.Column("document_number_bidx", sa.LargeBinary(), nullable=False),
        sa.Column("given_names", sa.LargeBinary(), nullable=False),
        sa.Column("family_names", sa.LargeBinary(), nullable=False),
        sa.Column("birth_date", sa.Date(), nullable=True),
        sa.Column("phone_e164", sa.LargeBinary(), nullable=True),
        sa.Column("phone_e164_bidx", sa.LargeBinary(), nullable=True),
        sa.Column("email", sa.LargeBinary(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        _enum_check(
            "document_type",
            ["CC", "CE", "TI", "RC", "PA", "PE", "PPT", "CN", "AS", "MS"],
            "document_type_valid",
        ),
        schema="app",
    )
    op.create_index(
        "uq_patients_clinic_id_document",
        "patients",
        ["clinic_id", "document_type", "document_number_bidx"],
        unique=True,
        schema="app",
    )
    op.create_index(
        "ix_patients_clinic_id_phone_e164_bidx",
        "patients",
        ["clinic_id", "phone_e164_bidx"],
        schema="app",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # ----------------------------------------------------------------- identity
    op.create_table(
        "phone_bindings",
        _id(),
        _clinic_fk(),
        sa.Column("phone_e164_bidx", sa.LargeBinary(), nullable=False),
        sa.Column("patient_id", sa.Uuid(), sa.ForeignKey("app.patients.id"), nullable=False),
        sa.Column("relationship_kind", sa.String(9), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        _enum_check(
            "relationship_kind", ["self", "guardian", "caregiver"], "relationship_kind_valid"
        ),
        schema="app",
    )
    op.create_index(
        "uq_phone_bindings_clinic_id_phone_patient",
        "phone_bindings",
        ["clinic_id", "phone_e164_bidx", "patient_id"],
        unique=True,
        schema="app",
    )
    op.create_index("ix_phone_bindings_patient_id", "phone_bindings", ["patient_id"], schema="app")

    op.create_table(
        "consents",
        _id(),
        _clinic_fk(),
        sa.Column("patient_id", sa.Uuid(), sa.ForeignKey("app.patients.id"), nullable=False),
        sa.Column("purpose", sa.String(21), nullable=False),
        sa.Column("channel", sa.String(8), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evidence_kind", sa.String(20), nullable=False),
        sa.Column("evidence_ref", sa.String(500), nullable=True),
        sa.Column("policy_version", sa.String(40), nullable=False),
        _created_at(),
        _enum_check(
            "purpose",
            ["appointment_messaging", "wellness_checkins", "sensitive_data", "telemedicine"],
            "purpose_valid",
        ),
        _enum_check("channel", ["whatsapp", "sms", "email", "any"], "channel_valid"),
        _enum_check(
            "evidence_kind",
            ["written", "verbal_recorded", "digital_form", "imported_declaration"],
            "evidence_kind_valid",
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= granted_at", name="revoked_after_granted"
        ),
        schema="app",
    )
    op.create_index(
        "ix_consents_patient_id_purpose", "consents", ["patient_id", "purpose"], schema="app"
    )

    # --------------------------------------------------------------- scheduling
    op.create_table(
        "appointment_types",
        _id(),
        _clinic_fk(),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("buffer_minutes", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("sensitivity", sa.String(6), server_default="normal", nullable=False),
        _created_at(),
        sa.CheckConstraint("duration_minutes > 0", name="duration_positive"),
        sa.CheckConstraint("buffer_minutes >= 0", name="buffer_non_negative"),
        _enum_check("sensitivity", ["normal", "high"], "sensitivity_valid"),
        schema="app",
    )
    op.create_index(
        "ix_appointment_types_clinic_id", "appointment_types", ["clinic_id"], schema="app"
    )

    op.create_table(
        "availability_rules",
        _id(),
        _clinic_fk(),
        sa.Column("doctor_id", sa.Uuid(), sa.ForeignKey("app.doctors.id"), nullable=False),
        sa.Column("location_id", sa.Uuid(), sa.ForeignKey("app.locations.id"), nullable=False),
        sa.Column("weekday", sa.SmallInteger(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_until", sa.Date(), nullable=True),
        _created_at(),
        sa.CheckConstraint("weekday BETWEEN 0 AND 6", name="weekday_range"),
        sa.CheckConstraint("end_time > start_time", name="time_window_ordered"),
        sa.CheckConstraint(
            "valid_until IS NULL OR valid_until >= valid_from", name="validity_ordered"
        ),
        schema="app",
    )
    op.create_index(
        "ix_availability_rules_doctor_id", "availability_rules", ["doctor_id"], schema="app"
    )

    op.create_table(
        "availability_exceptions",
        _id(),
        _clinic_fk(),
        sa.Column("doctor_id", sa.Uuid(), sa.ForeignKey("app.doctors.id"), nullable=False),
        sa.Column("on_date", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(11), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=True),
        sa.Column("end_time", sa.Time(), nullable=True),
        _created_at(),
        _enum_check("kind", ["unavailable", "extra_hours"], "kind_valid"),
        sa.CheckConstraint("(start_time IS NULL) = (end_time IS NULL)", name="times_paired"),
        sa.CheckConstraint(
            "start_time IS NULL OR end_time > start_time", name="time_window_ordered"
        ),
        sa.CheckConstraint(
            "kind <> 'extra_hours' OR start_time IS NOT NULL", name="extra_hours_has_times"
        ),
        schema="app",
    )
    op.create_index(
        "ix_availability_exceptions_doctor_id_on_date",
        "availability_exceptions",
        ["doctor_id", "on_date"],
        schema="app",
    )

    op.create_table(
        "appointments",
        _id(),
        _clinic_fk(),
        sa.Column("patient_id", sa.Uuid(), sa.ForeignKey("app.patients.id"), nullable=False),
        sa.Column("doctor_id", sa.Uuid(), sa.ForeignKey("app.doctors.id"), nullable=False),
        sa.Column("location_id", sa.Uuid(), sa.ForeignKey("app.locations.id"), nullable=False),
        sa.Column(
            "appointment_type_id",
            sa.Uuid(),
            sa.ForeignKey("app.appointment_types.id"),
            nullable=False,
        ),
        sa.Column("during", postgresql.TSTZRANGE(), nullable=False),
        sa.Column("status", sa.String(11), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(120), nullable=True, unique=True),
        _created_at(),
        _enum_check(
            "status",
            [
                "hold",
                "scheduled",
                "confirmed",
                "checked_in",
                "completed",
                "no_show",
                "cancelled",
                "rescheduled",
            ],
            "status_valid",
        ),
        sa.CheckConstraint(
            "NOT isempty(during) AND NOT lower_inf(during) AND NOT upper_inf(during)",
            name="during_bounded",
        ),
        sa.CheckConstraint(
            "(status = 'hold') = (expires_at IS NOT NULL)", name="expiry_only_for_holds"
        ),
        schema="app",
    )
    op.create_index("ix_appointments_patient_id", "appointments", ["patient_id"], schema="app")
    op.create_index(
        "ix_appointments_clinic_id_start",
        "appointments",
        ["clinic_id", sa.text("lower(during)")],
        schema="app",
    )
    # ADR-16/17: PostgreSQL rejects overlapping active bookings or holds for one
    # doctor, whatever code path attempts the write.
    op.execute(
        """
        ALTER TABLE app.appointments
        ADD CONSTRAINT no_overlapping_active_appointments
        EXCLUDE USING gist (doctor_id WITH =, during WITH &&)
        WHERE (status IN ('hold', 'scheduled', 'confirmed', 'checked_in'))
        """
    )

    # -------------------------------------------------------------------- audit
    op.create_table(
        "access_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("clinic_id", sa.Uuid(), nullable=True),
        sa.Column("actor_kind", sa.String(20), nullable=False),
        sa.Column("actor_id", sa.String(120), nullable=True),
        sa.Column("patient_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("resource", sa.String(60), nullable=False),
        sa.Column("resource_id", sa.String(120), nullable=True),
        sa.Column("purpose", sa.String(60), nullable=False),
        sa.Column("channel", sa.String(30), nullable=True),
        sa.Column("request_id", sa.String(128), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("processor", sa.String(60), nullable=True),
        sa.Column("processor_model", sa.String(80), nullable=True),
        sa.Column("fields_disclosed", postgresql.ARRAY(sa.String(60)), nullable=True),
        sa.Column("zero_retention", sa.Boolean(), nullable=True),
        sa.Column("prev_hash", sa.LargeBinary(), nullable=True),
        sa.Column("row_hash", sa.LargeBinary(), nullable=False),
        schema="audit",
    )
    op.create_index(
        "ix_access_log_patient_id_occurred_at",
        "access_log",
        ["patient_id", "occurred_at"],
        schema="audit",
    )
    # Reject UPDATE and DELETE on audit rows from any role, including the owner.
    # TRUNCATE does not fire row-level triggers, and the owner can disable this
    # trigger; the runtime role is granted neither TRUNCATE nor ownership.
    op.execute(
        """
        CREATE FUNCTION audit.reject_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit.access_log is append-only (attempted %)', TG_OP;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER access_log_append_only
        BEFORE UPDATE OR DELETE ON audit.access_log
        FOR EACH ROW EXECUTE FUNCTION audit.reject_mutation()
        """
    )

    # ------------------------------------------------------------------- grants
    role = _runtime_role()
    op.execute(f'GRANT USAGE ON SCHEMA app TO "{role}"')
    op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA app TO "{role}"')
    # The runtime role can read and append audit entries, nothing else.
    op.execute(f'GRANT USAGE ON SCHEMA audit TO "{role}"')
    op.execute(f'GRANT SELECT, INSERT ON audit.access_log TO "{role}"')


def downgrade() -> None:
    role = _runtime_role()
    op.execute(f'REVOKE ALL ON ALL TABLES IN SCHEMA app FROM "{role}"')
    op.execute(f'REVOKE ALL ON audit.access_log FROM "{role}"')
    op.execute(f'REVOKE ALL ON SCHEMA app FROM "{role}"')
    op.execute(f'REVOKE ALL ON SCHEMA audit FROM "{role}"')
    op.drop_table("access_log", schema="audit")
    op.execute("DROP FUNCTION IF EXISTS audit.reject_mutation()")
    for table in (
        "appointments",
        "availability_exceptions",
        "availability_rules",
        "appointment_types",
        "consents",
        "phone_bindings",
        "patients",
        "doctors",
        "locations",
        "clinics",
    ):
        op.drop_table(table, schema="app")
    op.execute("DROP SCHEMA IF EXISTS audit")
    op.execute("DROP SCHEMA IF EXISTS app")
