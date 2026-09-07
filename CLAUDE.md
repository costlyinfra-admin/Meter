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

## The two source-of-truth docs
- `docs/meter-design-doc.md` — canonical spec (intent, data model, attribution, screens). **The design doc wins on intent.**
- `docs/build-plan.md` — ordered milestones (M0–M8) with acceptance criteria. **Work one milestone at a time, in order; stop at each boundary for review.**

If either is wrong or underspecified, flag it and propose an update — don't silently guess.

## Non-negotiable invariants
1. **Connector path is the must-ship core** and must stand alone. The metering hook (M7) is a precision tier — it must never be required for onboarding or first value.
2. **Never blend build cost and inference cost.** They are always shown and stored separately.
3. **Every cost row carries a `confidence` value**, and every number must be explainable via its evidence trail (the `feature_signal` rows behind it). No black-box numbers.
4. **`feature_id` is the spine.** Build cost, inference cost, and usage all attribute to a feature, or land in the **Unattributed bucket** — never silently dropped.
5. **Reconcile, don't trust blindly.** Provider cost APIs are authoritative on dollars; hook-metered cost is reconciled against them, and any delta goes to Unattributed.
6. **All connectors are read-only**, use the customer's own admin credentials, stored **encrypted at rest**, with strict **per-tenant isolation** on every table.

## Stack & conventions
- **Backend:** Python, serverless-friendly. **Frontend:** React + TypeScript. **DB:** Postgres (multi-tenant, row-level tenant isolation). **SDK (M7):** Python first, then Node — keep it thin. **Cloud:** AWS, infra-as-code.
- Commit per logical change with clear messages. Add/maintain tests, especially for attribution and reconciliation logic. Keep secrets out of the repo (`.env.example` only).
- Prefer clarity over cleverness; this codebase is maintained by a non-developer founder working through Claude.

## Out of scope for v1 (do not build)
Product-analytics usage connectors (Slice 2), Stripe/revenue & quantitative ROI (Slice 3), trends & anomaly alerts (Slice 4). See design doc §11.

## Tone of collaboration
The founder is non-technical. Explain decisions briefly in plain language, surface trade-offs, and ask before expanding scope. Milestone-by-milestone with review checkpoints is the default working rhythm.
