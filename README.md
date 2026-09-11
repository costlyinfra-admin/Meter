<div align="center">

# Meter

**Know what every feature cost you — to *build* and to *run*.**

Meter takes a company's blended AI bill and shows exactly which **features**
consumed it: what each feature cost to **build** (AI coding tools) and to **run**
(LLM inference) — so a CTO/CFO can decide whether the AI investment was worth it.

[![CI](https://github.com/costlyinfra-admin/Meter/actions/workflows/ci.yml/badge.svg)](https://github.com/costlyinfra-admin/Meter/actions/workflows/ci.yml)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPLv3-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![Node](https://img.shields.io/badge/node-18%2B-green)
![Postgres](https://img.shields.io/badge/postgres-16-blue)

[**Live demo**](https://meter.costlyinfra.com) · [Design doc](docs/meter-design-doc.md) · [Testing with real accounts](docs/testing-with-real-accounts.md) · [Deploy guide](docs/deploy.md) · [Contributing](CONTRIBUTING.md)

</div>

> **Try it in 5 seconds:** open the [live demo](https://meter.costlyinfra.com)
> and click **“View the demo”** on the login screen — no signup required.

---

## Table of contents

- [The problem](#the-problem)
- [What Meter does](#what-meter-does)
- [Core concepts](#core-concepts)
- [Screens](#screens)
- [Architecture](#architecture)
- [Repository layout](#repository-layout)
- [Tech stack](#tech-stack)
- [Quick start (local)](#quick-start-local)
- [Running the app locally](#running-the-app-locally)
- [Configuration](#configuration)
- [The data model](#the-data-model)
- [Security & multi-tenancy](#security--multi-tenancy)
- [The metering hook (SDK)](#the-metering-hook-sdk)
- [Deploy your own instance](#deploy-your-own-instance)
- [Testing & quality](#testing--quality)
- [Project status](#project-status)
- [Documentation](#documentation)
- [Contributing](#contributing)
- [License](#license)

---

## The problem

Companies spend real money on AI with **zero attribution**. A typical bill is one
blended number that mixes:

- **LLM inference** — the product calling Claude / OpenAI in production,
- **AI coding tools** — Claude Code, Cursor, Copilot, Codex used to *build* features,
- **hosting/infra** for those AI workloads.

The CTO can't answer the board's simplest question: *“Is the AI money paying off?”*
They can't tell which features that spend built or runs, so they can't tell which
were worth shipping. Meter answers the one number they actually ask for:

> **“What did it cost to ship — and to run — this feature?”**

Not a dashboard full of vanity metrics. One credible, **defensible** number per
feature, with enough backup that a CFO trusts it and an auditor can challenge it.

## What Meter does

For every **feature** a company ships, Meter reports:

1. **Build cost** — AI coding-tool spend attributed to the developers and PRs that built it.
2. **Inference cost** — LLM API spend the deployed feature consumes in production.
3. A directional **“worth it?”** signal — usage / cost-per-active-user.

It connects **read-only** to the sources you already have (GitHub, Anthropic/OpenAI
admin APIs, coding-tool exports), auto-discovers your features from PR history, and
attributes spend to each one — with a **confidence level** and an **evidence trail**
on every number. Anything it can't confidently attribute lands in a visible
**Unattributed** bucket rather than being silently dropped.

The buyer is a **CTO/CFO**, not a developer: onboarding takes under 10 minutes and
requires no engineering project.

## Core concepts

| Concept | What it means |
|---|---|
| **The feature is the spine** | Every dollar attributes to a `feature_id`, or to the **Unattributed** bucket. |
| **Build vs. inference, never blended** | The two costs answer different questions (*was the build efficient?* vs *is it expensive to keep alive?*) and are always shown and stored separately. |
| **Connector path first** | Read-only connectors give per-feature cost with zero code changes. This is the must-ship core and stands alone. |
| **Optional metering hook** | A thin SDK can meter per-call inference for exact, per-feature precision — but it's never required for onboarding or first value. |
| **Confidence + evidence** | Every cost row carries `high`/`med`/`low` confidence; clicking any number opens the exact signals (PRs, branches, keys, hook tags) behind it. |
| **Reconcile, don't trust blindly** | Provider cost APIs are authoritative on dollars; hook-metered cost is reconciled against them, and any gap flows to Unattributed. |

## Screens

- **Features dashboard** — a table of features with build cost, monthly inference
  cost, active users, cost/user, a *“Worth it?”* indicator, a confidence badge per
  row, and an Unattributed row. The money screen.
- **Feature drill-down** — three headline numbers, build cost by developer,
  inference trend over time, and the full **evidence trail** behind every figure.
- **Onboarding wizard** — Connect → Review (auto-discovered features) → Confirm,
  in under 10 minutes.

## Architecture

A single deployable image serves the API **and** the web app; data lives in
Postgres; an optional SDK and a scheduled job feed it.

```
                 ┌──────────────────────────┐
                 │  Browser — React SPA     │
                 └────────────┬─────────────┘
                              │  HTTPS  (/api + static)
                              ▼
   read-only   ┌──────────────────────────────────┐    SQL    ┌───────────────────────┐
 ┌────────────▶│        FastAPI backend           │──────────▶│  PostgreSQL           │
 │  GitHub     │  auth · connector ingest ·       │           │  multi-tenant, RLS,   │
 │  Anthropic  │  feature discovery (Claude) ·    │           │  encrypted creds      │
 │  OpenAI     │  attribution · reconciliation ·  │           └───────────────────────┘
 │  coding     │  serves the built web app        │
 │  tools      └──────────────┬───────────────────┘
 └─────────────────┐          │ ▲
                   │          │ │ per-call events (optional)
   Scheduled ingest│          │ └──────────────  Metering SDK (Python / Node)
   (GitHub Actions │          │                  in the customer's app
    cron) ─────────┘          ▼
                     authoritative $ from provider cost APIs,
                     reconciled against hook-metered $
```

- **Connectors are read-only** and use the customer's own admin credentials,
  stored **encrypted at rest**.
- **Feature discovery** clusters the last 90 days of merged PRs into proposed
  features — using Claude when an API key is configured, and a deterministic
  branch/repo heuristic otherwise (so it always works offline).

## Repository layout

| Path | What it is |
|---|---|
| [`backend/`](backend) | Python API + ingest/attribution/reconciliation (FastAPI, psycopg). |
| [`backend/migrations/`](backend/migrations) | Plain SQL migrations, applied in filename order. |
| [`web/`](web) | React + TypeScript single-page app (Vite, Vitest). |
| [`sdk/python/`](sdk/python), [`sdk/node/`](sdk/node) | The optional metering-hook SDKs (dependency-free, fail-safe). |
| [`deploy/`](deploy) | Release + entrypoint scripts for the container. |
| [`infra/`](infra) | Deployment notes / IaC home. |
| [`docs/`](docs) | Design doc, build plan, deploy guide, demo script. |
| [`Dockerfile`](Dockerfile), [`render.yaml`](render.yaml) | Single-image build + a free-tier deploy blueprint. |
| [`Makefile`](Makefile) | One entry point for install / test / lint / run / deploy helpers. |
| [`.github/workflows/`](.github/workflows) | CI, the scheduled ingest cron, and a one-click demo seeder. |

## Tech stack

| Layer | Choice |
|---|---|
| Backend | Python, **FastAPI**, **psycopg** (Postgres driver), **uvicorn** |
| Frontend | **React** + **TypeScript**, **Vite**, **React Router** |
| Database | **PostgreSQL 16** with **Row-Level Security** (multi-tenant isolation) |
| Auth | Email/password, bcrypt, signed http-only session cookies |
| Discovery | **Claude** (Anthropic) over PR history, with a heuristic fallback |
| SDKs | **Python** (stdlib only) + **Node** (global `fetch`) — both thin & fail-safe |
| Tooling | **ruff** (lint+format), **pytest**, **ESLint**, **Vitest**, **Make** |
| Deploy | **Docker** (one image), **Render** + **Neon** (free tier), GitHub Actions cron |

## Quick start (local)

**Prerequisites**

- **Python 3.9+**
- **Node 18+** and npm
- **GNU Make**
- **PostgreSQL 16** — on macOS: `brew install postgresql@16`. (The test suite spins
  up its own throwaway Postgres, so you don't need a running server just to run tests.)

**Get it running**

```bash
git clone https://github.com/costlyinfra-admin/Meter.git
cd Meter

make install     # backend virtualenv + web dependencies
make demo        # spins up a throwaway seeded DB, starts API + web, prints a login
```

`make demo` opens the app at **http://localhost:5173** with a fully populated demo
tenant. Log in with the credentials it prints (or click **“View the demo”**).
Press **Ctrl-C** to tear everything down — nothing persists.

**Run the tests**

```bash
make test        # backend (pytest) + web (vitest)
make lint        # ruff + eslint
make test-sdk    # the Python + Node SDKs
```

## Running the app locally

Prefer to run the two processes yourself (e.g. for development)? You need a running
Postgres plus two environment variables.

```bash
# 1. A database
createdb meter
export DATABASE_URL="postgresql://localhost:5432/meter"
export APP_SECRET_KEY="dev-secret-change-me"   # local only — see Configuration

# 2. Schema + (optional) demo data
make db-migrate          # apply migrations
make db-seed             # optional: load the demo tenant + login

# 3. Two terminals
make api                 # FastAPI on http://localhost:8000
make web                 # Vite dev server on http://localhost:5173 (proxies /api)
```

> **macOS note:** if starting Postgres manually fails with *“postmaster became
> multithreaded during startup”*, run `export LC_ALL=en_US.UTF-8` first. The test
> suite handles this automatically.

Useful Make targets (`make help` lists them all):

| Target | Does |
|---|---|
| `make install` | Install backend (venv) + web (npm) dependencies |
| `make demo` | One-command seeded local app (throwaway DB + API + web) |
| `make test` / `make lint` / `make format` | Run / lint / auto-format both packages |
| `make db-migrate` / `make db-seed` | Apply migrations / load the demo tenant |
| `make api` / `make web` | Run the backend / web dev server |
| `make ingest` | Run the scheduled cost-ingest job once |

## Configuration

Copy [`.env.example`](.env.example) to `.env` for local development. **Never commit
a real `.env`** — `.gitignore` excludes it, and production secrets live only in your
host's secret store (Render env vars / GitHub Actions secrets), never in the repo.

| Variable | Required? | Purpose |
|---|---|---|
| `DATABASE_URL` | **yes** | Postgres connection for the owner/admin role (migrations, auth, seeding). |
| `APP_SECRET_KEY` | **yes** (API) | Signs session cookies **and** encrypts stored connector credentials. **Keep it stable** — changing it makes saved credentials undecryptable. |
| `METER_APP_DB_PASSWORD` | prod | Password for the RLS-enforced app role. When set, the app derives its DB connection from `DATABASE_URL` automatically. |
| `DATABASE_APP_URL` | optional | Explicit app-role connection (overrides the derivation above). |
| `METER_SECURE_COOKIES` | prod | Set `true` behind HTTPS to mark the session cookie `Secure`. |
| `METER_STATIC_DIR` | optional | When set, the API also serves the built web app (the Docker image sets this). |
| `ANTHROPIC_API_KEY` | optional | Enables Claude-powered feature discovery; falls back to a heuristic if unset. |
| `METER_LOG_LEVEL` | optional | Backend log level (default `INFO`). |

> Connector credentials (GitHub token, provider admin keys, coding-tool exports)
> are **not** environment variables — customers enter their own in the app, and
> they're encrypted at rest per tenant.

## The data model

Six core entities, plus a `tenant` anchor, defined as plain SQL in
[`backend/migrations/`](backend/migrations):

| Table | Purpose |
|---|---|
| `feature` | The spine. Auto-proposed from PRs, then confirmed by a human. |
| `feature_signal` | The evidence trail — PRs, branches, keys, hook tags behind each feature. |
| `build_cost` | AI coding-tool spend per developer/tool, attributed to a feature (or Unattributed). |
| `inference_cost` | LLM spend by key/project/model; `source` is `cost_api` (connector) or `hook` (metered). |
| `bill_reconciliation` | Keeps hook-metered totals honest against the authoritative provider bill. |
| `feature_usage` | Active users per feature, powering cost-per-user. |

## Security & multi-tenancy

Security is a first-class concern (the first vertical is cybersecurity):

- **Tenant isolation is enforced by the database, not just app code.** Every tenant
  table uses Postgres **Row-Level Security**: the app connects as a non-privileged
  role and each request sets `app.current_tenant`, so a query can only ever return
  the current tenant's rows — even if a `WHERE` clause is forgotten. (Covered by
  [`backend/tests/test_tenant_isolation.py`](backend/tests/test_tenant_isolation.py).)
- **All connectors are read-only** and use the customer's own admin credentials.
- **Credentials are encrypted at rest** (Fernet) before they touch the database;
  only ciphertext is stored.
- **Auth** is bcrypt-hashed passwords over signed, http-only session cookies; the
  metering hook authenticates server-to-server with a per-tenant token (hashed).

## The metering hook (SDK)

The optional precision tier. A thin, **fail-safe** wrapper reports per-call usage
(`tokens_in`, `tokens_out`, `model`, `feature_id`); cost is computed server-side
from versioned pricing tables. It's a no-op when unconfigured, so the same code runs
with or without it.

```python
# Python — pip install costlyinfra-meter
from costlyinfra_meter import Meter
meter = Meter(application="support-agent")   # reads METER_INGEST_URL/TOKEN

# One call: wrap the client once and every call becomes its own trace.
client = meter.wrap(anthropic_client, feature_id="feature-threat-triage")
client.messages.create(model="claude-sonnet-4-6", ...)

# A workflow: each step becomes a span under one run, so "why did this run
# cost $1.42" has an answer.
with meter.agent("resolve-ticket", feature_id="feature-threat-triage") as run:
    run.llm("classify", lambda: client.messages.create(...))
    docs = run.tool("retrieve-documents", retrieve_documents)
    run.llm("generate-answer", lambda: client.messages.create(...))
```

The SDK never sends prompts, responses, tool arguments, tool results, retrieved
documents or exception text — there is no field for any of it. Cost is computed
server-side from versioned pricing tables.

Hook totals are **reconciled against the provider's authoritative bill** each
period; any gap surfaces in Unattributed rather than corrupting a feature's number.
See [`sdk/`](sdk) for the Python and Node packages.

### Already using OpenTelemetry?

Meter also accepts OTLP/HTTP traces, so an application instrumented with
OpenLLMetry, OpenInference or OpenTelemetry's GenAI conventions can report without
the SDK. Point a trace exporter at it:

```bash
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=https://<your-meter>/api/otel/v1/traces
OTEL_EXPORTER_OTLP_TRACES_HEADERS=Authorization=Bearer%20<ingest token>
```

Spans are translated into the same runs and steps the SDK records
([`backend/meter/otel.py`](backend/meter/otel.py)) and priced the same way. Only a
fixed list of attributes is read. Prompt and response content, span events, and any
attribute not on that list are never read or stored.

Already sending to **Splunk Observability Cloud**? Add Meter as a second exporter
in the Splunk Collector. The setup guide's **Splunk** tab gives a pipeline that
strips prompt and response content from Meter's copy before it leaves, while what
goes to Splunk stays exactly as it was.

## Deploy your own instance

Meter ships as a **single Docker image** that serves the API and the web app,
backed by a managed Postgres. The repo includes everything to deploy on a **free**
stack reachable at your own subdomain:

- **[`docs/deploy.md`](docs/deploy.md)** — a numbered, copy-paste walkthrough:
  Neon (Postgres) → Render (the app) → your DNS → a free GitHub Actions cron.
- [`Dockerfile`](Dockerfile) is host-agnostic — any container host works.
- [`render.yaml`](render.yaml) is a one-click Render Blueprint.

You only provide three secrets (a database URL, an app secret key, and an app-DB
password) via your host's secret store — never in the repo.

## Testing & quality

- **Backend:** `pytest` against an ephemeral Postgres (real RLS, real SQL).
- **Web:** `vitest` + Testing Library.
- **SDKs:** `pytest` (Python) and `node --test` (Node).
- **Lint/format:** `ruff` (Python), `ESLint` + Prettier (TypeScript).
- **CI** ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs the backend,
  web, and SDK suites on every push and PR.

Run it all locally with `make test && make lint && make test-sdk`.

## Project status

Built milestone-by-milestone (see [`docs/build-plan.md`](docs/build-plan.md)).
**v1 is feature-complete and deployed:**

- **M0–M2** — Repo + tooling, data model & RLS multi-tenancy, auth & onboarding shell.
- **M3** — GitHub connector + feature auto-discovery.
- **M4** — Provider (Anthropic/OpenAI) inference-cost ingest + attribution.
- **M5** — Build-cost ingest from coding tools (CSV / Cursor export) + allocation.
- **M6** — The three screens (dashboard, drill-down, wizard end to end).
- **M7** — Optional metering hook: SDKs, ingest, reconciliation.
- **M8** — Hardening (retries, graceful failures, logging) + one-command demo.

The connector path is the shippable core; the metering hook is additive precision.

## Documentation

- [`docs/meter-design-doc.md`](docs/meter-design-doc.md) — the canonical spec (intent, data model, attribution, screens).
- [`docs/build-plan.md`](docs/build-plan.md) — the ordered milestones (M0–M8) with acceptance criteria.
- [`docs/deploy.md`](docs/deploy.md) — deploy-your-own-instance guide.
- [`docs/demo-script.md`](docs/demo-script.md) — a start-to-finish demo narrative.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — how to set up, run, and contribute.
- [`CLAUDE.md`](CLAUDE.md) — standing instructions / non-negotiable invariants (AI-build guidance; not required to use the app).

## Contributing

Contributions and forks are welcome. Start with **[`CONTRIBUTING.md`](CONTRIBUTING.md)**
for the dev setup, repo tour, coding conventions, and the invariants to preserve.
Please run `make test && make lint` before opening a PR.

## License

Meter is licensed under the **GNU Affero General Public License v3.0**
(AGPL-3.0) — see [`LICENSE`](LICENSE). You're free to use, modify, and self-host it;
if you run a **modified** version as a network service, the AGPL requires you to make
your source available to its users. For different (commercial/closed) terms, that's a
separate licensing conversation with the copyright holder.

© 2026 CostlyInfra.
