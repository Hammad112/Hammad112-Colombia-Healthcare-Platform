"""Run the API under uvicorn.

psycopg's async mode cannot run on a ProactorEventLoop, which is what uvicorn's
default loop setting creates on Windows. Passing `asyncio:SelectorEventLoop` as
the loop makes uvicorn use a selector loop on every platform, including in the
child process started by `--reload`. On Linux and macOS the default loop is
already a selector loop, so behaviour there does not change.

While the review API is enabled there is no authentication in front of it, so
`serve` refuses to bind anywhere but the loopback interface. That is a refusal
rather than a warning because a warning is exactly what gets missed.
"""

from __future__ import annotations

import ipaddress

import uvicorn

from src.core.config import get_settings

APP_FACTORY = "src.api.app:create_app"
# uvicorn imports a custom loop setting as a loop factory (uvicorn >= 0.36).
LOOP_FACTORY = "asyncio:SelectorEventLoop"


class UnsafeBindError(RuntimeError):
    """Raised when the requested bind address would expose unauthenticated data."""


def _is_loopback(host: str) -> bool:
    """True for an address reachable only from this machine.

    A hostname that is not an IP literal cannot be resolved to a single
    interface here, so only "localhost" is accepted by name.
    """
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_bind_address(host: str) -> None:
    """Refuse a bind address that would publish the review API to a network.

    The review API serves whole patient records with no login in front of them.
    It exists only in synthetic-data mode, where the records are synthetic, but
    the same process would serve real data the moment that mode is turned off,
    so the address is constrained while the routes exist at all.
    """
    if get_settings().synthetic_data_mode and not _is_loopback(host):
        raise UnsafeBindError(
            f"Refusing to bind {host}: the review API is enabled and has no authentication, "
            "so it may be served on the loopback interface only. Use --host 127.0.0.1, or "
            "set ALLOW_REAL_PATIENT_DATA=true or APP_ENV=staging to disable the review API."
        )


def serve(host: str = "127.0.0.1", port: int = 8000, *, reload: bool = False) -> None:
    check_bind_address(host)
    uvicorn.run(APP_FACTORY, factory=True, host=host, port=port, reload=reload, loop=LOOP_FACTORY)
