"""Run the API under uvicorn.

psycopg's async mode cannot run on a ProactorEventLoop, which is what uvicorn's
default loop setting creates on Windows. Passing `asyncio:SelectorEventLoop` as
the loop makes uvicorn use a selector loop on every platform, including in the
child process started by `--reload`. On Linux and macOS the default loop is
already a selector loop, so behaviour there does not change.
"""

from __future__ import annotations

import uvicorn

APP_FACTORY = "src.api.app:create_app"
# uvicorn imports a custom loop setting as a loop factory (uvicorn >= 0.36).
LOOP_FACTORY = "asyncio:SelectorEventLoop"


def serve(host: str = "127.0.0.1", port: int = 8000, *, reload: bool = False) -> None:
    uvicorn.run(APP_FACTORY, factory=True, host=host, port=port, reload=reload, loop=LOOP_FACTORY)
