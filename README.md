# Clinic Scheduler

AI-powered appointment scheduling for small and medium clinics in Colombia.

The planned product sends patients appointment reminders on WhatsApp, understands text
and voice replies to confirm, cancel or reschedule, and offers new times from each
doctor's real availability. Every model-written message is checked by an independent
evaluator before it reaches a patient.

**Status: milestone M0 (foundations).** This repository contains the data model,
encryption, audit logging, database provisioning, LangGraph checkpoint storage, a
read-only review API over synthetic data, and CI. No patient messaging exists yet.

---

## Requirements

- Python 3.12 or newer
- PostgreSQL 16 or newer, running, with an account allowed to create databases and
  roles (the default `postgres` superuser is). The `btree_gist` extension ships with
  standard PostgreSQL installers.

## Run

```bash
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env              # then set POSTGRES_PASSWORD to your postgres password

python main.py
```

`main.py` is the single entry point in the root. Each step is safe to repeat:

```
[1/6] Database localhost:5432/clinic ...     created if missing
[2/6] Runtime role 'clinic_app' ...          created, or password aligned with .env
[3/6] Applying migrations ...
[4/6] Checkpoint schema ...                  LangGraph tables in the `conversation` schema
[5/6] Seeding synthetic data ...             only into an empty database; see below
[6/6] API on http://127.0.0.1:8000
```

Then open http://127.0.0.1:8000/docs.

| Option | Effect |
|---|---|
| `--host`, `--port` | Bind address and port (default `127.0.0.1:8000`) |
| `--skip-seed` | Do not seed synthetic data |
| `--seed` | Seed synthetic data in `staging` or `production` (automatic only in `local` and `ci`) |
| `--no-serve` | Run every step except starting the API |
| `--reset-db` | Drop and recreate the database first. Refused unless `APP_ENV` is `local` or `ci` and `ALLOW_REAL_PATIENT_DATA=false` |
| `--reload` | Restart the API when source files change |

A `docker-compose.yml` is provided as an alternative; it runs the same `main.py`.

### Synthetic data in every environment

Until the compliance gate is signed (`ALLOW_REAL_PATIENT_DATA=false`), every environment
holds synthetic data only, so every environment can be seeded:

| `APP_ENV` | Seeding | Review API and `--reset-db` |
|---|---|---|
| `local`, `ci` | Automatic into an empty database | Enabled |
| `staging`, `production` | Only with `--seed` (and the `seed` extra installed) | Disabled |

Once `ALLOW_REAL_PATIENT_DATA=true`, seeding is refused everywhere, so synthetic records
never sit beside real ones. The seeder enforces this itself, not only `main.py`.

## Configuration

Settings are read from the environment, then `.env`, then one file per setting in the
directory named by `SECRETS_DIR` (the format Docker secrets, Kubernetes secret mounts and
Vault Agent produce). See [`.env.example`](.env.example) for every setting.

This file interface is how the secrets manager is wired in: whichever one the hosting
provider offers delivers its secrets as files into `SECRETS_DIR`, with no vendor SDK in the
code. The choice of provider depends on where the platform is hosted, which is not yet
decided; nothing in the code changes when it is. Outside `local` and `ci` the application
refuses to start on a missing or placeholder secret.

Two database accounts are used on purpose:

| Account | Settings | Used by | Privileges |
|---|---|---|---|
| Owner | `POSTGRES_USER`, `POSTGRES_PASSWORD` | `main.py`, migrations | Creates the database, schema and runtime role |
| Runtime | `APP_DB_USER`, `APP_DB_PASSWORD` | The API and the seeder | Read and write application tables; only read and insert on the audit log |

In `staging` and `production` the application refuses to start if the encryption keys
are shorter than 32 characters or placeholders, or if either database password is
missing or a placeholder.

## Reviewing the data

The review API is read-only and enabled only when `APP_ENV` is `local` or `ci` and
`ALLOW_REAL_PATIENT_DATA=false`. Otherwise a GET to any `/review` route responds 404,
and `/docs` and `/openapi.json` are not served.

It has no authentication, so while it is enabled the server refuses to bind to anything but
the loopback interface; `docker-compose.yml` publishes the port on the host's loopback
interface only.

Every route except `GET /review/clinics` requires a `clinic_id` query parameter and returns
only that clinic's data, which PostgreSQL enforces through row-level security. Start at
`GET /review/clinics` to find an id; omitting it is a 422.

| Endpoint | Returns |
|---|---|
| `GET /review/summary` | Row counts and appointments per status, for one clinic (`clinic_id`) |
| `GET /review/clinics` | Clinics |
| `GET /review/locations` | Locations (`clinic_id`) |
| `GET /review/appointment-types` | Appointment types (`clinic_id`) |
| `GET /review/doctors` | Doctors, paginated (`clinic_id`, `specialty` substring) |
| `GET /review/doctors/{doctor_id}` | A doctor with weekly availability, exceptions and count of upcoming active appointments |
| `GET /review/patients` | Patients, paginated (`clinic_id`; exact `phone` in E.164 or `document_number`) |
| `GET /review/patients/{patient_id}` | A patient with consents, phone bindings and appointments |
| `GET /review/appointments` | Appointments, paginated (`clinic_id`, `doctor_id`, `patient_id`, `status`, `date_from`, `date_to`) |
| `GET /review/appointments/{appointment_id}` | One appointment; start and end in Bogotá time |
| `GET /review/consents` | Consents, paginated (`clinic_id`, `patient_id`, `purpose`, `active_only`) |
| `GET /review/phone-bindings/shared` | Phone numbers shared by several patients, identified by an opaque reference |
| `GET /review/audit-log` | Audit entries, newest first (`patient_id`, `action`, `resource`) |
| `GET /review/audit-log/verify` | Recomputes the keyed audit hash chain and compares it with the newest anchor; `first_broken_id` names an altered entry, `truncated_after_id` a removal of the newest entries |
| `GET /healthz` | Process is running |
| `GET /readyz` | Database reachable as the runtime role; reports the real-data gate |

Paginated endpoints accept `limit` (1-200, default 50) and `offset`.

Safeguards while staff authentication does not exist (it arrives in M11):

- Document and phone numbers show only their last four characters; emails show only the
  first character and the domain.
- Every patient-data read writes an audit entry attributed to `local-reviewer`.
- Names and identifiers are encrypted at rest, so there is no name search; lookups by
  phone or document number are exact matches through HMAC blind indexes.

## What M0 guarantees, and the test that proves each

| Guarantee | Enforced by | Proven by |
|---|---|---|
| No overlapping active bookings or holds for a doctor, even under concurrent writes | PostgreSQL exclusion constraint | `tests/integration/test_constraints.py` |
| Every patient-data read through the API is audited | Repositories record each access | `tests/integration/test_review_api.py` |
| The application cannot rewrite the audit log | Runtime role granted only SELECT and INSERT; a trigger also rejects UPDATE and DELETE from any role | `tests/integration/test_migrations.py` |
| An altered audit entry, or one removed or inserted before the newest, is detected | SHA-256 hash chain covering every field except the entry's id and hashes | `tests/integration/test_audit_service.py` |
| Audit entries commit before the response is sent | Request session closes when the endpoint returns | `tests/unit/test_error_handling.py` |
| The database schema matches the models | Hand-written migration; tables, columns, indexes, keys, CHECK and exclusion constraints compared | `tests/integration/test_migrations.py` |
| Direct identifiers are stored as ciphertext | AES-256-GCM per column | `tests/integration/test_review_api.py`, `tests/unit/test_crypto.py` |
| Soft-deleted patients appear in no patient query | Repository filters | `tests/integration/test_review_api.py` |
| The review API is unavailable outside synthetic-data mode | Access dependency; OpenAPI document not served | `tests/unit/test_review_access.py`, `tests/unit/test_error_handling.py` |
| No secrets in source control | gitleaks in CI and pre-commit | CI job `Secret scan` |

## Tests and checks

```bash
pytest -q                                   # unit and integration tests
ruff check . && ruff format --check .       # lint and formatting
mypy src main.py                            # strict type checking
```

Integration tests never touch the application database. They drop, recreate and
provision a separate `clinic_test` database at the start of each run, using the same
steps as `main.py`. If PostgreSQL is unreachable they are skipped and the reason is shown.

## Layout

```
main.py              entry point
src/bootstrap.py     database, runtime role, migrations
src/core/            settings, database, encryption, logging, pagination, time zone
src/audit/           audit context, access log model, recording and verification
src/registry/        clinics, locations, doctors, patients
src/identity/        phone bindings, consents
src/scheduling/      appointment types, availability, appointments
src/conversation/    LangGraph checkpoint storage
src/devdata/         synthetic data seeder
src/api/             application factory, middleware, health, review API
migrations/          Alembic migrations
tests/               unit/ and integration/
```

## Known limitations at M0

- Staff authentication does not exist. The review API is the only data interface and is enabled
  only in synthetic-data mode; while it is enabled the server refuses to bind anywhere but the
  loopback interface. A request names its clinic in a `clinic_id` query parameter, which
  authentication will replace with the signed-in user's clinic.
- Rate limiting uses the in-process backend, so each API worker enforces its own limit. The
  `RateLimiter` protocol in `src/core/ratelimit.py` is where a shared Redis backend plugs in,
  which is required before running more than one worker.
- Patient data must not be stored in a conversation channel whose value is a bare string, number
  or boolean: LangGraph writes those into the `checkpoints` row without consulting the encrypting
  serializer. Structured values are encrypted.
- The checkpoint retention sweep is run by hand (`python -m src.conversation.checkpointer`) until
  background jobs arrive in M2.
- The audit chain is keyed and anchored, but an anchor bounds the loss only up to the last anchor
  taken; the API anchors on startup and shutdown, so entries written since then are not yet
  witnessed. Scheduled anchoring arrives with M2.
- The document type list follows MinSalud's Documento Técnico 1 under Resolución 948 de 2026.
  That table now lives outside the resolution and can change without a new norm, so it needs
  re-checking before real clinic files are imported in M1.

## License

Proprietary. All rights reserved.
