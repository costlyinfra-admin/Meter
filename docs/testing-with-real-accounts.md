# Testing Meter with real accounts

A practical, forwardable checklist for connecting a real company's sources so you
can see actual per-feature cost — not the demo data.

> **App:** <https://meter.costlyinfra.com> → **Create an account** (your data
> lives in its own isolated workspace).
>
> **Safety:** every connector is **read-only** and stored **encrypted at rest**.
> Meter never writes to your systems. Use short-lived tokens and revoke them
> after testing (see [Cleanup](#after-testing-cleanup)).

Meter splits a blended AI bill into **per-feature build cost** (what each
feature cost to *create*) and **inference cost** (what it costs to *run*), kept
separate, each traced to evidence. Onboarding mirrors that in four steps:

1. **Identify features** — connect GitHub, discover features from merged PRs
2. **Build cost sources** — per-developer AI coding-tool spend
3. **Inference cost sources** — provider/cloud LLM bills
4. **Confirm & go live**

You can connect as much or as little as you want; anything unmapped lands in an
honest **Unattributed** bucket rather than being faked.

---

## What to generate and send — your setup

Your stack — **GitHub (private repos)**, **Claude Code** on **Claude Team**,
**Anthropic** in production, **Modal**, **Vercel** — needs just **two
credentials**. Create them and send them over a **secure channel** (e.g. a
1Password share — not plain email/Slack). Both are read-only; revoke after
testing.

**1. GitHub token** (read access to private repos)
- GitHub → avatar (top-right) → **Settings** → **Developer settings** (bottom of
  left nav) → **Personal access tokens** → **Fine-grained tokens** →
  **Generate new token**.
- **Resource owner:** your organization. **Repository access:** *All repositories*
  (or just the ones you ship). **Permissions** (set each to **Read-only**):
  **Metadata**, **Contents**, **Pull requests**.
- **Expiration:** 30 days is plenty. Generate → **send me the token**.
- *Simpler alternative:* a **classic** token with the **`repo`** and **`read:org`**
  scopes.

**2. Anthropic Admin API key** — covers **both** Claude Code build cost and any
production Claude inference (one key, both sides).
- <https://console.anthropic.com> → **Settings** → **Admin Keys** (you must be an
  org **owner/admin**) → **Create Admin Key**.
- **Send me the `sk-ant-admin-…` key.**

**Optional — Modal:** only if you run your **LLMs** on Modal (not just app
compute): send the **monthly $** for that GPU usage (modal.com → **Settings** →
**Usage/Billing**). No key needed.

**Vercel:** nothing needed — it's app hosting, not an AI cost.

Those two tokens (plus the optional Modal figure) are everything I need to set up
your full build + inference picture.

---

## 1. GitHub — required (feature discovery + private repos)

Powers step 1. With a token, it reads **private** repos too. Discovery looks at
**merged pull requests from the last 90 days**, so a repo with few/no PRs will
surface few features (expected, not a bug).

**Recommended — fine-grained token (least privilege):**
1. GitHub → avatar (top-right) → **Settings** → **Developer settings** (bottom of
   left nav) → **Personal access tokens** → **Fine-grained tokens** →
   **Generate new token**.
2. **Resource owner:** your **organization**.
3. **Repository access:** *All repositories* (or hand-pick the ones you ship).
4. **Repository permissions** — set each to **Read-only**: **Metadata**,
   **Contents**, **Pull requests**.
5. Generate → copy the token. *(If the org requires approval for fine-grained
   tokens, approve it under Org → Settings → Personal access tokens.)*

**Simpler fallback — classic token:** Settings → Developer settings →
**Tokens (classic)** → Generate new (classic) → scope **`repo`** (+ **`read:org`**)
→ copy the `ghp_…`.

> Public-org test? You can skip the token entirely on step 1 — discovery works
> unauthenticated for public repos (lower rate limit).

---

## 2. Build cost sources — connect what you use

Each source produces per-developer spend, which Meter allocates to features
by **who authored which PRs**. Pick the connectors matching your tools; the rest
you can ignore. Precision ladder, most precise first:

| Your coding tool | In-app action (step 2) | What you need |
|---|---|---|
| **Claude Code** | *Sync Claude Code spend* | Anthropic **Admin API key** (see §3 — same key) |
| **GitHub Copilot** | *Sync seats* | GitHub token with **Copilot billing admin** access + your org login |
| **Cursor** | *Sync spend* | Cursor **Team admin API key** (actual usage dollars) |
| **Tabnine / Amazon Q / Gemini Code Assist / …** | *SSO seats* | An **Okta** API token or **Microsoft Entra** app registration + the app→tool mapping |
| **Anything else** | *Import build cost (CSV)* | `developer,tool,amount` rows (fallback) |

### Claude Code (the common case)
No CSV needed. Connect Anthropic (§3), then on the **Build cost sources** step
click **Sync Claude Code** — it pulls each developer's Claude Code cost from
Anthropic's Admin API and allocates it to features.

> The `developer` identity is matched to each person's **GitHub username** (via
> their email). If your corporate emails don't match GitHub logins, those seats
> are still counted but land in **Unattributed**.

### CSV fallback (any tool without a connector)
Build a file and paste it into *Import build cost*:
```
developer,tool,amount
alice,claude_code,30
bob,cursor,84.40
```
`developer` = the person's GitHub username; `amount` = their monthly spend.

---

## 3. Anthropic Admin API key (inference cost **and** Claude Code build cost)

One key serves two purposes: the **inference** cost connector (your production
Claude bill) and the **Claude Code** build-cost connector (§2).

1. <https://console.anthropic.com> → **Settings** → **Admin Keys** (visible to org
   **owners/admins** only).
2. **Create Admin Key** → copy the `sk-ant-admin-…`.
3. In Meter, connect **Anthropic** on the **Inference cost sources** step (or
   paste the key inline in the *Sync Claude Code* action) and click **Sync**.

> **Build vs run hygiene:** this key sees *all* Anthropic spend — Claude Code
> coding *and* production Claude calls. The Claude Code connector reads the
> per-developer **Claude Code analytics** (build side); the inference connector
> reads the **cost report** and attributes by API-key/project → feature (run
> side). For the cleanest split, use **distinct API keys or workspaces** for
> Claude Code vs production, and map the production keys to features in Meter.

---

## 4. Other inference providers — only what you run in production

Connect on the **Inference cost sources** step, then **Sync**:

| Provider | Credential / notes |
|---|---|
| **OpenAI** | Organization **Admin key** (org owner) |
| **Google Gemini** | Google Cloud billing OAuth token (project-scoped) |
| **OpenRouter / Together / Fireworks** | The provider's API/admin key |
| **Amazon Bedrock** | A JSON blob: `{"access_key_id","secret_access_key","region","tag"}` — reads AWS Cost Explorer, attributed by a cost-allocation **tag → feature** |
| **Self-hosted models** (vLLM/Ollama, or models on **Modal/RunPod/own GPUs**) | *Self-hosted models* → register a pool with its **monthly infra cost**; per-feature split needs the metering SDK around your model calls |

> **Not AI cost → not tracked:** plain app hosting/compute (e.g. **Vercel**, or
> Modal/EC2 when they're *not* running LLMs) isn't a build or inference cost, so
> Meter doesn't ingest it.

### Provider coverage — how much to trust each number

Not every source reports dollars the same way. When you're sanity-checking
totals against a provider's own console, expect this:

| Tier | What it means | Connectors |
|---|---|---|
| **Authoritative dollars** | We read the provider's actual **billed cost** (its cost/billing API). Match these to the vendor's invoice. | Anthropic · OpenAI · Google Gemini · Amazon Bedrock · Azure OpenAI · LiteLLM · Vercel AI Gateway · Portkey · Helicone |
| **Dollars if reported, else token-priced** | We use the provider's reported dollar amount when its usage API returns one; otherwise we compute cost from token counts × our price book (`pricing.py`). | OpenRouter · Together · Fireworks · Groq · Mistral · xAI (Grok) · Perplexity · Cohere · Replicate |
| **Estimated** | No dollar API exists, so the number is a transparent estimate — directionally right, not invoice-exact. | ElevenLabs (characters × a per-1k-char rate) · Self-hosted pools (infra cost split by usage share) · SSO-seat build cost (seats × seat price book) |

Two things make a "token-priced" or "estimated" total drift from the real bill,
and both are **visible, never silent**:

- **Price-book drift.** Per-token and per-seat rates live in `backend/meter/pricing.py`
  and `seatpricing.py`. If a vendor changed prices, the number is off until the
  table is updated — send the current rate and it's a one-line fix.
- **Reconciliation gap.** Where we can compare a computed total to an
  authoritative one, any difference is surfaced as an explicit gap (and unmapped
  spend lands in **Unattributed**), so you always see *that* it's off, not a
  confidently-wrong figure.

> **Beta endpoints:** the **Vercel AI Gateway** and **Modal** cost endpoints are
> still in beta; if a sync fails to reach them, add a `"url"` field to that
> connector's JSON credential to point at the right endpoint.

---

## Worked example — a "Claude Code shop"

Stack: **GitHub (private repos)**, **Claude Code** (primary tool, on Claude Team),
**Anthropic** in production, **Modal** for runtime, **Vercel** for hosting.

- **GitHub token** (§1) → step 1, discover features.
- **Anthropic Admin key** (§3) → step 3, *Sync* (production inference) **and**
  step 2, *Sync Claude Code* (build cost). One key, both sides.
- **Modal** → only if it runs your LLMs: register a self-hosted pool with the
  monthly Modal cost on step 3 (skip if it's just app compute).
- **Vercel** → nothing to connect.

Two connectors (GitHub + Anthropic) cover the whole picture.

---

## Order inside the app

1. **Sign up** → onboarding opens on **Identify features**.
2. Paste the **GitHub token**, type your **org login**, click **Analyze last 90
   days**, review/curate the proposed features.
3. **Build cost sources** → run **Sync Claude Code** (and/or Copilot/Cursor/SSO,
   or the CSV fallback).
4. **Inference cost sources** → connect **Anthropic** (and any other providers),
   click **Sync**; optionally register a self-hosted pool.
5. **Confirm & go live** → the dashboard shows per-feature **build vs run** cost,
   cost/user, auto-generated insights, and optimization suggestions.

Everything is also available later from the dashboard's **Add cost data** panel,
so you can connect sources incrementally.

---

## After testing (cleanup)

- **GitHub:** Settings → Developer settings → revoke the token.
- **Anthropic / OpenAI / Cursor / Okta / AWS:** revoke the key/token in that
  provider's console.

Meter stores credentials encrypted, but revoking removes any lingering access
once you're done.

---

## Honest caveats

- The provider/admin API JSON shapes are implemented to each vendor's documented
  spec but validated primarily against synthetic payloads; if a live response
  differs, the parser may need a small field-name tweak. Send a redacted sample
  and it's a quick fix.
- Per-developer attribution depends on the overlap between provider identities
  (emails) and **GitHub logins**. Where they don't match, spend is still counted
  but shown as **Unattributed** — never silently misattributed.
