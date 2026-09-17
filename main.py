"""Single entry point: provision the database, seed synthetic data, serve the API.

    python main.py [options]

Steps, in order. Every step is safe to repeat.

  1. Connect to PostgreSQL as the owner account and create POSTGRES_DB if missing.
  2. Create the runtime role APP_DB_USER, or set its password to the configured value.
  3. Apply database migrations.
  4. Create the LangGraph checkpoint schema.
  5. Seed synthetic data, if in synthetic-data mode and the database has no clinic.
  6. Start the API.

Options:
  --host HOST     bind address (default 127.0.0.1)
  --port PORT     port (default 8000)
  --skip-seed     skip step 5
  --no-serve      stop after step 5; used by CI
  --reset-db      drop and recreate the database first; synthetic-data mode only
  --reload        restart the API when source files change

Configuration is read from the environment, `.env` and SECRETS_DIR; see
src/core/config.py and .env.example.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from src import bootstrap
from src.api.server import serve
from src.conversation.checkpointer import ensure_checkpoint_schema
from src.core.config import get_settings
from src.core.db import configure_event_loop_policy


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the clinic scheduling platform")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--skip-seed", action="store_true")
    parser.add_argument("--no-serve", action="store_true")
    parser.add_argument("--reset-db", action="store_true")
    parser.add_argument("--reload", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    configure_event_loop_policy()
    settings = get_settings()
    target = f"{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}"

    try:
        if args.reset_db:
            print(f"[0/6] Dropping and recreating {target} (synthetic data only) ...")
            bootstrap.reset_database(settings)

        print(f"[1/6] Database {target} ...")
        created = bootstrap.ensure_database(settings)
        print("      created." if created else "      exists.")

        print(f"[2/6] Runtime role '{settings.app_db_user}' ...")
        bootstrap.ensure_runtime_role(settings)

        print("[3/6] Applying migrations ...")
        bootstrap.run_migrations()

        print("[4/6] Checkpoint schema ...")
        asyncio.run(ensure_checkpoint_schema(settings))
    except bootstrap.ProvisioningError as exc:
        print(f"\n{exc}", file=sys.stderr)
        raise SystemExit(1) from None

    if args.skip_seed:
        print("[5/6] Seeding skipped (--skip-seed).")
    elif not settings.synthetic_data_mode:
        print(f"[5/6] Seeding skipped (APP_ENV={settings.app_env} or real patient data enabled).")
    else:
        print("[5/6] Seeding synthetic data ...")
        # Imported here: the seeder needs Faker, which production installs omit.
        from src.devdata.seed import run_seed

        report = asyncio.run(run_seed())
        if report is None:
            print("      database already has data; nothing seeded.")
        else:
            print(
                f"      {report.doctors} doctors, {report.patients} patients, "
                f"{report.appointments} appointments."
            )

    if args.no_serve:
        print("[6/6] Not serving (--no-serve).")
        return

    print(f"[6/6] API on http://{args.host}:{args.port}  (docs: /docs, health: /healthz)")
    serve(args.host, args.port, reload=args.reload)


if __name__ == "__main__":
    main()
