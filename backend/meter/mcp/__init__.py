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

**Read-only.** There is no tool that applies an optimization, edits a feature,
changes settings or triggers a sync. The surface is three read functions, and
test_mcp.py asserts the whole of it, so a fourth tool cannot arrive quietly.

Layout:
  session.py — who the server acts as (one tenant, resolved once)
  tools.py   — the three tools: their schemas and their handlers
  server.py  — the stdio JSON-RPC loop
  __main__.py— `python -m meter.mcp`
"""

from .server import serve
from .session import NotAuthenticated, resolve_tenant
from .tools import TOOLS, ToolError, call_tool

__all__ = ["TOOLS", "NotAuthenticated", "ToolError", "call_tool", "resolve_tenant", "serve"]
