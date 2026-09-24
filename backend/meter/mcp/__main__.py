"""`python -m meter.mcp` — the stdio entry point a client spawns.

Two things happen before a single message is read: the process declares itself
read-only, so every connection it makes afterwards is a SELECT-only one, and it
resolves its token into a tenant. Both fail loudly here rather than quietly on
the first tool call.

Logging goes to stderr because stdout is the protocol stream (see server.py).
"""

from __future__ import annotations

import logging
import os
import sys

from ..db import read_only
from .server import serve
from .session import NotAuthenticated, resolve_tenant


def main() -> int:
    logging.basicConfig(
        stream=sys.stderr,
        level=os.environ.get("METER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Before anything opens a connection. A server that cannot write is worth
    # more than a server that merely does not.
    read_only()
    try:
        tenant_id = resolve_tenant()
    except NotAuthenticated as exc:
        print(f"meter-mcp: {exc}", file=sys.stderr)
        return 2
    except (KeyError, RuntimeError) as exc:
        # A missing DATABASE_URL surfaces as KeyError; a missing read-role
        # credential as RuntimeError from db.read_dsn().
        print(f"meter-mcp: {exc}", file=sys.stderr)
        return 2
    serve(tenant_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
