"""Single entry point for the platform.

    python main.py

Runs, in order:
  1. Connects to PostgreSQL using the settings in `.env`, and creates the
     application database if it does not exist yet.
  2. Applies database migrations (alembic upgrade head).
  3. Seeds synthetic data, only in local/CI environments with the real-data gate
     closed, and only if the database is empty (safe to run repeatedly).
  4. Starts the API server.

Options:
    --host 0.0.0.0      bind address (default 127.0.0.1)
    --port 8000         port (default 8000)
    --skip-migrate      do not run migrations
    --skip-seed         do not seed synthetic data
    --reload            auto-reload on code changes (not supported on Windows)

Requires a running PostgreSQL 16+ (with the standard btree_gist extension).
Configuration comes from `.env` (copy `.env.example`). See README.md.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _ensure_database() -> None:
    """Connect to the server and create the application database if missing."""
    import psycopg
    from psycopg import sql

    from src.core.config import get_settings

    s = get_settings()
    try:
        # Connect to the always-present maintenance database to check for ours.
        with psycopg.connect(
            host=s.postgres_host,
            port=s.postgres_port,
            user=s.postgres_user,
            password=s.postgres_password.get_secret_value(),
            dbname="postgres",
            connect_timeout=5,
            autocommit=True,  # CREATE DATABASE cannot run inside a transaction
        ) as conn:
            exists = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (s.postgres_db,)
            ).fetchone()
            if exists:
                print(f"      Database '{s.postgres_db}' found.")
            else:
                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(s.postgres_db)))
                print(f"      Database '{s.postgres_db}' created.")
    except psycopg.OperationalError as exc:
        print(
            f"\nCannot connect to PostgreSQL at {s.postgres_host}:{s.postgres_port} "
            f"as '{s.postgres_user}'.\n"
            f"  {str(exc).strip().splitlines()[-1]}\n\n"
            "Check that PostgreSQL is running, and that POSTGRES_HOST, POSTGRES_PORT,\n"
            "POSTGRES_USER and POSTGRES_PASSWORD in .env are correct.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


def _migrate() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(cfg, "head")


def _seed() -> None:
    from scripts.seed_synthetic import seed
    from src.core.db import dispose_engine

    async def run() -> None:
        try:
            await seed(n_patients=200, n_doctors=8)
        finally:
            await dispose_engine()  # the server opens its own engine in its own loop

    asyncio.run(run())


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the clinic scheduling platform")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--skip-migrate", action="store_true")
    parser.add_argument("--skip-seed", action="store_true")
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    from src.api.run import serve
    from src.core.config import get_settings
    from src.core.db import configure_event_loop_policy

    configure_event_loop_policy()
    settings = get_settings()

    print(f"[1/4] Connecting to PostgreSQL at {settings.postgres_host}:{settings.postgres_port} ...")
    _ensure_database()

    if args.skip_migrate:
        print("[2/4] Migrations skipped.")
    else:
        print("[2/4] Applying migrations ...")
        _migrate()

    seeding_allowed = settings.app_env in ("local", "ci") and not settings.allow_real_patient_data
    if args.skip_seed or not seeding_allowed:
        reason = "--skip-seed" if args.skip_seed else f"APP_ENV={settings.app_env} or real data enabled"
        print(f"[3/4] Seeding skipped ({reason}).")
    else:
        print("[3/4] Seeding synthetic data (skipped automatically if data exists) ...")
        _seed()

    print(f"[4/4] Starting API on http://{args.host}:{args.port}  (docs: /docs, health: /healthz)")
    serve(args.host, args.port, args.reload)


if __name__ == "__main__":
    main()
