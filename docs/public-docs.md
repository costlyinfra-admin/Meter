# Public documentation — costlyinfra.com/docs

The public docs are **generated from the handbook that ships inside Meter**. There is
one source of truth — `web/src/help/content.ts` — and three readers of it:

| Reader | What it does with the content |
| --- | --- |
| **Knowledge Base** (`/help` in the app) | Renders it as React, with live router links |
| **Ask Meter** | Retrieves matching passages in the browser and sends them to the assistant |
| **Public docs** (this) | Renders it as static HTML for `costlyinfra.com/docs` |

Nothing in the generator writes back into the handbook, so publishing cannot change what
a logged-in reader sees. Write a topic once, and it appears in all three.

## Build it

```bash
make docs          # or: cd web && npm run build:docs
```

Output lands in `web/dist-docs/` (gitignored, rebuilt from empty each time):

```
index.html                       → /docs/
<category>/index.html            → /docs/<category>/
<category>/<topic>/index.html    → /docs/<category>/<topic>/
docs.css
sitemap.xml
```

Options, as flags or environment variables:

| Flag | Variable | Default |
| --- | --- | --- |
| `--out` | — | `web/dist-docs` |
| `--base` | `DOCS_BASE` | `/docs` |
| `--origin` | `DOCS_ORIGIN` | `https://costlyinfra.com` |
| `--app` | `DOCS_APP_ORIGIN` | `https://meter.costlyinfra.com` |

## How it is put together

- **`web/src/docs/publish.ts`** — all of the rendering, as pure functions: content in,
  files out. Typed, linted and tested like the rest of the app.
- **`web/scripts/build-docs.mjs`** — the part that cannot be pure: reads arguments,
  writes files. It loads the TypeScript through Vite, so the generator reads the same
  source the app does rather than a compiled copy that can go stale.
- **`web/src/help/routes.ts`** — the in-app routes the handbook may link to. Shared by
  the generator and by `content.test.ts`, so the two cannot disagree.

**The pages carry no JavaScript.** Public documentation has to be readable by crawlers —
search engines, and the assistants people now ask about a tool before trying it — and a
client-rendered page serves those an empty shell. It also means there is no search box:
every page carries the full contents list instead, which at ~60 topics is navigable.

### Links

The one thing that genuinely reads differently outside the app:

| In the handbook | On the public site |
| --- | --- |
| `[Confidence](/help/concepts/confidence)` | `/docs/concepts/confidence/` |
| `[Connect sources](/cost-sources)` | `https://meter.costlyinfra.com/cost-sources`, marked `↗ — in the Meter app` |
| `https://…` | unchanged, `target="_blank" rel="noreferrer"` |
| anything else | **the build fails** |

App links are labelled because they lead to a sign-in screen, and an unannounced one
reads as a broken docs link. The build failing on an unhandled link is deliberate: a
docs site rots by emitting dead links quietly, and `web/src/docs/publish.test.ts` runs
the real handbook through the generator on every CI run, so a bad link fails the web
suite before anyone publishes it.

### What gets published

Every topic. The handbook describes how the product works, not what is in any tenant's
account, so there is nothing to hold back. If a topic ever does need holding back, add a
flag to `Topic` and skip it in `entries()` — the in-app path ignores fields it does not
read.

## Deploying

The generated directory is static files; any static host serves it. `costlyinfra.com` is
not in this repository, so the last step is wherever that site is hosted:

1. **Cloudflare Pages (recommended).** A project whose build command is
   `cd web && npm ci && npm run build:docs` and whose output directory is `web/dist-docs`.
   Add a route so `costlyinfra.com/docs/*` resolves to it.
2. **Alongside the marketing site.** Run `make docs` and copy `web/dist-docs/` into that
   site's `docs/` directory as part of its own build.

Either way, keep it off `meter.costlyinfra.com`: that service holds authenticated tenant
data, and on Render's free plan it sleeps when idle — public docs should not 503 because
nobody has logged in for an hour.

Add `Sitemap: https://costlyinfra.com/docs/sitemap.xml` to the marketing site's
`robots.txt`. The generator does not write a `robots.txt`, because that file belongs to
the site root and is not ours to overwrite.

## Not included

- **Search.** Would need either JavaScript and a shipped index, or a server. The contents
  list covers navigation; revisit if the handbook outgrows it.
- **Ask Meter in public.** Retrieval already runs in the browser, but
  `/api/assistant/chat` is session-bound, rate-limited per tenant and spends real model
  budget. Exposing it publicly is an abuse-handling decision, not a docs one.
