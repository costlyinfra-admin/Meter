"""A per-tenant sliding window, in process.

Abuse dampening, not a quota. It exists so one tenant looping a client cannot
monopolise the database or the model budget; it is not a billing mechanism and
does not try to be exact. In-process by design: there is no shared counter, so
on several instances each enforces its own window, which is the right trade for
something whose job is to stop runaway loops rather than to meter usage.

Used by the assistant (a person asking questions) and by the MCP server (an
agent asking them), with different budgets — see each caller.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Optional


class RateLimited(Exception):
    """The window is full. Callers map this to HTTP 429, or to a tool error."""


class Limiter:
    """`limit` events per `window` seconds, per key."""

    def __init__(self, limit: int, window: float, message: str):
        self.limit = limit
        self.window = window
        self.message = message
        self._seen: dict = defaultdict(deque)

    def check(self, key: str, *, now: Optional[float] = None) -> None:
        """Record one event, or raise RateLimited.

        `now` is injectable so a test can drive the window without sleeping —
        the clock must be monotonic, not wall time, or a clock change would
        either lock a tenant out or reset everyone's window.
        """
        now = time.monotonic() if now is None else now
        seen = self._seen[key]
        while seen and now - seen[0] > self.window:
            seen.popleft()
        if len(seen) >= self.limit:
            raise RateLimited(self.message)
        seen.append(now)

    def reset(self, key: Optional[str] = None) -> None:
        """Forget one key's history, or all of it. For tests."""
        if key is None:
            self._seen.clear()
        else:
            self._seen.pop(key, None)
