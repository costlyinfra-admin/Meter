# Deploying Meter (free stack → `meter.costlyinfra.com`)

This gets the whole app live on a **free** stack, on your own subdomain.

> **Forking this for your own use?** Replace the example values below with yours:
> the repo (`costlyinfra-admin/Meter`), the domain (`costlyinfra.com` /
> `meter.costlyinfra.com`), and any branding. The steps are otherwise identical.

**The stack**
- **Database:** Neon (free managed Postgres)
- **App (API + website):** Render (free Docker web service) — one service serves both
- **Scheduled cost ingest:** GitHub Actions (free cron)
- **Domain/DNS:** your existing Cloudflare dashboard (a single DNS record)

> ⏱️ ~30–45 minutes the first time. Free-tier note: the Render free service **sleeps
> after ~15 min idle** (first visit then takes ~30–60s to wake). Fine for demos;
> upgrade the Render service to a paid instance (~$7/mo) when a real user relies on it.

---

## What you'll need to invent (2 secrets)

Generate these once and keep them safe:

- **`APP_SECRET_KEY`** — a long random string. It encrypts stored connector
  credentials, so **never change it after launch** (changing it makes saved
  credentials undecryptable). Generate one:
  ```bash
  python3 -c "import secrets; print(secrets.token_urlsafe(48))"
  ```
- **`METER_APP_DB_PASSWORD`** — a password you pick for the app's database
  role (any strong random string; the same generator works).

---

## Step 1 — Database (Neon)

1. Sign up at **neon.tech**, create a project (pick a region near your users).
2. Copy the **connection string** it shows — looks like
   `postgresql://OWNER:PASSWORD@ep-xxx.neon.tech/neondb?sslmode=require`.
   This is your **`DATABASE_URL`**.

That's it — migrations create the tables and the app's database role automatically
on first deploy.

> If the deploy logs ever show a permission error creating the `meter_app`
> role, run this once in Neon's SQL editor, then redeploy:
> `CREATE ROLE meter_app LOGIN;`

## Step 2 — Deploy the app (Render)

1. Sign up at **render.com** and connect your GitHub (`costlyinfra-admin/Meter`).
2. **New → Blueprint** → pick the repo. Render reads [`render.yaml`](../render.yaml)
   and proposes the `meter` web service. Click **Apply**.
3. When prompted, fill the three secrets (these are `sync:false` in the blueprint):
   - `DATABASE_URL` → the Neon string from Step 1
   - `APP_SECRET_KEY` → your generated key
   - `METER_APP_DB_PASSWORD` → your chosen app DB password
4. Deploy. Render builds the Docker image (web + API), runs migrations on start,
   and gives you a URL like `https://meter-a1b2.onrender.com`. Open it — you
   should see the login page. 🎉

> That `onrender.com` hostname is fixed when the service is created and does
> **not** change if you later rename the service — only recreating it would. It
> stays reachable alongside your custom domain, so pick a service name you are
> happy to keep.

## Step 3 — Your subdomain (Render + Cloudflare)

1. In Render: **Settings → Custom Domains → Add** `meter.costlyinfra.com`.
   Render shows you a target (the `CNAME` value for your service).
2. In **Cloudflare** → your `costlyinfra.com` zone → **DNS → Add record**:
   - **Type:** `CNAME`
   - **Name:** `meter`
   - **Target:** the value Render gave you
   - **Proxy status:** **DNS only** (grey cloud) — see the note below
3. Wait a few minutes. Render auto-issues a free HTTPS certificate, and
   **https://meter.costlyinfra.com** goes live. Your `www` site is untouched.

> **⚠️ Cloudflare proxy + certificates.** Leave the record on **DNS only (grey
> cloud)** at first. If you turn Cloudflare's proxy on (orange cloud) before
> Render has issued its certificate, it can block the validation and you'll get
> SSL errors. Once Render shows the domain as **Verified / certificate issued**,
> you *may* switch the record to **Proxied (orange cloud)** for Cloudflare's CDN/
> protection — but if you do, set Cloudflare **SSL/TLS → Overview → Full (strict)**
> so it talks to Render over HTTPS. Simplest path: just keep it **DNS only**.

## Step 4 — Scheduled ingest & alerts (GitHub Actions, free)

The daily job ([`.github/workflows/ingest.yml`](../.github/workflows/ingest.yml))
pulls fresh cost data and then **evaluates alert rules** and dispatches
notifications. In GitHub: **Settings → Secrets and variables → Actions → New
repository secret**, add the three core secrets:

- `DATABASE_URL`, `APP_SECRET_KEY`, `METER_APP_DB_PASSWORD`

For email alerts (via [Resend](https://resend.com)) and clickable links in
notifications, also add these **optional** secrets:

- `RESEND_API_KEY` — a Resend API key
- `ALERT_EMAIL_FROM` — a verified Resend sender (e.g. `alerts@costlyinfra.com`)
- `APP_BASE_URL` — your app's base URL (e.g. `https://meter.costlyinfra.com`)

Without them, in-app / Slack / webhook alerts still work; email is reported as
"unconfigured" (never a fake success).

The same job also carries on any test Meter was running (a prompt-rewrite or
model test) that a restart interrupted and nobody has looked at since. For it
to carry on tests judged by **Meter's own model**, add the same three settings
the web service has: `METER_DISCOVERY_BASE_URL` and `METER_DISCOVERY_MODEL` as
repository **variables**, and `METER_DISCOVERY_API_KEY` as a **secret**. They
are optional: without them those tests simply wait, and carry on the moment
someone opens them in the app. It runs daily; you can also trigger it
anytime from the **Actions** tab (**Scheduled ingest & alerts → Run workflow**).

## Step 5 — First use

- Go to your URL and **create an account** — that's a fresh, empty tenant.
- Walk onboarding: connect GitHub + a provider, review features, confirm.
- On the dashboard, **Add cost data** to sync inference and import a build-cost CSV.

Want a populated demo instead? You can seed the demo tenant by running, with your
Neon `DATABASE_URL` exported locally: `make db-seed` (login `demo@costlyinfra.com` /
`meter-demo`). The demo tenant ("Acme Security") ships with 8 features and
~2 years of monthly build/inference history.

**One-click reset (recommended).** Two GitHub Actions handle the demo without a
local DB — both need the `DATABASE_URL` repo secret:

- **Seed demo data** — creates the demo tenant if it doesn't exist (no-op if it
  already does). Safe, non-destructive.
- **Reset demo data** — **wipes** the demo tenant and rebuilds it fresh with the
  latest dataset. Use this to refresh the demo after dataset changes. It deletes
  only the demo tenant (via `ON DELETE CASCADE`); no other tenant is touched.

Run either from the repo's **Actions** tab → pick the workflow → **Run workflow**.

---

## Connect with GitHub (optional, recommended)

Customers can connect GitHub two ways on **Features**: paste a personal access
token, or choose **Connect with GitHub**, sign in on github.com and pick the
organization and repositories Meter may read. The second needs Meter to be
registered with GitHub once, as a **GitHub App**. Until it is, the button is
simply not shown and the token form works as before.

Why an App and not "Sign in with GitHub": a GitHub sign-in token that can read
private repositories also gets permission to *write* to them. An App's
permissions are fixed by you, read-only, and GitHub enforces them.

**Register it (once, about ten minutes):**

1. On GitHub: your profile picture → **Settings → Developer settings → GitHub
   Apps → New GitHub App** (or the same under your organization's settings, so
   the App belongs to the company rather than a person).
2. **Name:** e.g. `Meter by CostlyInfra` (this is what customers see).
   **Homepage URL:** your app's URL.
3. **Callback URL:** `https://<your app>/github/callback`
   — and tick **Request user authorization (OAuth) during installation**.
   Meter needs this: it is how it proves the person really owns the
   organization they are connecting (the installation number GitHub sends
   back can be faked; the sign-in cannot).
4. Tick **Redirect on update**. Leave **Expire user authorization tokens** on.
5. **Webhook:** untick **Active**. Meter does not use webhooks.
6. **Permissions** — everything else stays "No access":
   - Repository → **Pull requests: Read-only**
   - Repository → **Metadata: Read-only** (GitHub requires it)
   - Organization → **GitHub Copilot Business: Read-only** — only if customers
     should be able to sync Copilot seats this way
7. **Where can this GitHub App be installed?** → **Any account**.
8. Create it. On the page that follows, note the **App ID** and **Client ID**,
   click **Generate a new client secret**, and under **Private keys** click
   **Generate a private key** (a `.pem` file downloads). The **slug** is the
   last part of the App's public URL, `github.com/apps/<slug>`.

**Then set five environment variables** on the web service (Render →
Environment), and anywhere else Meter reads GitHub on a schedule:

| Variable | Value |
|---|---|
| `GITHUB_APP_ID` | The App ID |
| `GITHUB_APP_SLUG` | The slug from `github.com/apps/<slug>` |
| `GITHUB_APP_CLIENT_ID` | The Client ID |
| `GITHUB_APP_CLIENT_SECRET` | The client secret |
| `GITHUB_APP_PRIVATE_KEY` | The whole `.pem` file. If the field is one line, replace line breaks with `\n` |

All five are needed; with any missing, the option stays hidden. Meter stores
only the installation's number for each customer, and asks GitHub for a fresh
one-hour read-only token each time it reads. A customer can change which
repositories Meter sees, or remove it, from GitHub at any time — the
connection card links there.

## For customers installing the metering hook (optional)

They point the SDK at:
`https://meter.costlyinfra.com/api/hook/events`
using the ingest token from **POST `/api/hook/token`** (offered in onboarding).

## Environment variables (reference)

| Variable | Where | Purpose |
|---|---|---|
| `DATABASE_URL` | Render + GitHub secrets | Neon connection (owner/admin role) |
| `APP_SECRET_KEY` | Render + GitHub secrets | Encrypts stored credentials — **keep stable** |
| `METER_APP_DB_PASSWORD` | Render + GitHub secrets | Password for the RLS-enforced app DB role |
| `METER_SECURE_COOKIES` | set to `true` in prod (blueprint default) | Secure session cookie over HTTPS |
| `METER_STATIC_DIR` | set by the Docker image | Tells the API to also serve the web app |
| `METER_ADMIN_EMAILS` | Render (comma-separated) | Unlocks the internal Admin Portal for these accounts |
| `METER_DISCOVERY_BASE_URL` | Render (blueprint default) | OpenAI-compatible endpoint for feature discovery and the in-app assistant |
| `METER_DISCOVERY_MODEL` | Render (blueprint default) | Model id on that endpoint |
| `METER_DISCOVERY_API_KEY` | Render (secret) | Key for that endpoint — **without it both features degrade** |
| `RESEND_API_KEY` | GitHub secrets (optional) | Resend API key — enables email alert delivery |
| `ALERT_EMAIL_FROM` | GitHub secrets (optional) | Verified Resend sender address for alert emails |
| `APP_BASE_URL` | GitHub secrets (optional) | App base URL for deep links in alert notifications |
| `GITHUB_APP_ID`, `GITHUB_APP_SLUG`, `GITHUB_APP_CLIENT_ID`, `GITHUB_APP_CLIENT_SECRET`, `GITHUB_APP_PRIVATE_KEY` | Render (optional) | "Connect with GitHub" — see the section above |

## The answering model (discovery + "Ask Meter")

Two features share one model: **feature discovery**, which clusters pull-request
metadata into proposed features, and the in-app **Ask Meter** assistant.

The blueprint pins the endpoint and the model, because neither is a secret and a
deploy should not depend on remembering them. Only the key is filled in:

```
METER_DISCOVERY_BASE_URL = https://api.groq.com/openai/v1   (in render.yaml)
METER_DISCOVERY_MODEL    = openai/gpt-oss-120b              (in render.yaml)
METER_DISCOVERY_API_KEY  = gsk_…                            (Render dashboard → Environment)
```

Get the key from [console.groq.com/keys](https://console.groq.com/keys). Set it
on the Render service under **Environment**, then redeploy — env changes do not
take effect until the service restarts.

> Groq retired `llama-3.3-70b-versatile` for Free and Developer plans in August
> 2026, which is why the default is now `openai/gpt-oss-120b` (131k context).
> Any OpenAI-compatible endpoint works: point the base URL at OpenAI, OpenRouter,
> Together, or a local Ollama and set the model to match.

**What happens with no key.** Nothing breaks, and nothing pretends to work.
Discovery falls back to a deterministic branch-name heuristic — free, offline,
and noticeably blunter. The assistant falls back to a fixed summary of the
tenant's own data: it can still say which runs have gone quiet and how stale
each feed is, but it cannot hold a conversation, explain a mechanism, or answer
"what caused yesterday's spike". The assistant's header says which mode it is in.

**Whose money.** This is Meter's own key, used for every tenant. It is
deliberately separate from the per-tenant BYOK configuration in Settings, which
is scoped to that tenant's own discovery runs — billing a customer's key for a
support conversation would be a surprise.

## Internal Admin Portal

The admin portal (onboard and support customers without touching the database) is
served by the **same** Render service — no extra infra and no extra hostname.

Set **`METER_ADMIN_EMAILS`** on the Render service to your admin accounts,
comma-separated. Those people sign in with their normal account and the portal
unlocks for them; for everyone else the API returns 403 and the entry point is
not rendered. No schema change, no self-service, nothing to grant in the app.

> **The allowlist is the control, not the URL.** The portal is part of the same
> single-page app, so its routes are in the JavaScript bundle every visitor
> downloads — they are discoverable by anyone who looks, and treating the path as
> a secret would be a false sense of safety. What actually gates it is the
> server-side check on every `/api/admin/*` route: no session is 401, a session
> that is not on the allowlist is 403. Keep the allowlist short, and remove people
> from it when they no longer need access.

## When you outgrow free
- **No more cold starts:** upgrade the Render service to a paid instance (~$7/mo).
- **Bigger/faster DB:** raise the Neon plan (same connection string).
- **Migrate to AWS** (the design-doc target) later — it's standard Postgres + a
  container, so nothing here locks you in.
