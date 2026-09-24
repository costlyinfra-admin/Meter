"""`python -m meter.mcp` — the stdio entry point a client spawns.

Logging goes to stderr because stdout is the protocol stream (see server.py).
"""

from __future__ import annotations

import logging
import os
import sys

from .server import serve
from .session import NotAuthenticated, resolve_tenant


def main() -> int:
    logging.basicConfig(
        stream=sys.stderr,
        level=os.environ.get("METER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        tenant_id = resolve_tenant()
    except NotAuthenticated as exc:
        print(f"meter-mcp: {exc}", file=sys.stderr)
        return 2
    except KeyError as exc:  # a missing DATABASE_URL surfaces as one
        print(f"meter-mcp: {exc} is not set. See docs/mcp-server.md.", file=sys.stderr)
        return 2
    serve(tenant_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
