# Contributing to Meter

Thanks for your interest! Meter is open source (AGPL-3.0) — bug reports, fixes,
features, docs, and forks are all welcome. This guide gets you from clone to first PR.

- [Ways to contribute](#ways-to-contribute)
- [Development setup](#development-setup)
- [Project tour](#project-tour)
- [Running tests, lint, and format](#running-tests-lint-and-format)
- [How the app fits together](#how-the-app-fits-together)
- [Coding conventions](#coding-conventions)
- [Common tasks (recipes)](#common-tasks-recipes)
- [The non-negotiable invariants](#the-non-negotiable-invariants)
- [Commit & PR guidelines](#commit--pr-guidelines)
- [Continuous integration](#continuous-integration)
- [Reporting security issues](#reporting-security-issues)
- [License of contributions](#license-of-contributions)

## Ways to contribute

- **Report a bug** — open an issue with steps to reproduce, expected vs. actual.
- **Fix a bug / add a feature** — open a PR (please run the checks first).
- **Improve docs** — README, the design doc, the deploy guide, code comments.
- **Add a connector** — new build-cost or inference sources are high-value (see recipes).

If a change is large or changes product behavior, open an issue to discuss it first.

## Development setup

**Prerequisites**

- **Python 3.9+**
- **Node 18+** and npm
- **GNU Make**
- **PostgreSQL 16** — macOS: `brew install postgresql@16`. You only need the
  binaries installed; the test suite launches its own throwaway Postgres.

**Install & run**

```bash
git clone https://github.com/costlyinfra-admin/Meter.git
cd Meter

make install     # backend virtualenv (backend/.venv) + web npm deps
make demo        # one-command local app: throwaway seeded DB + API + web
make test        # backend + web test suites
```

`make demo` opens the app at http://localhost:5173 with a seeded demo tenant and a
printed login. `make help` lists every target.

> **macOS:** if Postgres won't start manually (*“postmaster became multithreaded”*),
> `export LC_ALL=en_US.UTF-8`. The test suite handles this for you.

## Project tour

| Path | What lives here |
|---|---|
| `backend/meter/` | The Python package: API, ingest, attribution, reconciliation. |
| `backend/migrations/` | Plain SQL migrations, applied in filename order (`0001_…`, `0002_…`). |
| `backend/tests/` | pytest suite (runs against an ephemeral Postgres). |
| `backend/seed.py` | Seeds the demo tenant + login. |
| `web/src/` | React + TypeScript SPA (pages, components, API client). |
| `web/src/pages/` | Login/Signup, the onboarding wizard, dashboard, feature drill-down. |
| `sdk/python/`, `sdk/node/` | The optional metering-hook SDKs and their tests. |
| `deploy/` | `release.sh` (migrations + app-role password) and `start.sh` (entrypoint). |
| `docs/` | Design doc, build plan, deploy guide, demo script. |

Key backend modules (`backend/meter/`):

| Module | Responsibility |
|---|---|
| `db.py` | Connections + the `tenant_tx` context manager that drives Row-Level Security. |
| `migrations.py` | The tiny SQL migration runner. |
| `auth.py`, `crypto.py` | Signup/login (bcrypt) and Fernet credential encryption. |
| `github.py` | Read-only GitHub PR client. |
| `discovery.py` | Clusters PRs into proposed features (Claude or heuristic). |
| `features.py` | Feature editing: rename/split/merge/delete/confirm + signal mapping. |
| `providers.py` | Read-only Anthropic/OpenAI cost clients. |
| `inference.py` | Inference-cost ingest + attribution (the confidence ladder). |
| `build.py` | Build-cost CSV import + per-developer allocation. |
| `pricing.py`, `hook.py` | Pricing tables and the metering-hook ingest + reconciliation. |
| `dashboard.py` | Read-side aggregation for the dashboard and drill-down. |
| `api.py` | The FastAPI app (routes, sessions, serving the built web app). |

## Running tests, lint, and format

Please make sure all of these are green before opening a PR:

```bash
make test        # backend (pytest) + web (vitest)
make lint        # ruff (backend) + eslint (web)
make test-sdk    # Python + Node SDK tests
make format      # auto-format: ruff (Python) + prettier (TypeScript)
```

Per-package, if you prefer:

```bash
# Backend
cd backend && .venv/bin/pytest
cd backend && .venv/bin/ruff check . && .venv/bin/ruff format .

# Web
cd web && npm test
cd web && npm run lint && npm run build   # build = type-check + bundle
```

New behavior should come with tests — **especially** anything touching attribution,
reconciliation, or tenant isolation (the core guarantees). The backend tests spin up
a real Postgres, so they exercise actual SQL and RLS, not mocks.

## How the app fits together

- **Two database roles.** Migrations, auth, and seeding run as the **owner/admin**
  role (which can act across tenants). The running app connects as a separate,
  non-privileged **app role** (`meter_app`) for which Row-Level Security is
  always in force. Each request sets `app.current_tenant`; RLS filters every query.
- **Attribution → confidence → evidence.** Spend is matched to a feature via signals
  (`feature_signal` rows). Each cost row records a `confidence`; the drill-down traces
  any number back to the signals behind it. Unmatched spend goes to the **Unattributed**
  bucket (a `NULL` `feature_id`).
- **Connector vs. hook.** `inference_cost.source` is `cost_api` (authoritative
  provider totals) or `hook` (per-call metered). Reconciliation compares them and
  routes the gap to Unattributed — no double-counting.
- **One deployable.** In production the FastAPI app also serves the built web app
  (`METER_STATIC_DIR`), so it's a single image on one domain.

## Coding conventions

- **Python:** formatted and linted with **ruff** (line length 100). Type hints on
  public functions. Prefer clarity over cleverness. SQL is parameterized — never
  string-interpolate user input into queries.
- **TypeScript/React:** **ESLint** + **Prettier**; functional components and hooks;
  the API client lives in `web/src/api.ts`.
- **SQL migrations are append-only.** Never edit a migration that may have been
  applied; add a new numbered file. Keep them readable and reversible in intent.
- **Secrets never enter the repo.** Only `.env.example` (placeholders). Don't log
  secrets or credentials.
- **Tests:** colocated under `backend/tests/` and `web/src/**/*.test.tsx`.

## Common tasks (recipes)

**Add a database migration**

1. Create `backend/migrations/00NN_short_name.sql` (next number in sequence).
2. Apply locally: `make db-migrate` (or it's applied automatically by the test
   fixtures and on deploy).
3. If it changes tenant tables, confirm RLS still holds — see
   `backend/tests/test_tenant_isolation.py`.

**Add an inference or build-cost connector**

- Inference: add a read-only client in `providers.py` returning normalized
  `CostRecord`s; the attribution/persistence in `inference.py` is provider-agnostic.
- Build cost: produce `DeveloperSpend` records (e.g. a new CSV shape or API) and feed
  `build.allocate_and_store`.
- Keep connectors **read-only**, inject the HTTP client so it's testable with a mock
  transport (see `test_providers.py` / `test_github.py`), and add tests.

**Work on the metering SDK**

- Python: `sdk/python` (`make test-sdk` or `cd sdk/python && pytest`).
- Node: `sdk/node` (`node --test`).
- SDKs must stay **thin, dependency-free, and fail-safe** (never throw into the
  caller's request path).

## The non-negotiable invariants

These are the heart of the product (full list in [`CLAUDE.md`](CLAUDE.md)). Please
preserve them in any change:

1. **The connector path stands alone.** The metering hook is optional and must never
   be required for onboarding or first value.
2. **Build cost and inference cost are never blended** into one number — separate in
   storage and in the UI.
3. **Every cost row carries a confidence value** and is explainable via its evidence
   trail. No black-box numbers.
4. **`feature_id` is the spine.** Unmapped spend goes to the **Unattributed** bucket,
   never silently dropped.
5. **Reconcile, don't trust blindly.** Provider cost APIs are authoritative on
   dollars; hook-metered cost is reconciled against them, and any gap goes to Unattributed.
6. **All connectors are read-only**, credentials encrypted at rest, with strict
   per-tenant isolation (enforced by Postgres RLS).

## Commit & PR guidelines

- Small, focused commits with clear, present-tense messages explaining **what and why**.
- Branch off `main`; open your PR against `main`.
- Make sure CI is green and `make test && make lint` pass locally.
- Describe the change and, for behavior changes, how you verified it.
- Keep unrelated changes out of the same PR.

## Continuous integration

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs three jobs on every push
and PR: **backend** (ruff + pytest, with a real Postgres), **web** (eslint + vitest),
and **SDKs** (Python + Node). PRs should be green before review. (The repo also has a
scheduled cost-ingest cron and a manual demo-seeder workflow — those run against a
deployment, not in PR CI.)

## Reporting security issues

Please **do not** open a public issue for security vulnerabilities. Instead, report
them privately to the maintainers (e.g. via a GitHub private security advisory on the
repository). We'll acknowledge and work on a fix before any public disclosure. Tenant
isolation, credential handling, and the hook-ingest auth are the most sensitive areas.

## License of contributions

By contributing, you agree your contributions are licensed under the project's
**AGPL-3.0-or-later** license.
