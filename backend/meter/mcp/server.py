"""The MCP stdio transport: newline-delimited JSON-RPC 2.0 on stdin/stdout.

Hand-rolled rather than taken from the official `mcp` package, for one reason:
that package requires Python 3.10 and this backend supports 3.9 (see
pyproject.toml, and the venv a contributor gets from `make install`). A
tools-only server is a small, closed protocol surface — initialize, list, call —
and the value of this feature is in the three tools, not in the envelope around
them. If the backend's floor moves to 3.10, swapping this file for the SDK's
server would leave tools.py untouched.

**stdout carries the protocol and nothing else.** A stray print here corrupts
the stream and the client disconnects with an unhelpful parse error, so logging
goes to stderr — which is where a client shows a server's diagnostics anyway.

Errors are split the way MCP intends. A malformed request is a JSON-RPC error; a
tool that cannot answer — an unknown feature id, a bad period — returns a normal
result with `isError` set, because that text goes to the model, which can read it
and try again. A protocol error just looks like a broken server.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import IO, Any, NamedTuple, Optional

from .. import __version__
from ..ratelimit import RateLimited
from . import audit
from .limits import check_rate
from .tools import TOOLS, ToolError, call_tool

logger = logging.getLogger("meter.mcp")

SERVER_NAME = "meter"

#: Where the product's own handbook is published. Named in `initialize` so an
#: agent that needs a definition — what "modeled_ceiling" means, what lands in
#: Unattributed — can read the same pages a customer reads, instead of inferring
#: one from a field name. Cheaper and more honest than serving the handbook as
#: MCP resources: it is one URL rather than a copy of the content inside this
#: process, and it cannot fall out of step with what the app ships.
DOCS_URL = "https://costlyinfra.com/docs"

#: Protocol revisions this server implements. The client names one in
#: `initialize`; we echo it when we know it, and otherwise answer with our own
#: newest and let the client decide whether it can proceed.
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL = SUPPORTED_PROTOCOLS[0]

# JSON-RPC error codes (the subset a server like this can raise).
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603


class Caller(NamedTuple):
    """Who is asking.

    Always derived from a credential — a token, or the session behind one — and
    never from anything in a message. A tenant id that a client could put in a
    request body would be a tenant id a confused model could change.
    """

    tenant_id: str
    #: Which token, for the audit trail. None where there is no token to name.
    token_id: Optional[str] = None
    transport: str = "stdio"


def _result(request_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _tool_result(payload: dict, *, is_error: bool = False) -> dict:
    """A tool response. JSON as text: every MCP client renders text content, and
    the model reads JSON perfectly well."""
    return {
        "content": [{"type": "text", "text": json.dumps(payload, indent=2, default=str)}],
        "isError": is_error,
    }


def handle(message: dict, caller: Caller) -> Optional[dict]:
    """One request in, one response out — or None for a notification."""
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}
    is_notification = "id" not in message

    if not isinstance(method, str):
        return None if is_notification else _error(request_id, INVALID_REQUEST, "Missing method.")

    if method == "initialize":
        asked = params.get("protocolVersion")
        return _result(
            request_id,
            {
                "protocolVersion": asked if asked in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
                "instructions": (
                    "Read-only access to this organization's Meter data: AI cost by "
                    "feature, provider, model, workspace, product, customer and "
                    "application, and the optimization opportunities Meter has "
                    "detected. Build cost and inference cost are never summed. Check "
                    "an opportunity's savings_type and validation_guidance before "
                    "acting on it. How Meter works, and what every number means, is "
                    f"documented at {DOCS_URL} — fetch it rather than guessing at a "
                    "definition."
                ),
            },
        )

    # Notifications carry no id and must produce no reply, acknowledged or not.
    if is_notification:
        if method not in ("notifications/initialized", "notifications/cancelled"):
            logger.debug("ignoring notification %s", method)
        return None

    if method == "ping":
        return _result(request_id, {})

    if method == "tools/list":
        return _result(request_id, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str):
            return _error(request_id, INVALID_REQUEST, "tools/call needs a tool name.")
        arguments = params.get("arguments")
        started = time.perf_counter()

        def done(outcome: str) -> None:
            audit.record(
                caller.tenant_id,
                caller.token_id,
                transport=caller.transport,
                tool=name,
                arguments=arguments,
                outcome=outcome,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        try:
            # Before the work, not after: the point is to stop a client in a
            # retry loop from reaching the database at all.
            check_rate(caller.tenant_id)
            payload = call_tool(name, arguments, caller.tenant_id)
        except RateLimited as exc:
            # A tool result, not a protocol error — the model should read this
            # and wait, rather than see a broken server and reconnect.
            done(audit.RATE_LIMITED)
            return _result(request_id, _tool_result({"error": str(exc)}, is_error=True))
        except ToolError as exc:
            # The model's problem to fix, so it is told rather than the transport.
            done(audit.ERROR)
            return _result(request_id, _tool_result({"error": str(exc)}, is_error=True))
        except Exception:
            # Never leak an internal message — it can carry a DSN or a query.
            logger.exception("tool %s failed", name)
            done(audit.ERROR)
            return _result(
                request_id,
                _tool_result({"error": f"{name} failed. See the server log."}, is_error=True),
            )
        done(audit.OK)
        return _result(request_id, _tool_result(payload))

    return _error(request_id, METHOD_NOT_FOUND, f"Unknown method {method!r}.")


def serve(caller: Caller, stdin: IO[str] = None, stdout: IO[str] = None) -> None:
    """Read messages until the client closes the stream."""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    logger.info("meter MCP server ready (read-only)")
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            _write(stdout, _error(None, PARSE_ERROR, "Invalid JSON."))
            continue
        if not isinstance(message, dict):
            _write(stdout, _error(None, INVALID_REQUEST, "Expected a JSON-RPC object."))
            continue
        try:
            response = handle(message, caller)
        except Exception:
            logger.exception("error handling %s", message.get("method"))
            response = _error(message.get("id"), INTERNAL_ERROR, "Internal error.")
        if response is not None:
            _write(stdout, response)


def _write(stdout: IO[str], message: dict) -> None:
    # One message per line, and no embedded newlines: that is the framing.
    stdout.write(json.dumps(message) + "\n")
    stdout.flush()
