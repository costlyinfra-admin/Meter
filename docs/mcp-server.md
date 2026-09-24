# Meter's MCP server

A read-only [Model Context Protocol](https://modelcontextprotocol.io) server that
lets a coding agent — Claude Code, say — ask Meter what a feature costs and what
Meter has already found wrong with it, from inside the repository that produced
the spend.

It adds no analytics. Every number comes from the same functions the web app's
endpoints call, so a figure here and a figure on a Meter screen cannot disagree.

```
Claude Code ──stdio──▶ meter.mcp ──▶ dashboard / optimize_measured / ai_reads ──▶ Postgres (RLS)
                        (tools)        (the same services api.py calls)
```

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

- **It cannot write.** There is no tool that applies an optimization, renames a
  feature, changes settings or triggers a sync — the whole surface is three
  read functions, and a test asserts exactly that, so a fourth tool cannot
  arrive quietly.
- **It cannot cross tenants.** One process serves one tenant, resolved once at
  startup. Every query runs through the service layer, which opens `tenant_tx()`,
  so Postgres row-level security enforces the boundary — the same way it does for
  the API. Passing another organization's feature id returns "no such feature".
- **It returns no content.** Meter stores no prompts, responses, tool arguments
  or retrieved documents, so there is nothing to leak. Repeated-request evidence
  is a salted one-way fingerprint and a count. Example runs carry the operation
  name, model and cost — the things that locate a call site — and deliberately
  not the customer reference.

## Setting it up for Claude Code

You need a Python environment with the backend installed (`make install` gives
you one at `backend/.venv`) and a database URL for the Meter instance you want to
read — your local `make demo` database, or your deployed one.

```bash
claude mcp add meter \
  --scope user \
  --env DATABASE_URL="postgresql://…" \
  --env METER_EMAIL="you@example.com" \
  --env METER_PASSWORD="…" \
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
| `DATABASE_URL` | yes | The Meter database. |
| `METER_EMAIL` | yes | Your Meter login. Its organization is the one served. |
| `METER_PASSWORD` | yes | That login's password, checked once at startup. |
| `METER_APP_DB_PASSWORD` | no | Connect as the RLS-enforced `meter_app` role rather than the owner. Recommended; see [deploy.md](deploy.md). |
| `PYTHONPATH` | no | `…/Meter/backend`. Not needed when the editable install is healthy; see above. |
| `METER_LOG_LEVEL` | no | Defaults to `INFO`. Logs go to stderr — stdout carries the protocol. |

**On the password.** Meter authenticates browsers with a session cookie, and its
only token (`hook_token`) is write-scoped for SDK ingest; reusing that for reads
would make a write credential more powerful. So the server signs in the way a
person does, through `auth.login()`, with no new authentication model and no
privilege you do not already have. A scoped read-only API token is the obvious
next step, and would replace these two variables with one.

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
fourth tool has to be added deliberately.

The transport is hand-rolled because the official `mcp` package needs Python
3.10 and this backend supports 3.9. If that floor moves, `server.py` can be
replaced by the SDK's server and `tools.py` does not change.
