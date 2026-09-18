# Meter — v1 Build Plan

**Companion to:** `meter-design-doc.md` (the canonical spec — read it first)
**Audience:** Claude Code
**Goal:** Ship v1 — per-feature AI build + inference cost for a cybersecurity CTO/CFO, connector path as the must-ship core, metering hook just behind.

---

## Where this plan stands (updated 2026-09-18)

**M0–M8 are all complete.** They were built in order on 2026-06-03 and the
connector path has been shippable since M6. Everything the product has grown
since — alerts, reconciliation, traces, applications, products, the optimization
Copilot, prompt optimization, infrastructure cost — is **post-plan work this
document never described**, and it is summarised under *After M8* below.

Read the milestones as the historical record of what was planned. Each one now
carries a **Status** line saying what is actually true, including where the
build deviated from the plan. Where a milestone's acceptance criteria are not
fully met today, the Status line says so rather than claiming a pass.

**This plan is no longer where the live backlog lives.** Two specs carry it:

- [`optimization-opportunities-spec.md`](optimization-opportunities-spec.md) —
  the measured optimization foundation (Part I, built through M-opt-8) and the
  Copilot roadmap (Part II). This is the main one.
- [`prompt-optimization-spec.md`](prompt-optimization-spec.md) — consented
  prompt capture and eval-backed model downgrade.

## How this plan was worked

The milestones below are all delivered, so this section is the working rhythm
rather than a live instruction — it still applies to whatever is picked up next,
from the specs linked above.

Work **one milestone at a time, in order.** Do not start a milestone until the previous one meets its acceptance criteria. After each milestone: commit, run the checks, and stop for review before continuing. When something in this plan conflicts with the design doc, the design doc wins on *intent*; ask before deviating on scope.

**Two hard rules for the whole build:**
- The **connector path is the must-ship core** and must stand alone. The hook (M7) can land slightly behind but must never block onboarding or first value.
- **Never blend build cost and inference cost** into one number. They are always separate. Every cost row carries a `confidence` value.

---

## M0 — Repo scaffold & foundations
**Build:** Monorepo skeleton — `backend/` (Python, serverless-friendly), `web/` (React + TypeScript), `sdk/` (placeholder for M7), `infra/` (IaC), `docs/` (move the design doc + this plan here). Set up linting, formatting, a test runner per language, and a basic CI check. Add `.env.example`; never commit secrets.
**Acceptance:** `make test` (or equivalent) runs green with one placeholder test per package; CI passes; README explains how to run backend + web locally.
**Status:** ✅ Complete (2026-06-03). `make test`, `make lint` and `make format` exist and CI runs backend, web and both SDKs on every push. `make test-sdk` covers the Python and Node suites.

## M1 — Data model & multi-tenancy
**Build:** Postgres schema for the six entities in §6 of the design doc: `feature`, `feature_signal`, `build_cost`, `inference_cost`, `bill_reconciliation`, `feature_usage`. Migrations. Row-level tenant isolation (every table has `tenant_id`; enforce it). Seed script with one fake tenant and sample data so the UI has something to render.
**Acceptance:** Migrations apply cleanly; a query for tenant A never returns tenant B's rows (write a test); seed data loads; `inference_cost.source` and `feature.discovery_confidence` exist as designed.
**Status:** ✅ Complete (2026-06-03). All six entities are in `0001_core_schema.sql`; the schema is now 56 numbered additive migrations (through `0056_signal_prefix_measured.sql`). RLS is enforced per tenant with `tenant_isolation` policies and the app connects as the `meter_app` role (`0043`); `test_tenant_isolation.py` is the test the acceptance criteria asked for.

## M2 — Auth & tenant onboarding shell
**Build:** SaaS auth (signup/login), tenant creation on signup, encrypted storage for per-tenant connector credentials. The three-step onboarding wizard *shell* (Connect → Review → Confirm) with empty states — no real data yet.
**Acceptance:** A new user can sign up, land in an empty tenant, and walk the 3-step wizard UI; credentials are stored encrypted at rest; logout/login works.
**Status:** ✅ Complete (2026-06-03), then **deliberately replaced** (`4a198ae`, 2026-06-20). Auth, tenant-on-signup and encrypted credentials (`credentials.py` + `crypto.py`) are unchanged and still true. The **three-step wizard is gone**: a gated Connect → Review → Confirm flow put a wall in front of an empty dashboard, so it was folded into an app shell with sidebar nav, and onboarding is now an auto-hiding `OnboardingChecklist` on the Overview. `/onboarding` redirects to `/`. The wizard's own components were kept and reused (`FeaturesStep`, `ConnectorRow`, `BuildCostActions`), so behaviour moved rather than being rewritten.

## M3 — GitHub connector + feature auto-discovery
**Build:** Read-only GitHub connector (PRs, repos, branches, authorship). Auto-discovery: pull the **last 90 days** of merged PRs and use Claude to cluster them into proposed features, each with a `discovery_confidence` and supporting signals (PRs, branch pattern). Wire into Wizard Step 2 with **Rename / Split / Merge / Delete / Add manually**. Confirm writes `feature` + `feature_signal` rows.
**Acceptance:** Connecting a real GitHub org produces proposed features with PR/branch evidence and confidence badges; split/merge/rename/delete/add all persist correctly; "Confirm & go live" creates confirmed features.
**Status:** ✅ Complete (2026-06-03), and since extended. The 90-day window is still the default (`discovery.DEFAULT_LOOKBACK_DAYS`); rename / split / merge / delete / add all exist in `features.py` and are exercised from the Features page rather than a wizard step. Discovery has since gained scheduled runs with overlap, a repo scope, BYOK for the clustering model (`discovery_llm.py`), and **products** — a grouping above features for organizations that ship several products from one GitHub org (`0055_product.sql`, `products.py`). One GitHub owner per tenant is still the limit.

## M4 — Provider cost ingest (inference, connector path)
**Build:** Read-only connectors for the **Anthropic Usage & Cost Admin API** and **OpenAI usage/costs API**. Scheduled ingest on a cadence. Store authoritative totals by API key/project/model into `inference_cost` with `source = cost_api`. Attribute to features by per-feature key/project (high confidence) or service/repo mapping (lower confidence) per §7.1. Everything unmapped → Unattributed bucket.
**Acceptance:** After connecting a provider, monthly inference totals match the provider's own dashboard for the period; mapped features show inference cost with correct confidence; unmapped spend appears in Unattributed.
**Status:** ✅ Complete (2026-06-03), and far exceeded. The plan asked for two connectors; there are now **20 inference connectors** — Anthropic, OpenAI, Google Gemini, OpenRouter, Together, Fireworks, Bedrock, Azure OpenAI, LiteLLM, Vercel AI Gateway, Modal, ElevenLabs, Groq, Mistral, xAI, Perplexity, Cohere, Replicate, Portkey, Helicone — plus self-hosted compute pools. The Unattributed bucket works as designed. See `credentials.KNOWN_CONNECTORS` for the live list.

## M5 — Build-cost ingest (coding tools)
**Build:** Coding-tool usage connectors (start with Cursor for Teams per-seat export; add Claude Code/Copilot/Codex where APIs exist; support CSV fallback). Allocate per-developer coding spend to features by PR/branch overlap from M3. Write `build_cost` rows with `confidence`.
**Acceptance:** Build cost appears per feature and per developer, broken down by tool; allocation logic is covered by tests on a known fixture; CSV import path works for tools without an API.
**Status:** ✅ Complete (2026-06-03). Cursor for Teams (`cursorspend.py`) and Claude Code (`claudecode.py`) have real admin-API importers; Copilot seats come through the GitHub credential; CSV import is the fallback for everything else. Per-developer allocation by PR/branch overlap is in `build.py` and covered by fixture tests. Seat rosters can also come from Okta or Microsoft Entra (`0014`, `0015`).

## M6 — The three screens
**Build:** (a) **Features dashboard** — table with build cost, monthly inference cost, active users, cost/user, "Worth it?" indicator, per-row confidence badge, and the Unattributed row. (b) **Feature drill-down** — three headline numbers, build cost by developer, inference trend over time, evidence trail, connector-vs-hook indicator. (c) Finish wiring the onboarding wizard end to end. `feature_usage` can be manual/CSV for now.
**Acceptance:** A seeded/real tenant renders all three screens; clicking a number opens its evidence trail (the actual signals behind it); build and inference are never shown as one blended figure.
**Status:** ⚠️ Complete with one honest gap. (a) and (b) shipped and have grown well past three screens — the app now has Overview, Applications, Products, Features, Traces, Recommendations, Prompts, Alerts, Reconciliation, Connect sources, Install SDK, Settings and a knowledge base. (c) was overtaken: the wizard it refers to was deleted in `4a198ae` (see M2).

**The gap:** `feature_usage` was never wired up. The table, the service (`features.set_usage`), the route (`PUT /api/features/{id}/usage`) and the client method (`api.setUsage`) all exist, but **no screen calls it** — so Active users, Cost per user and "Worth it?" are populated only by the demo seed and are permanently blank for a real tenant. The plan said "manual/CSV for now"; neither was built. Either build the entry path or stop rendering three columns that can never fill.

> **Connector path complete here. This is shippable to the design partner. M7 adds precision.**

## M7 — Metering hook (SDK + ingest + reconciliation)
**Build:** Thin **metering SDK** (Python first, then Node) that wraps LLM client calls and emits per-call events (`tokens_in`, `tokens_out`, `model`, `feature_id`). Hook-ingest endpoint. Internal versioned **pricing tables** to cost metered tokens. Write `inference_cost` rows with `source = hook`. **Reconciliation job:** per period, compare summed hook cost vs the provider cost-API total → write `bill_reconciliation`; route any delta to Unattributed. Surface hook vs connector origin in the drill-down, and lift hook-attributed rows to High confidence.
**Acceptance:** Installing the SDK in a sample app produces per-feature hook rows; reconciliation ties hook totals to the provider bill within tolerance and pushes the gap to Unattributed; the confidence ladder (§7.3) is reflected in the UI; onboarding still completes with the hook *not* installed.
**Status:** ✅ Complete (2026-06-03), and rebuilt since. The SDK is at **2.2.0** in both Python and Node and has been through a v2 rewrite from per-call events to **traces and spans** (`0049_ai_traces.sql`, `traces.py`), which is what powers the Traces and Applications screens. Hook ingest, versioned pricing tables and the confidence ladder all work. Reconciliation grew from a job into its own package (`meter/reconciliation/`, `0040_reconciliation.sql`) with a screen of its own. Onboarding completes without the hook, as required.

Optimize mode — the SDK's salted-fingerprint signals behind the duplicate-call and prompt-caching findings — was **lost in the v2 rewrite and restored on 2026-09-18**; see the optimization spec for what it feeds. Nothing reaches PyPI or npm until an `sdk-v*` tag is pushed.

## M8 — Hardening & demo readiness
**Build:** Error handling on connector failures, ret/backoff on ingest, empty/loading states, basic observability/logging, and a scripted demo path for the design partner. Tighten the <10-minute onboarding.
**Acceptance:** A fresh signup reaches real numbers in under 10 minutes using connectors only; connector failures degrade gracefully (no crashes, clear UI state); a documented demo script runs start to finish.
**Status:** ✅ Complete (2026-06-03). `make demo` runs the whole stack against a throwaway Postgres, [`demo-script.md`](demo-script.md) is the scripted path, connector failures surface per connector rather than crashing, `retrying.py` handles backoff, and the frontend reports to Datadog RUM (`web/src/observability/`). The hosted demo still needs a manual "Reset demo data" run.

---

## After M8 — what the product grew into

None of this was in the plan. It is listed so the plan stops implying the
product ends at M8, and so the specs above have something to hang from.

| Area | What shipped | Where |
|---|---|---|
| **Alerts** | Threshold, percentage-increase and budget-percentage rules; incidents, an activity feed, Slack/webhook/email delivery | `0029`, `0050`, `alerts.py`, `alerts_eval.py` |
| **Budgets & forecast** | A monthly budget and a server-computed forecast on the Overview | `0041`, `budgets.py` |
| **Reconciliation** | Provider invoice against ingested cost, as its own module and screen | `0040`, `meter/reconciliation/` |
| **Traces & applications** | Agent runs as traces and spans; per-application and per-trace views | `0049`, `0051`, `traces.py`, `applications.py` |
| **Products** | A grouping above features, for an org shipping several products from one repo set | `0055`, `products.py` |
| **Optimization Copilot** | Measured and modelled savings, four levers, verified realized savings | `optimize_measured.py`, `optimize_billing.py` |
| **Prompt optimization** | Consented prompt capture, candidate prompts, evaluation with BYOK | `0052`–`0054`, `prompt_*.py` |
| **Infrastructure cost** | The cloud bill as first-class line items, classified per service | `0045`–`0048`, `infrastructure.py` |
| **Knowledge base + assistant** | In-app handbook and a retrieval-grounded support assistant | `assistant.py`, `web/src/help/` |

## Known gaps

Small, specific, and true as of 2026-09-18. Each is a candidate for the next
piece of work rather than a defect to be hidden.

- **`feature_usage` has no entry path** (see M6). Three columns can never fill
  for a real tenant.
- **Infrastructure cost is in no total.** `infra_cost` is ingested, classified
  and deduped against Bedrock, but it surfaces only on Connect sources — it
  reaches neither the Overview totals nor per-feature attribution. Folding it in
  is the spec's Phase 2.
- **One GitHub owner per tenant.** Discovery reads a single token and scans a
  single owner, so an org spread across two GitHub organizations cannot be
  fully covered.
- **Applications cannot be renamed**, and **products have no alert or budget
  scope** — alert rules scope to organization, provider, model or feature only.
- **`cost_sources.py` has no tests.**
- **The hosted demo needs a manual reset** to return to a clean state.

---

## Deferred (post-v1)

The original line here read *"Slice 2 usage-analytics connectors; Slice 3
Stripe/revenue and quantitative ROI; Slice 4 trends & anomaly alerts. See design
doc §11."* Two of those three are still deferred. **Slice 4 shipped anyway** —
cost-over-time trends are on the Overview and anomaly alerting is a full
subsystem (an `increase_pct` rule is exactly the design doc's "inference on
Report generator jumped 40% this month"), so listing it as forbidden was
misleading anyone reading this file. `CLAUDE.md` has been corrected to match.

- **Slice 2 — usage-analytics connectors.** Still deferred, and the reason
  `feature_usage` sits empty. Design doc §11.
- **Slice 3 — Stripe/revenue and quantitative ROI.** Still deferred. Until it
  lands, "Worth it?" is cost-per-user and is labelled directional.
- **Slice 4 — trends & anomaly alerts.** ✅ Shipped, from 2026-08-17.

## Commit & review discipline
Commit per logical change with clear messages. Stop at each milestone boundary for human review. If a milestone reveals the design doc is wrong or underspecified, flag it and propose an update rather than guessing.
