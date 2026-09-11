# Meter — Design Doc

**Version:** 0.2
**Date:** June 3, 2026
**Status:** Draft for build
**Author:** Bipin (with Claude)

> Meter takes a company's blended AI bill and tells them exactly which features consumed it — what each feature cost to **build** and to **run** — so a CTO/CFO can decide whether the AI investment was worth it.

This is a clean-slate design. It has **no relation** to any prior fork, scanner, or codebase. Meter is its own product, its own company.

**What changed in 0.2:** added the instrumentation hook (the metering SDK) as part of v1, alongside the connector-only path; introduced the confidence ladder and bill reconciliation; made the data model hook-ready; folded in the onboarding-wizard details from the validated wireframes.

---

## 1. The problem

Companies are spending real money on AI with zero attribution. Our design-partner customer (a cybersecurity company) spends **~$24k/month** on AI — a single blended number that mixes:

- LLM inference (their product calling Claude/OpenAI in production)
- AI coding tools their engineers use to build features (Claude Code, Cursor, Copilot, Codex)
- Hosting and infrastructure for AI workloads

The CTO can't answer the board's simplest question: *"Is the AI money paying off?"* They can't tell which features that $24k built or runs, so they can't tell which were worth shipping.

**The one number they asked for:** *"What did it cost to ship this feature?"*

Not a dashboard. Not an ROI model. One credible, defensible number per feature — with enough backup that a CFO trusts it.

## 2. The buyer

The buyer is a **CTO or CFO**, not a developer. That dictates everything:

- They want a **business-decision tool**, not an instrumentation toolkit.
- The output must be **board-grade**: clean, explainable, defensible.
- They will not adopt an SDK-heavy, developer-led product. (We chose SaaS over open source for this reason — CTOs/CFOs don't buy from GitHub.)
- Onboarding must take **under 10 minutes** and require no engineering project.

> **Design tension we resolve in this doc:** the buyer wants zero-friction onboarding, but the most accurate inference numbers need instrumentation. Meter resolves this with a **two-tier model** — connector-only for instant first value, plus an optional hook for precision. The hook is shipped in v1 but is *never* required to see real numbers.

**First vertical: cybersecurity.** Security companies are heavy, fast-growing AI spenders (threat triage, SOC automation, vuln summarization), they already buy "visibility" tools (SIEM, CSPM), and their CFOs are under board pressure to justify AI spend.

## 3. The product principle

Everything in Meter hangs off one spine: **the feature.**

A *feature* is a unit of product work the company shipped (e.g. "AI threat triage," "Report generator"). For each feature, Meter answers three questions, in priority order:

1. **What did it cost to build?** — AI coding-tool spend attributed to the developers and PRs that built it.
2. **What does it cost to run?** — inference spend (LLM API calls) the feature consumes in production.
3. **Is it worth it?** — usage and, eventually, value/revenue signal per feature.

v1 nails #1 and #2 cleanly and gives a *directional* answer to #3. We do **not** try to produce a single magic ROI number — we give the CTO/CFO just enough to make the call themselves, with confidence indicators so they know how much to trust each row.

## 4. The two cost categories

This split is core and comes directly from the customer:

| Category | What it is | Source of truth |
|---|---|---|
| **Build cost** | AI coding tools used to develop the feature (Claude Code, Cursor, Copilot, Codex). Attributed per developer and per PR. | Coding-tool admin/usage APIs + GitHub PR history |
| **Inference cost** | LLM API calls the deployed feature makes in production. | Provider Admin APIs (authoritative $) + optional hook (per-feature resolution) |

Build cost is **one-time-ish** (concentrated during development, with a long tail of maintenance). Inference cost is **recurring monthly**. We always show them separately — never blended — because they answer different questions ("was the build efficient?" vs. "is this feature expensive to keep alive?").

## 5. Core concept: the feature as spine

```
                          ┌─────────────────┐
                          │     FEATURE     │  ← the spine
                          │ "AI threat triage"│
                          └────────┬────────┘
                                   │
        ┌──────────────────────────┼──────────────────────────┐
        │                          │                          │
   BUILD COST                 INFERENCE COST              USAGE / VALUE
   (developers × PRs ×        (prod LLM calls            (active users,
    coding-tool spend)         tagged to feature)         adoption, $ later)
        │                          │                          │
   GitHub + coding-tool       Provider Admin APIs         Product analytics
   admin APIs                 + optional hook             / Stripe (later)
```

Every dollar Meter sees is either (a) attributed to a feature, or (b) sitting in an **Unattributed bucket** that the customer can triage. The unattributed bucket is a feature, not a bug — it's honest, and it gives the CTO a reason to come back ("there's $3.1k I haven't explained yet").

## 6. Data model (v1)

Six core entities. Postgres, multi-tenant with row-level tenant isolation.

- **`feature`** — `id`, `tenant_id`, `name`, `description`, `status` (proposed / confirmed / archived), `shipped_at`, `discovery_confidence` (high/med/low — how sure auto-discovery was when proposing it), `created_at`. Features are auto-proposed from PR history during onboarding, then confirmed/edited by a human.

- **`feature_signal`** — the evidence links that map raw activity to a feature. `id`, `feature_id`, `signal_type` (`pr` | `repo` | `branch` | `service` | `api_key` | `usage_tag` | `hook_tag`), `external_ref`, `confidence`, `source`. This table is *why* a number is what it is — it powers the evidence trail. `branch` (e.g. `feature/threat-*`) and `hook_tag` (a `feature_id` emitted by the SDK) are first-class signal types.

- **`build_cost`** — `id`, `feature_id`, `developer_id`, `tool` (claude_code | cursor | copilot | codex), `pr_ref`, `amount`, `period`, `confidence`. Rolls up to per-feature and per-developer build cost.

- **`inference_cost`** — `id`, `feature_id`, `provider` (anthropic | openai), `model`, `api_key_ref`, `amount`, `period`, `tokens_in`, `tokens_out`, `request_count`, `source` (`cost_api` | `hook`), `confidence`. The `source` field is what makes the model hook-ready: connector rows come from the provider cost API (authoritative totals, attributed by key); hook rows are per-call metered (`feature_id` known at the source). See §7 for how the two reconcile.

- **`bill_reconciliation`** — `id`, `tenant_id`, `provider`, `period`, `billed_total` (from provider cost API), `attributed_total` (sum of hook-metered rows), `delta`, `status`. Keeps hook numbers honest against the real bill; any delta flows to the unattributed bucket.

- **`feature_usage`** — `id`, `feature_id`, `period`, `active_users`, `events`, `source`. Powers the "worth it?" view (cost per active user). Value/revenue (`feature_value`) is deferred to a later slice.

`feature_id` is the foreign key that ties everything together. `confidence` lives on every attribution row so the UI can show how trustworthy each number is.

## 7. Attribution, the hook, and confidence

Attribution is **probabilistic and transparent**, never a black box. There are two layers, and both ship in v1.

### 7.1 Connector layer (always on, zero friction)

- **Build cost.** Coding-tool spend per developer (from the tool's admin API) is allocated to features by which PRs/repos/branches that developer touched during the period (from GitHub). A developer who spent $167 in Claude Code whose PRs that month were ~70% on "AI threat triage" contributes ~$117 of build cost to that feature.
- **Inference cost.** Prod LLM spend from the provider Admin APIs (Anthropic Usage & Cost API, OpenAI usage/costs API) is the **authoritative dollar total**. These APIs break spend down by **API key, workspace/project, and model** — *not* by arbitrary request metadata — so without a hook we attribute by per-feature key/project (clean) or by service/repo mapping (heuristic, lower confidence).

### 7.2 Hook layer (optional, ships in v1)

A lightweight **metering SDK** (Python + Node) wraps the customer's LLM calls. It does not merely tag — it **meters**: captures `tokens_in`, `tokens_out`, `model`, and a `feature_id` per call and reports them to Meter, which computes cost from internal pricing tables. This gives per-call, per-feature precision that the provider cost APIs can't.

Critically, the hook does **not** replace the provider bill — it's reconciled against it:

```
provider cost API   →  authoritative monthly total per key      (the truth on $)
hook metered rows   →  sum of per-feature costs we computed      (the resolution)
reconciliation      →  match the two; any gap → Unattributed bucket
```

If hook totals tie out to the provider bill, the attributed features are trustworthy. If there's a delta (untagged calls, a model we mispriced), it surfaces in the unattributed bucket instead of silently corrupting a feature's number. The provider API keeps the hook honest; the hook gives the provider API resolution.

**What the hook never carries.** Prompts, responses, messages, tool arguments and results, retrieved documents, exception text. The event format has no field for them and ingest refuses payloads that try. The one exception is separate from metering entirely: **Prompt Optimization** collects prompt templates and short-lived samples on its own channel, only for features where a customer admin has explicitly consented, encrypted per tenant and deleted after 30 days. See [`prompt-optimization-spec.md`](prompt-optimization-spec.md).

### 7.3 The confidence ladder

Every inference number carries a confidence level, driven by how it was attributed:

| Tier | How inference cost was attributed | Confidence |
|---|---|---|
| 1 | Shared key, split by service/repo heuristic | **Low** |
| 2 | Dedicated per-feature API key / OpenAI project | **Med–High** |
| 3 | Hook installed, metered per call, reconciled to the bill | **High** |

Build-cost rows carry their own High/Med/Low (direct PR/branch match vs. inferred overlap). And feature *discovery* carries a separate `discovery_confidence` (how sure auto-discovery was that a cluster of PRs is one real feature) — distinct from cost-attribution confidence.

Clicking any number opens the **evidence trail**: the exact signals (PRs, branches, keys, hook tags, periods) that produced it. This is what lets a CFO trust the number and an auditor challenge it.

## 8. Connectors

v1 connects four categories (Stripe/value deferred):

1. **Inference spend** — Anthropic Admin API and OpenAI Admin API (org-level usage + cost by key/project/model). Polled on a regular cadence. Authoritative dollar source.
2. **Build activity** — GitHub (PRs, repos, branches, authorship) + coding-tool admin/usage exports (Claude Code, Cursor for Teams, Copilot, Codex). GitHub is the backbone for mapping developers → features.
3. **Hook ingest** — an endpoint that receives metered per-call events from the Meter SDK (§7.2). Optional for the customer, but part of the shipped product.
4. **Usage (light)** — optional product-analytics connector for active-user counts per feature; can also be a manual/CSV input in v1.

All connectors are **read-only** and use the customer's own admin credentials, stored encrypted. Data is per-tenant isolated. Minimum to get started: **GitHub + one AI provider.**

## 9. The screens

These are mocked and validated against the customer's need. v1 ships these.

### 9.1 Features dashboard (home)
The money screen. A table of features with: build cost, monthly inference cost, active users, cost per user, a **"Worth it?"** indicator, and a confidence badge per row. Plus an **Unattributed** row showing spend not yet mapped (including any bill-reconciliation delta). The CTO scans this in 30 seconds and knows which features to question.

### 9.2 Onboarding wizard (<10 min) — three steps
A linear three-step flow:

1. **Connect sources.** Connect GitHub + one AI provider to start (Anthropic Admin API, OpenAI Admin API), with coding-tool sources (e.g. Cursor for Teams per-seat export) addable here or later. Each source shows a clear connected/not-connected state.
2. **Review auto-discovered features.** Claude analyzes the **last 90 days of GitHub PRs** and proposes features, reporting what it found (e.g. "47 merged PRs across 4 repositories → 5 proposed features, 3 high / 2 medium confidence"). Each proposal shows its PRs and branch pattern, a discovery-confidence badge, and edit actions: **Rename, Split** (one proposal is really two features), **Merge** (two proposals are one feature), **Delete**, and **Add feature manually**.
3. **Confirm & go live.** The user confirms the feature list and lands on the dashboard with real connector-based numbers. The optional hook is offered here as the precision upgrade (with copy-paste SDK snippets) — install now or later; it never blocks going live.

The promise is "from signup to first real numbers in under 10 minutes," and onboarding is where the customer decides if the product is real.

### 9.3 Feature drill-down
Everything for one feature on one page: the three headline numbers up top, **build cost by developer** (with which coding tool each used) on the left, **inference cost trend over time** on the right, and the **evidence trail** at the bottom so any number can be defended. Surfaces per-developer-seat detail on the build side, and shows whether each inference figure is connector-derived or hook-metered.

## 10. Tech stack

Pragmatic, cloud-native, one engineer (Claude) building it:

- **Frontend:** React (TypeScript), single-page app. The screens above.
- **Backend:** Python services / serverless functions for connector ingest, hook-event ingest, an API layer, and reconciliation jobs.
- **Metering SDK:** Python + Node packages (the hook). Thin wrapper around LLM client calls; emits metered events. Versioned, semver, minimal dependencies.
- **Pricing tables:** internal, versioned per-model price data used to cost hook-metered tokens; must be kept current as model prices change.
- **DB:** Postgres (multi-tenant, row-level tenant isolation).
- **Ingest:** scheduled jobs pulling Admin/GitHub APIs on a cadence; feature auto-discovery uses an LLM (Claude) over PR history; a streaming/batch path for hook events.
- **Cloud:** AWS (or equivalent). Infra-as-code.
- **Auth:** standard SaaS auth + per-tenant data isolation; encrypted credential storage.

(Deliberately conventional — the differentiation is the attribution model and the buyer-grade UX, not the plumbing.)

## 11. Roadmap (slices)

- **Slice 1 — v1 (build + inference, connector + hook).** The three screens, GitHub + Anthropic/OpenAI + coding-tool connectors, the metering SDK + hook ingest + bill reconciliation, per-feature build & run cost, the confidence ladder, evidence trail, and unattributed triage. Ship to the design-partner customer.
- **Slice 2 — Usage depth.** Product-analytics connectors for real per-feature adoption and cost-per-active-user.
- **Slice 3 — Value / ROI.** Stripe or self-attested revenue/retention signal per feature → the "is it worth it" answer gets quantitative.
- **Slice 4 — Trends & alerts.** Cost-over-time trends, anomaly alerts ("inference on Report generator jumped 40% this month").

## 12. Risks & open questions

- **v1 scope is larger now.** Shipping the hook in v1 adds the SDKs (Python + Node), pricing-table maintenance, and reconciliation logic on top of the connector path. Mitigation: connector path is the must-ship core and stands alone; the hook can land slightly behind it within v1 without blocking onboarding.
- **Pricing-table accuracy.** Hook-metered cost is only as right as our per-model price data. Mitigation: reconcile against the provider bill every period — drift shows up immediately as a reconciliation delta.
- **SDK maintenance burden.** Two language SDKs to keep working as provider clients evolve. Mitigation: keep them thin; cover the dominant clients first (Anthropic + OpenAI Python/Node).
- **Attribution accuracy (connector path).** Build-cost allocation by PR/branch overlap is heuristic. Mitigation: always show confidence + evidence; let customers correct mappings, which improves the model.
- **Adoption of the hook.** The buyer may never install it, leaving inference at Med/Low confidence. Mitigation: per-feature keys/projects (config, not code) get most of the way there; the hook is the opt-in ceiling, not the floor.
- **"Worth it?" without revenue.** Until Slice 3, worth-it is cost-per-user, not true ROI. Be explicit that it's directional.
- **Single design partner.** All current requirements come from one cybersecurity customer. Validate the dashboard with 2–3 more security CTOs before generalizing.

## 13. Definition of done for v1

A cybersecurity CTO can, within 10 minutes of signup, see their monthly AI spend broken out per feature into build vs. inference cost, with a confidence level and an evidence trail on every number — using connectors alone. Customers who want feature-level precision can install the metering SDK day one and see hook-metered inference reconciled against their actual provider bill. The features dashboard is board-ready as-is.
