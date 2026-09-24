"""How fast one organization may ask.

Lives apart from session.py because it outlived it: the stdio server serves one
tenant for the life of a process, the HTTP endpoint serves many within one, and
the limit is per organization in both. Keying it on the tenant means a busy
customer cannot slow down anyone else, on either transport.

Generous for a person working with an agent — a question usually costs two or
three calls — and low enough that a client stuck in a retry loop stops being the
database's problem. Bigger than the assistant's budget, because a tool call is
cheaper than a model call.
"""

from __future__ import annotations

from .. import ratelimit

RATE_LIMIT = 120
RATE_WINDOW = 60.0

_limiter = ratelimit.Limiter(
    RATE_LIMIT,
    RATE_WINDOW,
    f"Too many Meter tool calls in the last minute (limit {RATE_LIMIT}). "
    "Wait a moment before retrying.",
)


def check_rate(tenant_id: str) -> None:
    """Raises ratelimit.RateLimited when a client is asking too fast."""
    _limiter.check(tenant_id)


def reset_rate() -> None:
    """Forget the window. For tests."""
    _limiter.reset()
