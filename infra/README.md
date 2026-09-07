# infra/ — Deployment

Meter ships as a **single Docker image** that serves both the API and the
built web app, backed by a managed Postgres. The full step-by-step is in
**[`docs/deploy.md`](../docs/deploy.md)** (free stack: Neon + Render + GitHub
Actions cron + your Cloudflare-managed subdomain).

## What's here / related

| File | Purpose |
|---|---|
| [`../Dockerfile`](../Dockerfile) | Multi-stage build: compiles the web app, then serves it from the FastAPI backend. |
| [`../render.yaml`](../render.yaml) | Render Blueprint — one free Docker web service. |
| [`../deploy/release.sh`](../deploy/release.sh) | Runs migrations + sets the app DB-role password (idempotent). |
| [`../deploy/start.sh`](../deploy/start.sh) | Container entrypoint: release tasks, then launch uvicorn on `$PORT`. |
| [`../.github/workflows/ingest.yml`](../.github/workflows/ingest.yml) | Free scheduled cost-ingest cron. |

## Production model (how isolation holds on managed Postgres)

- The **admin/owner** role (from `DATABASE_URL`) owns the schema and runs
  migrations + auth + seeding. As the table owner it is exempt from RLS — the
  portable replacement for the local "superuser bypass" (see migration
  `0006_relax_force_rls.sql`).
- The **app** role (`meter_app`) is a non-owner, so Row-Level Security still
  fully governs every tenant query. Its password comes from
  `METER_APP_DB_PASSWORD`; the app derives its connection automatically.

## Beyond the free stack

The design doc's eventual target is AWS + infrastructure-as-code (App Runner/ECS
+ RDS + EventBridge). The image and config here port directly — it's a standard
container + Postgres, so there's no lock-in. Terraform/CDK for AWS can be added
in this folder when you're ready to scale.
