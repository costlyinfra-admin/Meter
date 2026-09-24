"""A read-only MCP server over Meter's existing read services.

Model Context Protocol lets a coding agent — Claude Code, say — ask Meter what a
feature costs and what Meter has already found wrong with it, while it is sitting
in the repository that produced the spend. That is the one place the answer is
directly actionable.

**This package adds no analytics.** Every number it returns comes from the same
functions the web app's endpoints call: `dashboard`, `optimize_measured` and
`ai_reads`. It is a second transport for one read layer, not a second read layer
— `api.py` maps HTTP to those functions, this maps MCP tool calls to them, and
if the two ever disagreed about a number, one of them would be wrong.

**Read-only twice over.** There is no tool that applies an optimization, edits
a feature, changes settings or triggers a sync; the surface is three read
functions and test_mcp.py asserts the whole of it. And the process runs as
`meter_read` (migration 0060), a role holding SELECT and nothing else — so the
guarantee does not rest on nobody adding the wrong tool later.

Layout:
  tokens.py  — the credential: create, list, revoke, resolve
  mint.py    — `python -m meter.mcp.mint`, which issues one
  audit.py   — what was asked, and when
  limits.py  — how fast one organization may ask
  tools.py   — the three tools: their schemas and their handlers
  server.py  — protocol handling, transport-agnostic
  session.py — who the stdio server acts as (one tenant, resolved once)
  http.py    — the hosted endpoint, for customers with no database access
  __main__.py— `python -m meter.mcp`
"""

from .server import Caller, handle, serve
from .session import NotAuthenticated, resolve_caller
from .tools import TOOLS, ToolError, call_tool

__all__ = [
    "TOOLS",
    "Caller",
    "NotAuthenticated",
    "ToolError",
    "call_tool",
    "handle",
    "resolve_caller",
    "serve",
]
