# Meter — Design-Partner Demo Script

A start-to-finish demo of the v1 connector path (plus the optional hook). Target
length: **under 10 minutes**.

## 0. One-time setup

```bash
make install                 # backend venv + web deps
brew install postgresql@16   # macOS; or any Postgres 16
```

## 1. Launch (one command)

```bash
make demo
```

This spins up a throwaway Postgres, seeds the **Acme Security** demo tenant,
starts the API (`:8000`) and the web app (`:5173`), and prints the login. Press
**Ctrl-C** to tear everything down (nothing persists).

Open **http://localhost:5173** and sign in:

> **demo@costlyinfra.com** / **meter-demo**

---

## 2. The narrative (what to say)

### The dashboard — "the one number per feature"
You land on the **Features dashboard**. Talking points:

- "Every row is a feature this team shipped. For each one we show **what it cost
  to build** and **what it costs to run** — *in separate columns*. We never blend
  them, because they answer different questions: *was the build efficient?* vs.
  *is this feature expensive to keep alive?*"
- Point at **AI threat triage**: ~$181 build, ~$4,200/mo inference, 540 users,
  ~$7.78 cost/user, **Healthy**, **med** confidence.
- Point at the **Unattributed** row: "This is spend we haven't mapped to a
  feature yet — $30 of build, $760 of inference. It's honest: nothing is silently
  dropped, and it's a to-do list, not a black hole."
- "Every number carries a **confidence** badge, so a CFO knows how much to trust
  each row."

### The drill-down — "defend any number"
Click **AI threat triage**. Talking points:

- Three headline numbers (build / inference / users), still separate.
- **Build cost by developer** — who built it, with which coding tool.
- **Inference trend** — monthly run cost.
- **Evidence trail** at the bottom: "Click into any feature and you see the exact
  PRs, branch patterns, and keys behind the number. This is what lets a CFO trust
  it and an auditor challenge it."

### Onboarding — "under 10 minutes, no engineering project"
Open a fresh signup (or describe it):

1. **Connect** GitHub + one AI provider (read-only tokens).
2. **Review** — Meter reads the last 90 days of merged PRs and proposes
   features; rename / split / merge / delete / add as needed.
3. **Confirm & go live** — lands on the dashboard. Then **Add cost data** syncs
   inference and imports a coding-tool CSV (e.g. a Cursor seat export).

### The hook — "optional precision"
On the dashboard or in onboarding, show the **metering SDK** offer:

- "Connectors already give per-feature cost. For exact, per-call inference
  numbers, drop a one-line SDK into your app. It's **optional** — onboarding and
  first value never require it."
- "Hook numbers are **reconciled against your real provider bill** every period.
  If they tie out, the features are trustworthy; any gap surfaces in Unattributed
  instead of corrupting a feature's number."

---

## 3. Graceful failure (optional aside)

If a connector token is wrong or a provider is down, the UI shows a clear error
(not a crash), retries transient failures automatically, and the rest of the app
keeps working. The connector path stands alone; the hook is additive.

## 4. Close

> "In under ten minutes, a CTO/CFO sees their monthly AI spend broken out per
> feature into build vs. inference, each with a confidence level and an evidence
> trail — using connectors alone. That's the one number the board keeps asking for."
