"""What the agent read, and when.

The first question anyone asks about connecting a coding agent to their cost
data is what it looked at. A token's `last_used_at` answers "is this still in
use"; this answers "what did it do".

One row per tool call, from either transport. It records the question — the tool
and its arguments — and never the answer: the arguments are ids, periods and
dimension names, while the results would be a second copy of the customer's cost
data with its own retention problem.

The insert goes through `mcp_audit_log()`, a SECURITY DEFINER function, because
the MCP server holds a role with no INSERT privilege anywhere and that is worth
more than the convenience of writing this table directly. A role that can write
its own audit trail is a role that can write.

**Auditing must never break a tool call.** A failed insert is logged and
swallowed: a customer whose question failed because the logging failed would
rightly regard that as the worse bug.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Optional

from ..db import app_dsn, connect, tenant_tx

logger = logging.getLogger("meter.mcp")

#: Matches the column's CHECK; truncation also happens in SQL, so a caller
#: cannot get past it by calling the function directly.
MAX_ARGUMENTS = 500

OK = "ok"
ERROR = "error"
RATE_LIMITED = "rate_limited"

#: How long an entry is kept. Fixed, not a per-tenant setting like trace
#: retention (0049): traces are bulky evidence whose value decays, while this is
#: an access log whose whole value is being able to answer a question about the
#: past. Making it configurable would mostly offer people a way to set it to a
#: day, which defeats the point of having it.
RETENTION_DAYS = 365

#: Deleted in batches, so a tenant with a year of history behind them does not
#: hold one long transaction open. Matches ai_reads.RETENTION_CHUNK.
RETENTION_CHUNK = 1000


def _arguments(arguments) -> Optional[str]:
    """The call's arguments as a short line of text, or None."""
    if not arguments:
        return None
    try:
        return json.dumps(arguments, default=str, sort_keys=True)[:MAX_ARGUMENTS]
    except (TypeError, ValueError):
        return "<unserializable>"


def record(
    tenant_id: str,
    token_id: Optional[str],
    *,
    transport: str,
    tool: str,
    arguments=None,
    outcome: str = OK,
    duration_ms: Optional[int] = None,
) -> None:
    """Log one tool call. Never raises."""
    try:
        with connect(app_dsn(), autocommit=True) as conn:
            conn.execute(
                "SELECT mcp_audit_log(%s, %s, %s, %s, %s, %s, %s)",
                (
                    tenant_id,
                    token_id,
                    transport,
                    tool[:100],
                    _arguments(arguments),
                    outcome,
                    duration_ms,
                ),
            )
    except Exception:  # noqa: BLE001 — see the module docstring
        logger.exception("could not record an MCP audit entry for %s", tool)


def recent(tenant_id: str, limit: int = 50) -> list:
    """The latest calls, newest first. For the Settings panel."""
    limit = max(1, min(int(limit), 200))
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        rows = conn.execute(
            """
            SELECT a.created_at, a.tool, a.arguments, a.outcome, a.duration_ms,
                   a.transport, t.label
            FROM mcp_audit a LEFT JOIN mcp_token t ON t.id = a.token_id
            ORDER BY a.created_at DESC LIMIT %s
            """,
            (limit,),
        ).fetchall()
    return [
        {
            "at": at.isoformat(),
            "tool": tool,
            "arguments": args,
            "outcome": outcome,
            "duration_ms": ms,
            "transport": transport,
            "token_label": label,
        }
        for at, tool, args, outcome, ms, transport, label in rows
    ]


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------
def purge_expired(tenant_id: str, now: Optional[dt.datetime] = None) -> dict:
    """Delete one tenant's audit entries past the retention window.

    Only the trail. The tokens themselves are untouched, revoked ones included:
    a token's own history — when it was made, when it was last used, when it was
    turned off — is not part of what expires here.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=RETENTION_DAYS)
    deleted = 0
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        while True:
            removed = conn.execute(
                """
                DELETE FROM mcp_audit WHERE id IN (
                    SELECT id FROM mcp_audit WHERE created_at < %s LIMIT %s
                ) RETURNING id
                """,
                (cutoff, RETENTION_CHUNK),
            ).fetchall()
            deleted += len(removed)
            if len(removed) < RETENTION_CHUNK:
                break
    return {"deleted": deleted, "cutoff": cutoff.isoformat()}


def purge_all_tenants(now: Optional[dt.datetime] = None) -> list:
    """Retention sweep across every tenant. Cron entry point."""
    from ..db import admin_dsn

    with connect(admin_dsn()) as conn:
        tenants = [str(r[0]) for r in conn.execute("SELECT id FROM tenant")]
    out = []
    for tenant_id in tenants:
        result = purge_expired(tenant_id, now)
        if result["deleted"]:
            out.append({"tenant_id": tenant_id, **result})
    return out


if __name__ == "__main__":
    swept = purge_all_tenants()
    print(f"Expired MCP audit entries for {len(swept)} tenants.")
    for entry in swept:
        print(" ", entry)
