"""Server runner that guarantees a psycopg-compatible event loop.

psycopg's async mode cannot run on Windows' default proactor event loop, and
uvicorn installs its own loop policy at startup, which overrides anything set
beforehand. So on Windows we tell uvicorn not to manage the loop (`loop="none"`)
and run the server inside our own selector loop instead.

On Linux, where the container runs, this is plain `uvicorn.run`.

The usual entry point is `python main.py` at the repository root, which calls
`serve()` after migrating and seeding. This module stays runnable on its own:

    python -m src.api.run [--host H] [--port P] [--reload]
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import uvicorn

APP = "src.api.main:app"


def serve(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    if sys.platform == "win32":
        if reload:
            # Reload needs uvicorn's supervisor, which would reinstall the
            # proactor policy in the child process. Say so rather than starting
            # a server whose database calls will fail.
            print(
                "--reload is not supported on Windows because uvicorn's reloader "
                "reinstalls an event loop psycopg cannot use. Run without --reload, "
                "or use 'docker compose up' for a hot-reloading Linux environment.",
                file=sys.stderr,
            )
            raise SystemExit(2)

        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        config = uvicorn.Config(APP, host=host, port=port, loop="none")
        asyncio.run(uvicorn.Server(config).serve())
        return

    uvicorn.run(APP, host=host, port=port, reload=reload)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the API server only")
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    serve(args.host, args.port, args.reload)


if __name__ == "__main__":
    main()
