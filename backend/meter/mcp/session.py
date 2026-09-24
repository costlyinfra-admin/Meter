"""Who the stdio MCP server is acting as.

One process serves one tenant, resolved once at startup from a token. That is
deliberate: the server runs on a developer's machine next to their editor, and
the alternative — a process that can reach several tenants and picks one per
call — would put tenant selection in a tool argument, where a confused model
could change it.

**The credential is an MCP token, not a person's login.** It is minted from the
Meter account it belongs to (`python -m meter.mcp.mint`), scoped to one tenant,
revocable on its own without disturbing anything else, and useless for signing
in to Meter. Nothing about it can be used to write.

**The connection is read-only.** The process runs as `meter_read` (migration
0060), which holds SELECT and nothing else, so "this server cannot write" stops
depending on nobody adding the wrong tool.
"""

from __future__ import annotations

import os

from . import tokens
from .server import Caller

#: Where the server reads its credential from.
TOKEN_VAR = "METER_MCP_TOKEN"


class NotAuthenticated(Exception):
    """The server has no usable Meter identity, so it must not start."""


def resolve_caller() -> Caller:
    """Who this process serves, or raise.

    Deliberately fails at startup rather than on the first tool call: a client
    that connects successfully and then fails every query looks like a Meter
    outage, when it is a missing environment variable.
    """
    token = os.environ.get(TOKEN_VAR, "").strip()
    if not token:
        raise NotAuthenticated(
            f"Set {TOKEN_VAR} to a Meter MCP token. Mint one with "
            "`python -m meter.mcp.mint`."
        )
    found = tokens.resolve(token)
    if found is None:
        # No detail about why: this message can reach a log, and "expired"
        # versus "never existed" is information a guesser can use.
        raise NotAuthenticated("That MCP token is not valid. It may have been revoked.")
    return Caller(found.tenant_id, found.token_id, "stdio")
