# Clinic Scheduler

AI-powered medical appointment scheduling for small and medium clinics in Colombia.

Patients receive appointment reminders on their usual messaging app, reply by text or
voice note to confirm, cancel or reschedule, and the agent negotiates a new time against
the doctor's real availability. Every message a model writes is checked by an independent
evaluator before it reaches a patient.

**Current status: M0 (Foundations) complete.** No patient-facing behaviour yet.

---

## Quick start

Everything starts from one file, `main.py`. It checks the database, applies migrations,
seeds synthetic data (local only, and only into an empty database), then starts the API.

```bash
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env              # set POSTGRES_HOST=localhost and your database credentials

python main.py
```

Then open http://127.0.0.1:8000/docs, or:

```bash
curl http://127.0.0.1:8000/healthz     # {"status":"ok"}
curl http://127.0.0.1:8000/readyz      # database reachable, compliance gate reported
```

`main.py` is safe to run repeatedly. Options:

| Flag | Effect |
|---|---|
| `--host 0.0.0.0` | Bind address (default `127.0.0.1`) |
| `--port 8000` | Port (default `8000`) |
| `--skip-migrate` | Do not run migrations |
| `--skip-seed` | Do not seed synthetic data |
| `--reload` | Auto-reload on code changes (not supported on Windows) |

It needs a reachable PostgreSQL 16+. If you do not have one, `docker compose up db -d`
starts one, or run the whole stack in containers with `docker compose up --build`,
which uses the same `main.py` inside the container.

## Commands

| Command | What it does |
|---|---|
| `python main.py` | Migrate, seed and serve: the single entry point |
| `docker compose up --build` | Whole stack in containers, via the same `main.py` |
| `alembic upgrade head` | Apply migrations |
| `python -m scripts.seed_synthetic` | Seed synthetic data (refuses to run if real data is enabled) |
| `pytest -q` | All tests |
| `pytest tests/test_schema_constraints.py -q` | Proves overlapping bookings are impossible |
| `pytest tests/test_audit.py -q` | Proves the audit log is append-only |
| `ruff check . && mypy src` | Lint and types |
| `gitleaks detect --no-git -c .gitleaks.toml` | Proves no secrets in the tree |

Tests that need a database skip automatically when none is reachable, so `pytest` is
always runnable. CI runs them against a real PostgreSQL service, so they are never
silently skipped where it matters.

## What M0 delivers

| Exit criterion (from the scope) | How to verify |
|---|---|
| All services start from a clean checkout with one command | `python main.py` (or `docker compose up --build`), then `curl /healthz` |
| No secret values in source control, verified by a scan | `gitleaks detect --no-git`; CI fails the build on any finding |
| Base schema migrated and seedable with synthetic data | `alembic upgrade head && python -m scripts.seed_synthetic` |

Beyond the scope's list, M0 also lands the things that are cheap now and expensive to
retrofit later: multi-tenant scoping, the consent table, the append-only audit schema,
request-scoped audit context, column encryption with a blind index for lookup, rate
limiting, body-size limits, and the exclusion constraint that makes double-booking
structurally impossible.

## Layout

```
src/
  api/        FastAPI app, routers, edge middleware
  audit/      append-only audit context and writer
  core/       settings, database, logging, encryption
  models/     SQLAlchemy models (app and audit schemas)
migrations/   Alembic; exclusion constraints are hand-written
scripts/      synthetic data seeding
tests/        unit tests, plus integration tests gated on a live database
```

## Security and compliance notes

- **No real patient data** may be processed until the compliance package is signed.
  `ALLOW_REAL_PATIENT_DATA` defaults to `false` and the seeder refuses to run when it is true.
- **No credentials in the repository.** Local values live in `.env`, which is gitignored.
  Staging and production resolve secrets at runtime, and the application refuses to start
  with a development key outside local environments.
- **Patient data never reaches the logs.** Direct identifiers are redacted by a log processor.
- Health data is sensitive personal data under Colombian law. See `ARCHITECTURE.md` §12.

## License

Proprietary. All rights reserved.
