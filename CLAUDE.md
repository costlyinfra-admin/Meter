# CLAUDE.md — Meter

> **What this file is (for forkers):** these are the project's standing
> instructions for AI coding assistants (e.g. Claude Code) — the product intent,
> non-negotiable invariants, and working conventions. It's **not** required to run
> or use Meter; for that, see [`README.md`](README.md),
> [`CONTRIBUTING.md`](CONTRIBUTING.md), and [`docs/`](docs). It's kept in the repo
> because the invariants below are genuinely useful context for any contributor.

Standing instructions for building Meter. Read these before any work.

## What Meter is
A SaaS that disaggregates a company's blended AI bill into **per-feature cost** — what each feature cost to **build** (AI coding tools) and to **run** (inference). Buyer is a **CTO/CFO** (a business-decision tool, not a developer tool). First vertical: cybersecurity. Clean-slate product — **no relation to any prior fork, scanner, or codebase.**

## The source-of-truth docs
- `docs/meter-design-doc.md` — canonical spec (intent, data model, attribution, screens). **The design doc wins on intent.**
- `docs/build-plan.md` — the original milestones M0–M8, **all of which are complete**, each annotated with what is actually true today and where the build deviated. Read it for history and for its *Known gaps* list; it is no longer the backlog.
- `docs/optimization-opportunities-spec.md` and `docs/prompt-optimization-spec.md` — **where the live backlog is.** Start here for what to build next.

If any of these is wrong or underspecified, flag it and propose an update — don't silently guess.

## Non-negotiable invariants
1. **Connector path is the must-ship core** and must stand alone. The metering hook (M7) is a precision tier — it must never be required for onboarding or first value.
2. **Never blend build cost and inference cost.** They are always shown and stored separately. This is absolute and is not softened by the clause that follows. *Human effort* (design doc §4.1) is a third category, stored on its own: it may be added to **metered customer inference** to give a clearly labelled *customer-attributed delivery cost*, which is never presented as the provider bill and never includes build cost.
3. **Every cost row carries a `confidence` value**, and every number must be explainable via its evidence trail (the `feature_signal` rows behind it). No black-box numbers.
4. **`feature_id` is the spine.** Build cost, inference cost, and usage all attribute to a feature, or land in the **Unattributed bucket** — never silently dropped.
5. **Reconcile, don't trust blindly.** Provider cost APIs are authoritative on dollars; hook-metered cost is reconciled against them, and any delta goes to Unattributed.
6. **All connectors are read-only**, use the customer's own admin credentials, stored **encrypted at rest**, with strict **per-tenant isolation** on every table.

## Stack & conventions
- **Backend:** Python, serverless-friendly. **Frontend:** React + TypeScript. **DB:** Postgres (multi-tenant, row-level tenant isolation). **SDK (M7):** Python first, then Node — keep it thin. **Cloud:** AWS, infra-as-code.
- Commit per logical change with clear messages. Add/maintain tests, especially for attribution and reconciliation logic. Keep secrets out of the repo (`.env.example` only).
- Prefer clarity over cleverness; this codebase is maintained by a non-developer founder working through Claude.

## Out of scope for v1 (do not build)
Product-analytics usage connectors (Slice 2) and Stripe/revenue & quantitative ROI (Slice 3). See design doc §11.

Slice 4 (trends & anomaly alerts) **was** on this list and shipped anyway, in August 2026 — trends are on the Overview and alerting is a full subsystem with rules, incidents and delivery. Saying otherwise here was misleading anyone who read it, so it has been removed rather than left as a rule nobody follows.

## Tone of collaboration
The founder is non-technical. Explain decisions briefly in plain language, surface trade-offs, and ask before expanding scope. Milestone-by-milestone with review checkpoints is the default working rhythm.
