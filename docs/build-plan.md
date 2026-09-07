# Meter — v1 Build Plan

**Companion to:** `meter-design-doc.md` (the canonical spec — read it first)
**Audience:** Claude Code
**Goal:** Ship v1 — per-feature AI build + inference cost for a cybersecurity CTO/CFO, connector path as the must-ship core, metering hook just behind.

## How to use this plan

Work **one milestone at a time, in order.** Do not start a milestone until the previous one meets its acceptance criteria. After each milestone: commit, run the checks, and stop for review before continuing. When something in this plan conflicts with the design doc, the design doc wins on *intent*; ask before deviating on scope.

**Two hard rules for the whole build:**
- The **connector path is the must-ship core** and must stand alone. The hook (M7) can land slightly behind but must never block onboarding or first value.
- **Never blend build cost and inference cost** into one number. They are always separate. Every cost row carries a `confidence` value.

---

## M0 — Repo scaffold & foundations
**Build:** Monorepo skeleton — `backend/` (Python, serverless-friendly), `web/` (React + TypeScript), `sdk/` (placeholder for M7), `infra/` (IaC), `docs/` (move the design doc + this plan here). Set up linting, formatting, a test runner per language, and a basic CI check. Add `.env.example`; never commit secrets.
**Acceptance:** `make test` (or equivalent) runs green with one placeholder test per package; CI passes; README explains how to run backend + web locally.

## M1 — Data model & multi-tenancy
**Build:** Postgres schema for the six entities in §6 of the design doc: `feature`, `feature_signal`, `build_cost`, `inference_cost`, `bill_reconciliation`, `feature_usage`. Migrations. Row-level tenant isolation (every table has `tenant_id`; enforce it). Seed script with one fake tenant and sample data so the UI has something to render.
**Acceptance:** Migrations apply cleanly; a query for tenant A never returns tenant B's rows (write a test); seed data loads; `inference_cost.source` and `feature.discovery_confidence` exist as designed.

## M2 — Auth & tenant onboarding shell
**Build:** SaaS auth (signup/login), tenant creation on signup, encrypted storage for per-tenant connector credentials. The three-step onboarding wizard *shell* (Connect → Review → Confirm) with empty states — no real data yet.
**Acceptance:** A new user can sign up, land in an empty tenant, and walk the 3-step wizard UI; credentials are stored encrypted at rest; logout/login works.

## M3 — GitHub connector + feature auto-discovery
**Build:** Read-only GitHub connector (PRs, repos, branches, authorship). Auto-discovery: pull the **last 90 days** of merged PRs and use Claude to cluster them into proposed features, each with a `discovery_confidence` and supporting signals (PRs, branch pattern). Wire into Wizard Step 2 with **Rename / Split / Merge / Delete / Add manually**. Confirm writes `feature` + `feature_signal` rows.
**Acceptance:** Connecting a real GitHub org produces proposed features with PR/branch evidence and confidence badges; split/merge/rename/delete/add all persist correctly; "Confirm & go live" creates confirmed features.

## M4 — Provider cost ingest (inference, connector path)
**Build:** Read-only connectors for the **Anthropic Usage & Cost Admin API** and **OpenAI usage/costs API**. Scheduled ingest on a cadence. Store authoritative totals by API key/project/model into `inference_cost` with `source = cost_api`. Attribute to features by per-feature key/project (high confidence) or service/repo mapping (lower confidence) per §7.1. Everything unmapped → Unattributed bucket.
**Acceptance:** After connecting a provider, monthly inference totals match the provider's own dashboard for the period; mapped features show inference cost with correct confidence; unmapped spend appears in Unattributed.

## M5 — Build-cost ingest (coding tools)
**Build:** Coding-tool usage connectors (start with Cursor for Teams per-seat export; add Claude Code/Copilot/Codex where APIs exist; support CSV fallback). Allocate per-developer coding spend to features by PR/branch overlap from M3. Write `build_cost` rows with `confidence`.
**Acceptance:** Build cost appears per feature and per developer, broken down by tool; allocation logic is covered by tests on a known fixture; CSV import path works for tools without an API.

## M6 — The three screens
**Build:** (a) **Features dashboard** — table with build cost, monthly inference cost, active users, cost/user, "Worth it?" indicator, per-row confidence badge, and the Unattributed row. (b) **Feature drill-down** — three headline numbers, build cost by developer, inference trend over time, evidence trail, connector-vs-hook indicator. (c) Finish wiring the onboarding wizard end to end. `feature_usage` can be manual/CSV for now.
**Acceptance:** A seeded/real tenant renders all three screens; clicking a number opens its evidence trail (the actual signals behind it); build and inference are never shown as one blended figure.

> **Connector path complete here. This is shippable to the design partner. M7 adds precision.**

## M7 — Metering hook (SDK + ingest + reconciliation)
**Build:** Thin **metering SDK** (Python first, then Node) that wraps LLM client calls and emits per-call events (`tokens_in`, `tokens_out`, `model`, `feature_id`). Hook-ingest endpoint. Internal versioned **pricing tables** to cost metered tokens. Write `inference_cost` rows with `source = hook`. **Reconciliation job:** per period, compare summed hook cost vs the provider cost-API total → write `bill_reconciliation`; route any delta to Unattributed. Surface hook vs connector origin in the drill-down, and lift hook-attributed rows to High confidence.
**Acceptance:** Installing the SDK in a sample app produces per-feature hook rows; reconciliation ties hook totals to the provider bill within tolerance and pushes the gap to Unattributed; the confidence ladder (§7.3) is reflected in the UI; onboarding still completes with the hook *not* installed.

## M8 — Hardening & demo readiness
**Build:** Error handling on connector failures, ret/backoff on ingest, empty/loading states, basic observability/logging, and a scripted demo path for the design partner. Tighten the <10-minute onboarding.
**Acceptance:** A fresh signup reaches real numbers in under 10 minutes using connectors only; connector failures degrade gracefully (no crashes, clear UI state); a documented demo script runs start to finish.

---

## Deferred (post-v1, do NOT build now)
Slice 2 usage-analytics connectors; Slice 3 Stripe/revenue and quantitative ROI; Slice 4 trends & anomaly alerts. See design doc §11.

## Commit & review discipline
Commit per logical change with clear messages. Stop at each milestone boundary for human review. If a milestone reveals the design doc is wrong or underspecified, flag it and propose an update rather than guessing.
