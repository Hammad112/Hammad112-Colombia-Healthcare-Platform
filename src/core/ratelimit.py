"""Rate-limit backends.

`RateLimiter` is the seam the API depends on. `InProcessRateLimiter` is the only
implementation today; a Redis-backed one arrives with the second API worker,
because counters held in a process are per process and N workers therefore admit
up to N times the configured limit.

A backend answers one question: may this key make a request now, and if not, how
long until it may. Everything else — which header carries the wait, which paths
are exempt — belongs to the middleware.
"""

from __future__ import annotations

import math
import time
from collections import deque
from typing import Protocol


class RateLimiter(Protocol):
    """Decides whether a key may proceed, over a sliding window."""

    async def check(self, key: str) -> int | None:
        """Count a request and allow it, returning None.

        When the key is over its limit the request is not counted and the whole
        seconds until it may retry are returned instead, always at least 1.
        """
        ...


class InProcessRateLimiter:
    """A sliding window held in this process's memory.

    Correct for a single worker and nothing else: two workers keep two windows,
    and a restart forgets every window. Sized for one API process serving a
    clinic's staff, which is what M0 runs.
    """

    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._last_sweep = time.monotonic()

    async def check(self, key: str) -> int | None:
        now = time.monotonic()
        self._sweep_idle_keys(now)

        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= self.window_seconds:
            hits.popleft()

        if len(hits) >= self.limit:
            # The window frees a slot when its oldest hit ages out.
            return max(1, math.ceil(self.window_seconds - (now - hits[0])))

        hits.append(now)
        return None

    def _sweep_idle_keys(self, now: float) -> None:
        """Drop keys with no requests in the window, at most once per window.

        Without this the dictionary would grow with every distinct client seen.
        """
        if now - self._last_sweep < self.window_seconds:
            return
        self._last_sweep = now
        for key in [
            key
            for key, hits in self._hits.items()
            if not hits or now - hits[-1] >= self.window_seconds
        ]:
            del self._hits[key]
