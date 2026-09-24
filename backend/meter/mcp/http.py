"""The hosted MCP endpoint: MCP's Streamable HTTP transport, statelessly.

A customer on a hosted Meter has no database credentials and must never have
any, so the stdio server — which connects to Postgres itself — can only ever
serve people running their own instance. This is the same server reached over
HTTP, running inside the API process that already has a database connection.

**Stateless.** The spec allows a server to assign an `Mcp-Session-Id` at
initialization and require it afterwards. This does not, because there is
nothing to remember between calls: every tool call is a fresh read, authorized
by the token on the request. That makes the endpoint safe to run behind a load
balancer, safe to redeploy mid-conversation, and impossible to exhaust by
opening sessions.

**One JSON response per request, never SSE.** The spec lets the server answer a
JSON-RPC request with either `text/event-stream` or `application/json`; these
tools compute an answer in milliseconds and have nothing to stream, so a stream
would be ceremony. `GET` therefore returns 405, as the spec provides for a
server that offers no server-initiated stream.

**Bearer token only — never the session cookie.** Authentication is in api.py,
but the rule belongs here: if this endpoint honoured the browser session, any
web page could drive it with the user's ambient credentials. Requiring a header
no cross-origin form can set is what makes that impossible.

Everything here is pure: headers and a body in, a status and a body out. The
FastAPI glue in api.py does the credential lookup and turns the result into a
Response, so the protocol can be tested without a client or a server.
"""

from __future__ import annotations

from typing import Any, NamedTuple, Optional

from .server import Caller, handle

#: Protocol revisions this endpoint implements, newest first.
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

#: What to assume when the client sends no MCP-Protocol-Version header. The spec
#: names this exact version for backwards compatibility — clients predating the
#: header do not send one.
ASSUMED_PROTOCOL = "2025-03-26"

PROTOCOL_HEADER = "mcp-protocol-version"


class Reply(NamedTuple):
    """An HTTP response, before FastAPI gets hold of it."""

    status: int
    #: None for a bodyless reply (202 Accepted, 405 Method Not Allowed).
    body: Optional[dict] = None


def allowed_origins(app_origin: Optional[str]) -> set:
    """Browser origins this endpoint will answer.

    The spec requires validating `Origin` to prevent DNS rebinding. A real MCP
    client sends no Origin at all — it is not a browser — so the practical
    effect is to refuse a page that tries to drive the endpoint from a tab. The
    app's own origin is allowed because that is where a future in-product client
    would live.
    """
    return {app_origin} if app_origin else set()


def check_origin(origin: Optional[str], app_origin: Optional[str]) -> Optional[Reply]:
    """None if the request may proceed, or the refusal."""
    if origin is None:  # not a browser; the ordinary case
        return None
    if origin in allowed_origins(app_origin):
        return None
    return Reply(403, {"error": "Origin not allowed."})


def check_protocol(version: Optional[str]) -> Optional[Reply]:
    """None if the request may proceed, or the refusal.

    An unsupported version is 400, as the spec requires. Absent is not
    unsupported — it means an older client, and gets the version the spec says
    to assume.
    """
    if version is None or version in SUPPORTED_PROTOCOLS:
        return None
    return Reply(
        400,
        {
            "error": f"Unsupported MCP-Protocol-Version {version!r}.",
            "supported": list(SUPPORTED_PROTOCOLS),
        },
    )


def post(body: Any, caller: Caller, *, origin=None, protocol=None, app_origin=None) -> Reply:
    """Handle one POSTed JSON-RPC message."""
    refusal = check_origin(origin, app_origin) or check_protocol(protocol)
    if refusal is not None:
        return refusal
    if not isinstance(body, dict):
        # Batching was removed in 2025-06-18, and this server never supported
        # it: a list here is a client bug, and saying so beats a 500.
        return Reply(400, {"error": "Expected a single JSON-RPC object."})

    response = handle(body, caller)
    if response is None:
        # A notification or a response. The spec: 202 Accepted, no body.
        return Reply(202)
    return Reply(200, response)


def get() -> Reply:
    """The spec's alternative to an SSE stream, for a server that has none."""
    return Reply(405, {"error": "This endpoint does not offer a server-initiated stream."})


def delete() -> Reply:
    """Session termination, for a server with no sessions to terminate."""
    return Reply(405, {"error": "This endpoint is stateless and has no session to end."})
