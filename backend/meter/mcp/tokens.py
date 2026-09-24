"""The MCP server's credential: create, list, revoke, resolve.

Modelled on `hook.generate_token` (0005), with the differences a read credential
needs. An ingest token is one per tenant and rotating it is a deploy; an MCP
token is per machine — a laptop, a CI job — so there are several, they carry a
label, and revoking one must not disturb the others.

Only the SHA-256 hash is stored. The token is returned once, at creation, and
cannot be recovered afterwards: a credential a support engineer can read back is
a credential that leaks through a support conversation.

Resolution goes through `mcp_resolve_token()`, a SECURITY DEFINER function, so a
process holding only the SELECT-only role can turn ITS token into ITS tenant
without being able to read the token table at all.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Optional

from ..db import app_dsn, connect, tenant_tx

#: Long enough that guessing is not a strategy, and URL-safe so it survives an
#: env var, a JSON config and a shell.
TOKEN_BYTES = 32

#: A prefix, so a leaked string is recognisably a Meter MCP token — to a
#: secret scanner, and to whoever finds it in a config file.
TOKEN_PREFIX = "mtr_mcp_"

MAX_LABEL = 100


class TokenError(Exception):
    """Something the caller can fix: an empty label, an unknown token id."""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create(tenant_id: str, label: str) -> dict:
    """Mint a token. The only time its plaintext exists."""
    label = (label or "").strip()
    if not label:
        raise TokenError("A token needs a label — name the machine it is for.")
    if len(label) > MAX_LABEL:
        raise TokenError(f"Label must be at most {MAX_LABEL} characters.")
    token = TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        row = conn.execute(
            "INSERT INTO mcp_token (tenant_id, label, token_hash) "
            "VALUES (%s, %s, %s) RETURNING id, created_at",
            (tenant_id, label, _hash(token)),
        ).fetchone()
    return {
        "id": str(row[0]),
        "label": label,
        "created_at": row[1].isoformat(),
        # Returned once. Nothing stores it, and nothing can read it back.
        "token": token,
    }


def list_tokens(tenant_id: str) -> list:
    """Every token this tenant has, revoked ones included — the history is the
    point of keeping them."""
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        rows = conn.execute(
            "SELECT id, label, created_at, last_used_at, revoked_at "
            "FROM mcp_token ORDER BY created_at DESC"
        ).fetchall()
    return [
        {
            "id": str(tid),
            "label": label,
            "created_at": created.isoformat(),
            "last_used_at": used.isoformat() if used else None,
            "revoked_at": revoked.isoformat() if revoked else None,
            "active": revoked is None,
        }
        for tid, label, created, used, revoked in rows
    ]


def revoke(tenant_id: str, token_id: str) -> dict:
    """Turn one token off. Idempotent: revoking a revoked token keeps the
    original timestamp, because when it stopped working is the fact worth
    keeping."""
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        row = conn.execute(
            "UPDATE mcp_token SET revoked_at = COALESCE(revoked_at, now()) "
            "WHERE id = %s RETURNING id, label, revoked_at",
            (token_id,),
        ).fetchone()
    if row is None:
        raise TokenError("No such token.")
    return {"id": str(row[0]), "label": row[1], "revoked_at": row[2].isoformat()}


def resolve(token: str) -> Optional[str]:
    """The tenant this token belongs to, or None. Records the use.

    Runs on whatever role the process has — including the SELECT-only one —
    because the lookup is a SECURITY DEFINER function rather than a query
    against the table.
    """
    if not token:
        return None
    with connect(app_dsn(), autocommit=True) as conn:
        row = conn.execute("SELECT mcp_resolve_token(%s)", (_hash(token),)).fetchone()
    return str(row[0]) if row and row[0] else None
