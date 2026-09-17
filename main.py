"""Single entry point for the platform.

    python main.py

Runs, in order:
  1. Checks the database is reachable (clear message if not).
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

Configuration comes from `.env` (copy `.env.example`). See README.md.
"""

from __future__ import annotations

import argparse
import asyncio
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _database_reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


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

    print(f"[1/4] Checking database at {settings.postgres_host}:{settings.postgres_port} ...")
    if not _database_reachable(settings.postgres_host, settings.postgres_port):
        print(
            "\nDatabase is not reachable. Either:\n"
            "  - start PostgreSQL and set POSTGRES_HOST / POSTGRES_PORT in .env, or\n"
            "  - run 'docker compose up db -d' to start one (then set POSTGRES_HOST=localhost),\n"
            "  - or run the whole stack with 'docker compose up --build'.",
            file=sys.stderr,
        )
        raise SystemExit(1)

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
