# Meter's MCP server

A read-only [Model Context Protocol](https://modelcontextprotocol.io) server that
lets a coding agent — Claude Code, say — ask Meter what a feature costs and what
Meter has already found wrong with it, from inside the repository that produced
the spend.

It adds no analytics. Every number comes from the same functions the web app's
endpoints call, so a figure here and a figure on a Meter screen cannot disagree.

This file is the engineering account of it. The customer-facing version lives in
the handbook, under **Privacy & trust → Coding agents (MCP)**, which also
publishes to costlyinfra.com/docs.

Two ways to reach it, one implementation behind both:

```
hosted    Claude Code ──HTTPS──▶ /api/mcp ─┐
                        bearer token        │
                                            ├─▶ tools ─▶ dashboard / optimize_measured / ai_reads
local     Claude Code ──stdio──▶ meter.mcp ─┘             (the same services api.py calls)
                        METER_MCP_TOKEN                              │
                                                                     ▼
                                                        Postgres as meter_read
                                                        SELECT only, RLS per tenant
```

**Use the hosted endpoint.** It needs nothing installed and no database
credentials, which is the only option for a customer who does not run Meter
themselves. The stdio server exists for people working on Meter itself, and for
self-hosted installs that would rather not expose an endpoint at all.

## The three tools

| Tool | Answers | Reuses |
| --- | --- | --- |
| `get_cost_summary` | What did we spend, on what, over which months | `dashboard.dashboard`, `spend_by_provider`, `spend_by_product`, `spend_by_customer`, `feature_detail`, `ai_reads.list_applications` |
| `find_optimization_opportunities` | What has Meter already detected, ranked by priority | `optimize_measured.copilot_overview`, `optimize_measured.opportunities` |
| `get_optimization_details` | Why does it think that, and where is the code | `optimize_measured.opportunities`, `dashboard.feature_inference`, `ai_reads.list_traces` |

`get_cost_summary` slices with `group_by`: `feature` (default), `provider`,
`model`, `workspace`, `product`, `customer`, `application` — the dimensions
Meter's read layer already has. Windows are months: `range` (`this_month`,
`last_month`, `last_3_months`, `last_6_months`, `last_12_months`) or explicit
`start`/`end` as `YYYY-MM`, matching the app's own period selector.

`find_optimization_opportunities` answers two different questions. With no
arguments it returns the organization-wide shortlist Meter ranks by priority —
the same one the Optimize screen shows, and deliberately short. With a
`feature_id` it returns *every* opportunity on that feature, including
directional estimates and findings superseded by a better-evidenced one (those
carry `overlaps`, and must never be added to the finding that supersedes them).

It returns an `opportunity_id` of the form
`<feature_id>:<lever>:<YYYY-MM-DD>`. Opportunities are computed on read rather
than stored, so that address — which feature, which lever, which month — is what
identifies one. Hand it to `get_optimization_details` to get the finding back
exactly as it was, rather than whatever that lever says today.

## What it will not do

- **It cannot write.** Twice over. The surface is three read functions and a
  test asserts the whole of it — but more importantly the process connects as
  `meter_read` (migration 0060), a database role holding `SELECT` and nothing
  else. "This server does not write" is a claim about the code; "this server
  cannot write" is a property of the connection, and that is the one worth
  having.
- **It cannot read a credential.** `meter_read` has no privilege at all on
  `app_user`, `hook_token` or `mcp_token`, and on `connector_credential` it has
  a column grant covering everything except the encrypted secret — enough to
  answer "is a GitHub connector configured", not enough to read it.
- **It cannot cross tenants.** The tenant comes from the credential, never from
  a message — over stdio it is fixed at startup, over HTTP it is resolved per
  request, and in neither case can a client name it. `meter_read` is a
  non-owner, so every RLS policy governs it exactly as it governs the app; with
  no tenant set it sees nothing, never everything. Passing another
  organization's feature id returns "no such feature".
- **It cannot be hammered.** 120 tool calls per minute per organization, checked
  before the work rather than after, so a client stuck in a retry loop never
  reaches the database. Over the line comes back as a tool result telling the
  model to wait, not as a server error that would prompt a reconnect.
- **It cannot be driven from a web page.** The hosted endpoint authenticates
  with a bearer token and is deliberately blind to the browser session — if it
  honoured the session cookie, any page a signed-in user visited could read
  their organization's costs. An `Origin` header that is not the app's own is
  refused outright, which is the DNS-rebinding protection the MCP specification
  requires.
- **It cannot act unobserved.** Every tool call is recorded against the token
  that made it: the tool, its arguments, the outcome and how long it took. The
  trail keeps the question and never the answer — results would be a second copy
  of your cost data with its own retention story. Entries are kept for a year,
  swept nightly; unlike trace retention this is fixed rather than per-tenant,
  because an access log whose value is answering questions about the past should
  not be configurable down to a day.
- **It returns no content.** Meter stores no prompts, responses, tool arguments
  or retrieved documents, so there is nothing to leak. Repeated-request evidence
  is a salted one-way fingerprint and a count. Example runs carry the operation
  name, model and cost — the things that locate a call site — and deliberately
  not the customer reference.

## Connecting to a hosted Meter

Open **Settings → Coding agents**, create a token, and paste the command it
shows you:

```bash
claude mcp add --transport http meter https://meter.costlyinfra.com/api/mcp \
  --scope user --header "Authorization: Bearer mtr_mcp_…"
```

`--scope user` keeps the credential out of any repository. The token is shown
once — Meter stores only a hash — so if you lose it, revoke it and make another.

That screen is also where you see what the agent has been doing: every tool call
is listed with what it asked for, and every token shows when it was last used and
how many calls it made this week. Revoking one takes effect immediately and does
not disturb the others.

### What a deployment needs

| Variable | Why |
| --- | --- |
| `METER_READ_DB_PASSWORD` | The `meter_read` role's password. **`/api/mcp` answers 503 without it** — it narrows every call to that role rather than falling back to one that can write. |
| `METER_APP_ORIGIN` | The only browser `Origin` the endpoint will answer. MCP clients are not browsers and send none. |

## Setting it up locally, over stdio

For working on Meter itself, or a self-hosted install. You need a Python
environment with the backend installed (`make install` gives you one at
`backend/.venv`) and a database URL for the Meter instance you want to read —
your local `make demo` database, or your deployed one.

### 1. Give the read-only role a password

The role exists after migrations but cannot log in until it has one. Locally,
where Postgres trusts you, this is enough:

```bash
psql "$DATABASE_URL" -c "ALTER ROLE meter_read WITH LOGIN PASSWORD 'choose-one'"
```

In production, set `METER_READ_DB_PASSWORD` and redeploy —
[`deploy/release.sh`](../deploy/release.sh) applies it on every release.

### 2. Mint a token

Settings → Coding agents does this from the app. From a shell, where there is no
browser:

```bash
cd backend && .venv/bin/python -m meter.mcp.mint create "Office laptop"
```

It asks for your Meter password at the prompt, authenticates you the ordinary
way, and prints a token **once**. Meter stores only its SHA-256 hash, so it
cannot be read back — if you lose it, mint another and revoke the old one.

`list` shows every token with when it was last used; `revoke <id>` turns one off
without touching the others.

### 3. Point Claude Code at it

```bash
claude mcp add meter \
  --scope user \
  --env DATABASE_URL="postgresql://…" \
  --env METER_READ_DB_PASSWORD="choose-one" \
  --env METER_MCP_TOKEN="mtr_mcp_…" \
  --env PYTHONPATH="/absolute/path/to/Meter/backend" \
  -- /absolute/path/to/Meter/backend/.venv/bin/python -m meter.mcp
```

`--scope user` keeps it out of any repository, which is what you want for a
credential. The interpreter path must be absolute: the agent spawns the server
from whatever directory it happens to be in, which is rarely this one.

`PYTHONPATH` is belt and braces. `make install` puts `meter` on that venv's path
already, but an editable install records an absolute path and goes stale if the
checkout is ever moved or renamed — at which point `meter` imports from
`backend/` and nowhere else, and the server fails to start with
`No module named 'meter'`. Naming the directory explicitly means it does not
matter. (If you hit that anyway, `backend/.venv/bin/python -m pip install -e
backend` repoints it.)

Then, in Claude Code:

```
> /mcp
> What did we spend on AI last month, by feature?
> What's the biggest optimization opportunity, and show me the evidence for it.
```

### Environment

| Variable | Required | What it is |
| --- | --- | --- |
| `METER_MCP_TOKEN` | yes | The token from step 2. Scoped to one organization, revocable, useless for signing in. |
| `DATABASE_URL` | yes | The Meter database. |
| `METER_READ_DB_PASSWORD` | yes | The `meter_read` password from step 1. `DATABASE_READ_URL` replaces both if you prefer a full connection string. |
| `PYTHONPATH` | no | `…/Meter/backend`. Not needed when the editable install is healthy; see above. |
| `METER_LOG_LEVEL` | no | Defaults to `INFO`. Logs go to stderr — stdout carries the protocol. |

There is no password in either setup. Earlier versions took `METER_EMAIL` and
`METER_PASSWORD`, which meant a full Meter login sat in an agent's config with
no way to revoke it short of changing the password. The token replaces it: one
per machine, revocable on its own, and it records when it was last used so you
can tell whether anything still depends on it before turning it off.

If the server cannot get a read-only connection it **exits** rather than falling
back to a role that can write.

## Checking it works

Without a client — it is line-delimited JSON-RPC on stdin, so a shell will do:

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_cost_summary","arguments":{"range":"last_month"}}}' \
  | backend/.venv/bin/python -m meter.mcp
```

Three JSON lines back means the server, the credentials and the database are all
working. A missing variable exits 2 with a message on stderr rather than starting
and failing every call.

## Adding a tool later

`tools.py` is a list of tool definitions and a dict of handlers; `server.py` is
the transport and knows nothing about Meter. A new tool is an entry in `TOOLS`, a
handler that calls an existing service, and a test. Keep the read-only property:
`test_the_server_offers_nothing_that_writes` asserts the whole surface, so a
fourth tool has to be added deliberately — and if it reads a table nobody has
granted `meter_read`, `test_every_tool_runs_as_the_read_only_role` fails with
the table's name. Add the grant to a new migration rather than widening the
role.

The transport is hand-rolled because the official `mcp` package needs Python
3.10 and this backend supports 3.9. If that floor moves, `server.py` can be
replaced by the SDK's server and `tools.py` does not change.
