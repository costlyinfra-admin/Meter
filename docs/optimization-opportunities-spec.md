# Spec — Measured optimization & the AI Cost Optimization Copilot

**Status:** Part I (§1–§16) built through M-opt-8. Part II (§17–§27) is the
Copilot roadmap, added 2026-07-19 (see the changelog, §27).
**Scope of this doc:** **Part I** — the measured (not heuristic) optimization
foundation: detectors, pricing, ingest, reconciliation. **Part II** — repositions
that foundation as an **AI Cost Optimization Copilot** (five pillars: Observe,
Detect, Recommend, Optimize, Prove) and defines the next milestones, without
redesigning the architecture.
**Relationship to existing work:** replaces nothing. The heuristic estimator in
[`optimize.py`](../backend/meter/optimize.py) stays as the zero-instrumentation
*directional* tier; the measured tier sits above it; the Copilot is the layer on top.

---

## 1. Why

Today's "Optimization opportunities" are fixed percentages applied to a feature's
monthly usage totals (see `optimize.py`: 10% of premium spend, 12% of input cost,
etc.). They point at the right *levers* but the dollar figures are rules of thumb,
not evidence. Goal: turn "features shaped like this usually have room" into
"**this feature made 1,240 duplicate calls last month costing $312 — here's the
fix**," where every number is measured and every dollar is computed from the
price book, never guessed.

## 2. Principles (inherit the product's invariants)

1. **Grounded, not guessed.** Every opportunity is backed by a measured signal
   (a count, a token total) and priced with `pricing.py` — never a flat %.
   Same bar as the rest of Meter: *no black-box numbers* (invariant 3).
2. **Metadata only, never content.** We capture *shapes* of calls (hashes,
   counts, token sizes), never prompt or response text. Hashes are salted
   per-tenant so they can't be dictionary-attacked or cross-referenced.
3. **Reconcile, don't trust** (invariant 5). A projected saving becomes a
   *realized* saving only after the user applies it and we measure the actual
   month-over-month delta. Projections are labelled as projections until then.
4. **Never break the caller.** The SDK's existing contract holds — all new work
   is off the request path, bounded memory, fail-safe.
5. **Connector-only still gets value.** Customers who won't install the SDK get
   a weaker-but-real tier from fields the provider bill already reports.

## 3. What this first slice detects

Two detectors, chosen because they are **exactly measurable** and **privacy-safe**:

| Detector | What it proves | Why it's real (not a %) |
|---|---|---|
| **Duplicate calls** | The same request was sent N times in a period | The (N−1) repeats are avoidable; savings = repeats × the call's real priced cost |
| **Cacheable prompt prefix** | Many calls share a large identical prefix that isn't cached | Savings = repeated prefix tokens × (input rate − cached-read rate), from the price book |

Explicitly **out of scope for this slice** (future tiers, §12): model-downgrade
recommendations backed by evals, code/call-site analysis, semantic (near-dup)
caching, LLM-written recommendations.

## 4. Data captured (SDK, metadata-only)

The metering SDK ([`sdk/python/costlyinfra_meter`](../sdk/python/costlyinfra_meter))
gains an **optional** optimization mode (`Meter(..., optimize=True)`, default
off). When on, for each recorded call the SDK computes locally:

| Field | Meaning | How derived |
|---|---|---|
| `request_fp` | fingerprint of the full normalized request | `sha256(tenant_salt + model + normalized(messages))`, hex |
| `prefix_fp` | fingerprint of the cacheable static prefix | `sha256(tenant_salt + system_prompt + tool_defs)` when present; else hash of the first `prefix_chars` (default 2 000) |
| `prefix_tokens` | representative size of that prefix | provider-reported prompt tokens for the static part, else `len(prefix)/4` estimate |
| `cache_read` | were input tokens served from cache? | from the provider response usage (`cache_read_input_tokens` > 0) |
| `was_retry` | is this a retry of a failed call? | caller passes it, or SDK wraps a retry helper (later) |

**Only the hashes and counts leave the process.** Never the prompt. `tenant_salt`
is a per-tenant secret fetched once with the ingest token, so hashes are useless
to anyone without it.

### 4.1 The SDK aggregates client-side (keeps volume + storage bounded)

To avoid shipping millions of per-call events and storing every unique request:

- The SDK keeps a small **bounded LRU** (`request_fp → last_seen`, cap ~5 000).
  On a *repeat* within the window it emits a **duplicate signal** — so only
  confirmed duplicates are ever sent. Non-repeats cost nothing and are never
  stored.
- It keeps a tiny **counter map** (`prefix_fp → {count, prefix_tokens,
  cached_count}`) and flushes summaries on a timer (default 60 s) or at cap.
  Distinct static prefixes per feature are few (a handful of system prompts), so
  this is naturally small.

Result: the server only ever stores *duplicates* and *prefix summaries* — both
inherently bounded — not raw traffic.

## 5. Data model

New table (migration **0019** — 0018 was taken by the latency/customer-cost work),
tenant-isolated with RLS like every other table:

```sql
CREATE TABLE usage_signal (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
  feature_id    uuid REFERENCES feature(id) ON DELETE SET NULL,   -- NULL => Unattributed
  provider      text NOT NULL,
  model         text,
  period        date NOT NULL,                                    -- monthly bucket (rest of the system)
  signal_kind   text NOT NULL CHECK (signal_kind IN ('duplicate','prefix')),
  fingerprint   text NOT NULL,                                    -- request_fp or prefix_fp (salted hash)
  call_count    bigint NOT NULL DEFAULT 0,                        -- duplicates: repeats; prefix: total calls sharing it
  prefix_tokens bigint,                                           -- prefix kind only
  tokens_in     bigint NOT NULL DEFAULT 0,                        -- sums, to price a representative call
  tokens_out    bigint NOT NULL DEFAULT 0,
  cached_count  bigint NOT NULL DEFAULT 0,                        -- calls already served from cache
  updated_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, feature_id, provider, model, period, signal_kind, fingerprint)
);
```

Upsert-on-ingest (increment `call_count`, add token sums). Monthly `period`
keeps it consistent with the review-period model. This table holds **signals**,
never cost — cost is always derived at read time from `pricing.py`, so a price
change reprices old opportunities automatically.

## 6. Ingest path

Extend `hook.ingest_events` ([`hook.py`](../backend/meter/hook.py)). Events
may now carry an optional `signal` block:

```json
{ "provider":"anthropic","model":"claude-sonnet-4-6","feature_id":"…",
  "tokens_in":4200,"tokens_out":180,
  "signal": { "kind":"duplicate", "fingerprint":"…", "count":1 } }
{ "provider":"anthropic","model":"claude-sonnet-4-6","feature_id":"…",
  "signal": { "kind":"prefix", "fingerprint":"…", "count":320,
              "prefix_tokens":4100, "cached_count":0, "tokens_in":…, "tokens_out":… } }
```

- Cost accounting is unchanged — a duplicate signal is *also* a normal metered
  call and still lands in the monthly `inference_cost` row (we never
  double-count; the signal is separate bookkeeping).
- Signal blocks upsert into `usage_signal`. Unknown/foreign `feature_id` →
  Unattributed, same rule as today.
- Everything stays best-effort and non-fatal.

## 7. Detectors (the grounded math)

A read-time module `optimize_measured.py` computes opportunities for a feature +
period from `usage_signal` rows, pricing each with `pricing.py`.

**Duplicate calls**
```
repeats      = Σ call_count over ('duplicate') rows for (feature, period)
cost/call    = price(model, avg_tokens_in, avg_tokens_out, provider)   # per model
savings      = Σ_model repeats_model × cost/call_model
```
Evidence: "N duplicate calls across M distinct requests." Confidence **high**
(exact). Framed as "response caching / dedup could avoid these," because some
duplicates are legitimate (idempotent retries, different users asking the same
thing) — we surface the ceiling and let the user judge.

**Cacheable prompt prefix**
```
for each ('prefix') row with call_count C, prefix_tokens P, cached_count K:
  cacheable_calls   = C − K                       # not already cached
  if cacheable_calls ≥ threshold (default 100) and P ≥ 1 000:
    input_rate      = price_in(model, provider)   # $ / token
    cached_rate     = input_rate × CACHE_READ_MULT # e.g. 0.10 for Anthropic
    savings        += cacheable_calls × P × (input_rate − cached_rate)
```
Evidence: "a P-token static prefix repeated across C calls, currently uncached."
Confidence **high** when P and C are large.

Both numbers are computed from measured counts × price-book rates. No fixed
percentages anywhere.

### 7.1 Pricing additions

`pricing.py` gains a small, transparent cache/batch model (versioned like the
rest): per-provider `CACHE_READ_MULT` (Anthropic ≈ 0.10, OpenAI ≈ 0.50 — list
values, kept current; drift shows up as a reconciliation gap, never a silent
wrong number) and a `BATCH_MULT` (≈ 0.50) reserved for the batch-eligibility
detector in a later tier.

## 8. Connector-only complement (Tier A, no SDK)

The provider cost APIs report cache usage the connectors currently discard.
Extend the Anthropic/OpenAI clients ([`providers.py`](../backend/meter/providers.py))
to read `cache_read_input_tokens` / `cache_creation_input_tokens` (Anthropic) and
`cached_tokens` (OpenAI) and store them on the inference row. This gives, without
any instrumentation:

- **Current cache utilization** per feature ("8% of input is cached") — shown as
  context on the opportunity, and
- a floor for the caching recommendation (don't recommend caching what's already
  cached).

It can't *prove* repetition (that needs the SDK), so connector-only caching stays
a **medium-confidence hint**; the SDK upgrades it to a measured number.

## 9. API

```
GET /api/features/{id}/opportunities?range=…    ->
{
  "measured": [
    { "lever":"duplicate_calls", "savings":312.40, "confidence":"high",
      "evidence":"1,240 duplicate calls across 90 distinct requests this month",
      "fix":"Add response caching for identical requests (e.g. keyed on the request hash).",
      "trail":[ {feature_signal-style rows backing it} ] },
    { "lever":"prompt_caching", "savings":880.00, "confidence":"high",
      "evidence":"4,100-token static system prompt repeated across 26,000 uncached calls",
      "fix":"Enable prompt caching (set cache_control on the static system block)." }
  ],
  "estimated": [ …the existing heuristic opportunities, clearly labelled… ],
  "cache_utilization": 0.08
}
```

Follows the review-period range model already built.

## 10. UI (feature drill-down)

On the feature page, split the optimization section into two clearly-labelled
groups:

- **Measured opportunities** (new, on top): each row shows the **lever**, the
  **measured evidence** sentence, the **grounded $/mo** (and /yr), a **confidence
  badge**, and the **specific fix** (one line; later, a file/line when
  code-analysis lands). An expandable evidence trail mirrors the cost evidence
  trail already used elsewhere.
- **Estimated opportunities** (the current heuristic), kept but demoted and
  labelled "directional estimate" so the two are never confused.

Empty state when the SDK isn't installed: a nudge — "Install the metering SDK
with `optimize=True` to turn these estimates into measured, per-call findings."
(Great pull-through for SDK adoption; the SDK stops being just a cost-accuracy
tool and starts *finding money*.)

## 11. Reconciliation loop (projected → realized)

New table `optimization_action(tenant_id, feature_id, lever, applied_on,
projected_monthly)`. When a user marks an opportunity **applied**:

- the projection is frozen with the applied date;
- next period, we compute the feature's actual cost delta for that lever's signal
  (e.g., duplicate count dropped from 1,240 → 40) and show **projected vs
  realized**;
- this both proves ROI to the CFO and tunes future projections.

This is the same "reconcile against reality" ethos as bill reconciliation.

## 12. Later tiers (not in this slice, but the architecture supports them)

- **Batch-API eligibility** — flag async/non-latency-sensitive features → ~50% off.
- **Semantic (near-duplicate) caching** — embed request fingerprints; needs an
  embedding step and a similarity threshold (privacy: embeddings of hashes/short
  normalized forms, opt-in).
- **Code / call-site analysis** — from the existing GitHub connection, read the
  actual `client.messages.create(...)` sites and produce file/line-specific fixes
  ("`triage.py:88` uses Opus with a 4k static prompt and no `cache_control`").
- **Eval-backed model downgrade** — replay a sample of real prompts against a
  cheaper model and judge quality. Highest trust, heaviest lift; requires prompt
  capture → **explicit opt-in** and careful handling.
- **LLM-as-analyst** — hand a model the assembled *measured* evidence + call-site
  facts to write the prioritized recommendation. Guardrail: it may only cite
  numbers we computed; dollars come from `pricing.py`, not the model.

## 13. Milestones & acceptance criteria

- **M-opt-1 — Schema + ingest.** ✅ Done. Migration 0019 (`usage_signal` + RLS);
  extended `hook.ingest_events` to accept `signal` blocks and upsert (duplicate
  rides on the metered call; prefix is a summary that never re-costs). `HookEvent`
  gained a typed `signal` field so the model never strips it. *Accept:* signals
  persist per-tenant; cost accounting unchanged; tenant isolation test passes.
- **M-opt-2 — SDK optimize mode.** ✅ Done (SDK v0.3.0, Python + Node).
  `optimize=True` adds a client-side duplicate LRU + prefix counters + salted
  hashing + timer/cap flush; a per-tenant salt endpoint (`GET /api/hook/salt`,
  migration 0020). All fingerprinting/hashing runs off the call path (background
  thread in Python, deferred in Node); off by default; memory capped; no signals
  emitted without a salt. *Accept:* off by default, zero added latency on the
  call path, memory capped, duplicates and prefix summaries emitted; unit tests
  with a fake transport (Python + Node) pass.
- **M-opt-3 — Detectors + API.** ✅ Done. `optimize_measured.py` (duplicate + prefix
  detectors) + pricing cache model (`CACHE_READ_MULT`, `BATCH_MULT`, `rate_in`,
  `cache_read_mult`) + `GET /api/features/{id}/opportunities`. The heuristic block
  was extracted to `dashboard.heuristic_optimization` and reused as the estimated
  tier. *Accept:* duplicate and prefix savings computed from seeded signals match a
  hand-calc from the price book (tests: 2M dup input @ $3/M = $6.00; 1,000×4,000-tok
  uncached prefix @ $3/M × 0.90 = $10.80).
  - *Deviations from this draft (deliberate):* the endpoint takes `?period=YYYY-MM`
    (monthly, matching the feature drill-down) rather than `?range=`; the response is
    symmetric — `{period, measured:{opportunities,monthly_savings,annual_savings},
    estimated:{…same shape…}, cache_utilization}` — rather than bare lists; prefix
    caching is only claimed for providers with a priced cache discount (Anthropic/
    OpenAI/Google), never OSS hosts we can't price; `cache_utilization` is derived
    from SDK prefix signals for now and will be strengthened by connector cache
    fields in M-opt-5.
- **M-opt-4 — UI.** ✅ Done. The feature page's Optimization section now fetches
  `/opportunities` and renders a **Measured** group (grounded-in-metered-calls tag,
  cache-utilization note, per-lever cards with the evidence sentence, the specific
  fix, a confidence badge, and an expandable evidence trail) above a demoted
  **Estimated** group ("directional estimate"). Empty measured state shows an SDK
  nudge linking to Install SDK. Demo seed adds `usage_signal` rows for AI threat
  triage (3 duplicate fingerprints + a 4,100-token uncached prefix, ~8% cached).
  *Accept:* measured opportunities render with evidence, $ and confidence; demo
  seed includes signal rows; browser-verified.
- **M-opt-5 — Connector cache fields (Tier A).** ✅ Done. The Anthropic/OpenAI cost
  parsers now read cache-read + token fields tolerantly (Anthropic
  `cache_read_input_tokens`, OpenAI `cached_tokens`) into `CostRecord`; migration
  0021 adds `inference_cost.cached_tokens_in`, threaded through `ingest_records`.
  `optimize_measured` computes cache utilization from these connector/hook cache
  tokens (cached input / total input — a floor), falling back to the SDK prefix
  ratio. Demo: Report generator (no SDK) reports cached tokens, so it shows "8% of
  input is already cached" from connector data alone. *Accept:* utilization
  surfaces without the SDK; browser-verified on a signal-free feature.
- **M-opt-6 — Reconciliation loop.** ✅ Done. Migration 0022 (`optimization_action`,
  tenant-isolated). Marking a measured opportunity applied freezes its projection
  with the period; `opportunities` returns an `actions` list where, once past the
  applied period, `realized = projected − the lever's current avoidable spend`.
  API: POST/DELETE `/features/{id}/opportunities/apply`. UI: an "Applied" chip +
  Undo on each measured card and an "Applied optimizations" table showing projected
  vs realized (or "awaiting next period"). Demo: triage's dedup applied Apr 2026 at
  $500/mo → realized $131/mo now. *Accept:* marking applied and advancing a period
  shows the realized delta; browser-verified (apply persists + reconciles).

## 14. Risks & open questions

- **Duplicates aren't always waste** (idempotent retries, distinct users). → Frame
  as a ceiling; require confirmation before counting as realized savings.
- **Hash privacy.** Salting defeats casual dictionary attacks; document the
  threat model. Prompts themselves are never sent.
- **SDK footprint grows** (LRU + timer + salt fetch). Keep it optional, bounded,
  stdlib-only; publish the memory ceiling.
- **Provider cache-field availability varies** and shapes evolve — parse
  tolerantly, same offline-spec caveat as the cost connectors.
- **Volume at very high call rates** — client-side summarization is the mitigation;
  add sampling if a tenant's prefix cardinality is unexpectedly high.
- **Scope vs the build plan.** This is a new workstream beyond the current v1
  milestones (design doc §11 lists trends/anomaly work as later slices). Proposed
  to slot *after* core ship, SDK adoption permitting.

## 15. Opportunity catalog & build roadmap

A structured triage of the broad optimization space (a 120-item industry list was
the input). The filter is the product's own bar, not "is this a real technique":

> **Meter may recommend an optimization only when it can both (a) DETECT it
> from data it actually has, and (b) QUANTIFY the saving from the price book or a
> measured count — never an invented percentage.** Detect-but-can't-quantify → a
> low-confidence *symptom flag*. Neither → it's the customer's engineering team's
> job, and we don't pretend to recommend it.

**Three detection surfaces** (each opportunity is unlocked by one):

- **A — Connector data** (provider cost/usage APIs): spend, tokens, cache tokens,
  request counts, model mix, latency. Ships to every user, no instrumentation.
- **B — SDK optimize signals**: request/prefix fingerprints, cache-read flags,
  per-call token distributions, latency, and (small addition) a session/trace id.
- **C — GitHub code analysis** (call sites): the actual `create(...)` calls —
  models, `max_tokens`, `cache_control`, retry/agent-loop structure. A new
  subsystem; grouped as its own batch.

### 15.1 Catalog (doable only)

Grouped by build status. "Grounded?" = is the dollar figure exact/measured (vs a
directional estimate). Numbers in parentheses reference the 120-item source list.

| Opportunity | Surface | Savings mechanism | Grounded? | Status |
|---|---|---|---|---|
| Duplicate calls / response cache (45, 63, 118) | B | Priced cost of avoidable repeats | Exact | ✅ M-opt-1..4 |
| Prompt / prefix caching (41, 61, 69) | B | Repeated prefix × (input − cached rate) | Exact | ✅ M-opt-3 |
| Cache utilization context (105) | A/B | cached ÷ total input | Measured | ✅ M-opt-5 |
| Cross-provider price arbitrage (40) | A | Same model, cheaper provider — rate delta × spend | Exact | ✅ M-opt-8 |
| Model right-sizing ceiling (31, 32, 33, 116) | A→B | Cost delta to a cheaper tier (quality-gated) | Ceiling | ✅ M-opt-7 |
| Context / output reduction (11, 51, 52) | A | % of input / output cost | Directional | ✅ (heuristic) |
| Semantic caching (62) | A | % of total (volume-flagged) | Directional (low) | ✅ (heuristic) |
| ~~Model downgrade flat %~~ | A | ~~10% of premium spend~~ | — | Superseded by M-opt-7 |
| **Batch-API eligibility (81, 82, 83)** | A + tag | ~50% off async-tolerant spend (`BATCH_MULT`) | Grounded | Deferred (see §22) |
| **Conditional invocation / FAQ-static (85, 111–113)** | B | Calls eliminable (100% on exact-repeat share) | Grounded | Deferred (see §22) |
| **Agent iteration reduction (71, 45, 78, 79)** | B (session id) | Redundant calls per session × priced cost | Grounded | Deferred (see §22) |
| Semantic (near-dup) cache (12, 62) | B + embeddings | Near-dup share × priced cost | Grounded | Batch 2 |
| Session / tool-result cache (66, 67) | B | Repeated tool/session results | Grounded | Batch 2 |
| Multi-model cascade / confidence escalation (33, 34) | B + eval | Share routable to a cheaper model | Ceiling | Batch 2 |
| `cache_control` missing on static prefix (41) | C | Upgrades prefix lever to a file/line fix | Exact | Batch 3 |
| No `max_tokens` / stop sequences (48, 53) | C | Output-token waste at the call site | Grounded | Batch 3 |
| Retry storms (89) | C | Redundant retried calls | Grounded | Batch 3 |
| Recursive reflection / excess iterations (76, 77) | C | Redundant agent calls | Grounded | Batch 3 |
| Redundant CoT / few-shot bloat (7, 8) | C | Input tokens trimmable at the call site | Grounded | Batch 3 |

### 15.2 Explicitly out of scope (why)

A cost-attribution tool can't see these or can't quantify them, so recommending
them would be a black-box number:

- **Inference-engine internals** (42–44, 46, 50 — KV-cache, continuous/dynamic
  batching, speculative decoding, scheduling): invisible from cost data.
- **RAG pipeline internals** (21–30 — chunking, hybrid search, re-ranking,
  embeddings): we see the *result* (high input tokens), never the pipeline. Best
  we do is flag the symptom ("input tokens/call is 3× peers") and hand it off.
- **Prompt-writing craft** (1–6, 9, 10): symptom-detectable, technique not — except
  where surface C spots it.
- **Infra / GPU tuning** (91–99): the customer's platform team, beyond a
  low-pool-utilization flag.

Deferred to other product slices (design doc §11), not this workstream: cost
anomaly detection (106) and budget alerts (107) → Slice 4.

### 15.3 Batch plan (revised — see §17 onward for the Copilot repositioning)

- **Batch 1 — the four core levers (M-opt-7, M-opt-8).** ✅ Done: model right-sizing
  and cross-provider arbitrage, joining the already-shipped duplicate and prompt
  caching. These four are now the *world-class core* the Copilot layer builds on.
- **Batch 2 — the Copilot layer (M-opt-9..13).** Make those four detectors excellent
  and turn findings into *decisions*: a unified opportunity model, deterministic
  prioritization + effort, guidance/validation templates, overlap rules, and a
  Copilot Overview. **Prioritized ahead of new detectors** (see §17–§22).
- **Batch 3 — new measured detectors (M-opt-14..16):** batch-API eligibility,
  conditional/FAQ-static, agent iteration reduction — the three deferred from the
  original Batch 1, now sequenced *after* the core is world-class.
- **Batch 4 — GitHub code-analysis subsystem**: turns measured symptoms into
  file/line fixes (the highest-trust, most actionable tier).
- **Later — deeper detectors** (semantic/tool/session caching, cascades) once SDK
  adoption warrants the extra machinery.

## 16. Batch 1 — the four core levers (M-opt-7, M-opt-8) ✅

The largest dollar levers, on existing surfaces (no new subsystem). Together with
the already-shipped duplicate (M-opt-1..4) and prompt-caching (M-opt-3) detectors,
these are the **four world-class core detectors** the Copilot layer (§17+) builds
on. The three *new* detectors originally sketched here — batch-API eligibility,
conditional/FAQ-static, agent iteration reduction — are **deferred to Batch 3
(§22)**, per the detector strategy: make the core excellent before adding more.

- **M-opt-7 — Model right-sizing ceiling.** ✅ Done. `pricing.py` gains
  `_DOWNGRADE_TARGET` (one step down per vendor: opus→sonnet→haiku, gpt-4o→mini,
  gemini pro→flash→flash-lite) and `downgrade_ceiling()` returning the target + the
  cost-saving *fraction* at the token mix. `optimize_measured._rightsizing_opportunity`
  applies that fraction to the feature's REAL spend (so the ceiling tracks displayed
  cost, not the demo's token counts), med confidence, with a per-model trail. It's a
  measured card but **excluded from the guaranteed "Measured savings" headline** —
  that now sums high-confidence only — and the UI prefixes med/low cards with
  "up to". This *replaces* the flat heuristic "Model downgrade" (removed from
  `optimize.py`; `estimate()` dropped its `premium_cost` arg). Demo: triage's
  sonnet+opus surface a $3,126.67/mo ceiling (opus→sonnet $560, sonnet→haiku
  $2,566.67), headline stays $634. *Accept:* a premium-heavy feature shows a grounded
  downgrade ceiling vs a named target model; browser-verified.
- **M-opt-8 — Cross-provider price arbitrage.** ✅ Done. `pricing.py` gains a
  `_MODEL_FAMILY` map (same open weights under each host's different model id) and
  `cheapest_equivalent()`, which reprices the feature's own token mix at every
  host serving that family and returns the cheapest. `optimize_measured`'s new
  `provider_switch` detector reads the feature's connector rows and surfaces a
  grounded, high-confidence measured opportunity — no SDK needed. UI renders it as
  a "Cheaper provider" card (the Measured tag is now "grounded in measured usage",
  and the evidence trail carries a `together → deepinfra · save $X (N% less)`
  note). Demo: Log enrichment on Together Llama-70B → DeepInfra, $73.20/mo (59%).
  *Accept:* a feature on a non-cheapest host shows the exact saving of the
  lowest-cost provider; browser-verified on a signal-free feature.

---

# Part II — The AI Cost Optimization Copilot

*Added in the roadmap update of 2026-07-19. Part I (§1–§16) is the measured-
optimization foundation and is complete through M-opt-8. Part II repositions that
foundation as a Copilot and defines the next milestones. No architecture is
redesigned; every item below extends existing modules and computes over the
existing schema (`inference_cost`, `usage_signal`, `optimization_action`).*

## 17. Positioning & the five pillars

Meter is repositioned as an **AI Cost Optimization Copilot** — built *on top of*
the cost-attribution product, not replacing it. The existing strengths stay load-
bearing: feature attribution, build vs inference split, connector-first onboarding,
optional SDK precision, reconciliation, privacy-first metadata.

The **North Star** is a five-minute answer to six questions, and every Part II item
must move a customer toward it:

1. *Where is AI money being wasted?* → Copilot Overview (§21)
2. *What should we optimize first?* → deterministic prioritization (§19)
3. *How much could each save?* → measured / modeled-ceiling savings (§18)
4. *Why does Meter believe this?* → evidence trail + confidence reason (§18)
5. *How hard is the fix?* → per-lever engineering effort (§19)
6. *Did it actually work?* → reconciliation, projected → realized → verified (§18, §20)

Organizing frame — **five pillars**, mapped to what already exists:

| Pillar | What it means | Where it lives today |
|---|---|---|
| **1. Observe** | Understand AI cost | build + inference attribution, provider usage (M0–M8) |
| **2. Detect** | Find opportunities from measured evidence | the 4 core detectors (M-opt-1..8) |
| **3. Recommend** | Turn findings into decisions | evidence + `fix` today → unified model + guidance (§18) |
| **4. Optimize** | Support execution | `optimization_action` apply/undo → full status model (§18) |
| **5. Prove** | Projected → realized → verified savings | reconciliation loop (M-opt-6) → verified state (§20) |

Pillars 1–2 are largely built; Part II is mostly Recommend, Optimize, Prove.

## 18. The unified opportunity model (M-opt-9) ✅ Done

Shipped. `GET /features/{id}/opportunities` now returns **one `opportunities` array**
(every measured/ceiling/directional item in a single shape with `savings_type`,
`source`, `confidence_reason`, `title`, `projected_monthly/annual_savings`, `status`,
`evidence`, `fix`, `trail`) plus a `totals: {measured, modeled_ceiling, directional}`
object computed separately. `optimize_measured` gained `_LEVER_META` +
`_unify_measured`/`_unify_directional`; `_measured` returns the raw list and
`opportunities()` builds the unified response. The feature UI partitions by
`savings_type` (measured + modeled-ceiling as cards, directional as the demoted
table); the headline shows "Measured savings $X + up to $Y modeled" (or "Modeled
ceiling up to $Y" when there's no guaranteed savings). Migration 0023 + the API
patterns now allow **all four** measured levers to be applied (fixing a latent 422
on the provider-switch / right-sizing "Mark as applied" buttons). Browser-verified;
backend 162, frontend 31 green.

---

The original design of this milestone, for reference:

Today `GET /features/{id}/opportunities` returns two differently-shaped lists
(measured cards: `lever/savings/confidence/evidence/fix/trail`; estimated rows:
`opportunity/savings/confidence/rationale`) plus an `actions` list. M-opt-9 unifies
them into **one opportunity shape**, computed at read time — **no new tables**.

Fields (compute from existing data; omit what a given lever can't populate):

| Field | Source |
|---|---|
| `title`, `lever` | lever → friendly label map |
| `source` | which surface produced it: `connector` \| `sdk` \| `heuristic` |
| `savings_type` | **`measured`** \| **`modeled_ceiling`** \| **`directional`** (see below) |
| `projected_monthly_savings`, `projected_annual_savings` | detector output × 12 |
| `confidence`, `confidence_reason` | per-lever template (e.g. "exact rate delta on identical weights") |
| `engineering_effort` | per-lever constant — `very_low` \| `low` \| `medium` \| `high` (§19) |
| `implementation_guidance`, `validation_guidance` | per-lever deterministic template (§20) |
| `priority_score` | deterministic formula (§19) |
| `status` | `detected` \| `investigating` \| `planned` \| `applied` \| `verified` \| `dismissed` |
| `realized_savings` | from the reconciliation loop when applied |
| `evidence_trail` | the existing `trail` |

**Savings taxonomy — the single most important rule** (formalizes what M-opt-7/8
already do implicitly via `confidence` + which group):

- **Measured** — observed traffic × price book, guaranteed given the traffic
  (duplicate, prompt caching, provider switch). *Sums into the headline total.*
- **Modeled ceiling** — measured traffic but realization depends on an assumption
  (model right-sizing: quality must hold). **Must render "up to …"** and is
  **excluded from the measured total** (already true today).
- **Directional** — a symptom, not a priced fix (the heuristic tier). **Must never
  contribute to any measured total.**

Making `savings_type` an explicit field (rather than inferring from `confidence`)
is the concrete deliverable. *Accept:* every opportunity across both tiers carries
`savings_type`; the three totals are computed separately and never combined.

## 19. Prioritization & engineering effort (M-opt-10) ✅ Done

Shipped. Each opportunity now carries `engineering_effort` (a per-lever constant:
provider switch `very_low`, prompt caching `low`, duplicate calls `medium`, model
right-sizing `high`; directional levers default `medium`) and a deterministic
`priority_score = savings × confidence_weight × effort_weight` (weights below). The
unified list is ranked by `priority_score`, so an easy guaranteed win outranks a
bigger, riskier, higher-effort one — e.g. the demo now shows Prompt caching (low
effort, $265) above Duplicate calls (medium effort, $369). The feature cards show a
colour-coded effort chip. Browser-verified; backend 163, frontend 31 green.

Answers *"what should we optimize first?"* with a **deterministic, explainable**
ranking — never a black box:

```
priority_score = projected_monthly_savings
               × confidence_weight   (high 1.0 / med 0.6 / low 0.3)
               × effort_weight        (very_low 1.0 / low 0.8 / medium 0.5 / high 0.3)
```

`engineering_effort` is a **per-lever constant**, not a per-instance estimate — the
difficulty is inherent to the fix type, so it's deterministic and defensible:

| Lever | Effort | Why |
|---|---|---|
| Cross-provider switch | Very Low | change a base URL / model id; identical weights |
| Prompt caching | Low | set `cache_control` on the static block |
| Duplicate / response cache | Low–Medium | add a cache keyed on the request hash |
| Model right-sizing | Medium–High | needs a quality eval before switching |

No hour estimates. The confidence weight naturally down-ranks modeled ceilings so a
big-but-risky right-sizing number never dominates a guaranteed switch.

**Non-goal (explicit):** no synthetic "Optimization Score" for a tenant. Real
dollars — measured, modeled-ceiling, verified — are more honest and more useful. A
per-opportunity `priority_score` (a transparent ranking key) is *not* that; the two
must not be confused.

## 20. Guidance, validation & the Prove loop (M-opt-11) ✅ Done

Shipped. Each opportunity now carries `validation_guidance` and `verification` from
a deterministic per-lever template (`_LEVER_GUIDANCE`, with a directional fallback)
— the card shows a "How to apply & verify" expander (Validate / Meter verifies)
alongside the implementation one-liner (`fix`). The **Prove loop** gained the
terminal `verified` state: `_actions` advances pending → measured → **verified**
once the realized drop has held for `_VERIFY_PERIODS` (2) periods with a positive
delta, and the opportunity's `status` follows (detected → applied → verified). The
UI shows a "✓ Verified" badge in the Applied-optimizations table. Demo: triage's
dedup, applied Mar 2026, is now verified ($131/mo realized, held 2 periods).
Browser-verified; backend 165, frontend 32 green.

Every recommendation carries a **deterministic, per-lever template** (no LLM, no
guessed numbers) answering the seven questions:

1. what was observed · 2. why it matters · 3. how savings were calculated ·
4. recommended implementation · 5. engineering effort · 6. validation steps ·
7. how Meter verifies success.

Example (provider switch): *"3.1B tokens on Together for Llama-3.1-70B; DeepInfra
serves identical weights at $0.35/$0.40 vs $0.88 → save $X (rate delta × your
tokens). Point the client's base URL at DeepInfra. Very low effort. Validate: run
your eval suite (weights are identical, so parity is expected). Meter verifies:
next month's provider row shifts to DeepInfra and the reconciliation loop reports
the realized drop."*

The **Prove loop** extends the existing reconciliation (M-opt-6): projected →
realized (already built) → **verified** (a new terminal status once realized savings
hold for N periods within tolerance). *Accept:* an applied opportunity advances
`detected → applied → verified`, and verified savings roll up in the Overview (§21).

## 21. Copilot Overview (M-opt-12) ✅ Done

Shipped. `optimize_measured.copilot_overview()` aggregates the per-feature
opportunity computations (via a shared `_feature_opportunities(conn, …)` helper, so
the Overview and the feature page agree) into: three separate savings figures
(measured / modeled_ceiling / verified), a priority-ranked top-N recommendation
list tagged with each feature, and by-feature / by-lever rollups plus the applied +
verified reconciliations — no new tables. Exposed at `GET /api/copilot/overview`.
New `/optimize` route + "Optimize" nav item render the **Optimization Copilot** page
with three colour-accented KPI cards (never blended), the ranked recommendations
table (linking to each feature), and the two rollups. Demo: $707/mo measured, up to
$5,867/mo modeled (Model right-sizing across 4 features), $1,572/yr verified.
Browser-verified; backend 166, frontend 34 green.

A tenant-level screen — the flagship of the repositioning — answering "where's the
money and what do I do first" across all features at once. A new read-only endpoint
that aggregates the per-feature opportunities already computed; **no new tables**.

Shows, with measured and modeled **kept strictly separate** (never one blended
number):

- **Measured savings identified** (guaranteed) · **Modeled ceiling** ("up to") ·
  **Verified savings** (proven, annualized) — three distinct figures.
- **Top recommendations** across the tenant, ranked by `priority_score`.
- **Opportunities by feature** and **by lever** (where the money and the leverage are).
- **Applied / verified** recommendations and their realized savings.

*Accept:* the Overview renders the three savings figures separately, a ranked
top-N recommendation list, and by-feature / by-lever rollups — all from the existing
opportunity computations.

## 22. Overlap & exclusion groups (M-opt-13) ✅ Done

Shipped. `_apply_exclusions` is a **read-time exclusion rule** (not a graph engine):
within a group present on a feature, the strongest member wins (measured beats a
directional estimate; then higher savings) and the rest are flagged `overlaps =
<winner title>` and dropped from the totals. Applied inside `_feature_opportunities`,
so both the feature page and the Overview dedup identically.

**Repo-reality finding (the interesting part).** The overlaps the draft imagined
between *measured* detectors don't actually double-count today: provider-switch (OSS)
and right-sizing (frontier) are disjoint per-model, and duplicate-calls vs
prompt-caching are ~independent (their true overlap is a tiny slice, so hard-excluding
one would badly *under*-count — worse than the double-count it prevents). The real,
present overlap is a **measured finding superseding the directional ESTIMATE of the
same thing**, so we never show a rough estimate for what we've measured precisely.
Encoded groups: `{prompt_caching, prompt_caching_est}` and `{duplicate_calls,
semantic_caching}`. (The true full overlap `duplicate_calls ⊕ conditional-invocation`
activates when that detector lands — M-opt-15.) UI greys the superseded estimate row
with "· measured as X" and excludes it from the estimated total. Demo: triage's
directional total drops $795.53 → $224.82 (Prompt caching and Semantic caching
estimates superseded); the measured total is unchanged. Browser-verified; backend
167, frontend 35 green. *Accept:* an overlapping member is shown but excluded from the
totals, marked with its superseding lever.

---

The original design of this milestone, for reference:

Prevents double-counting when two levers address the *same* spend. Implemented as
**read-time exclusion groups**, not a graph engine: within a group, keep the
highest-priority member and suppress the rest from the measured total (still shown,
marked "overlaps X").

Current reality first: today overlap is minimal — **arbitrage acts only on hosted
open models, right-sizing only on single-vendor frontier models (disjoint sets)**,
so those two never double-count. The real cases to encode:

- **Duplicate calls ⊕ conditional-invocation/FAQ** — both remove whole calls; the
  same avoidable call must be counted once. (Matters once the deferred
  conditional detector lands, §22 batch.)
- **Duplicate calls ⊕ prompt caching** — a duplicated call's prefix tokens appear in
  both bases; when both fire on the same feature, don't sum them naively.
- **Provider switch ⊕ model downgrade** — both change the model for a spend; pick one.
- **Prompt cache ⊕ prompt compression** — same input-token waste (future).

*Accept:* when two members of an exclusion group fire on one feature, the measured
total counts the winner only; the loser is shown with an "overlaps" note.

## 23. Deferred detectors — Batch 3 (M-opt-14..16)

The three new detectors moved out of Batch 1, now sequenced after the core is
world-class:

- **M-opt-14 — Batch-API eligibility.** ~50% off async-tolerant spend (offline
  reports, bulk enrichment). Detect from a latency-tolerance signal (high volume +
  steady latency, or a user "async" tag); price with the reserved `BATCH_MULT`.
- **M-opt-15 — Conditional invocation / FAQ-static.** 100% on the calls it removes:
  a high exact-duplicate share means those calls can skip the LLM. Grounds out of
  the duplicate detector; in an exclusion group with it (§22).
- **M-opt-16 — Agent iteration reduction.** Many LLM calls per user action (dominant
  for agentic security customers). Add an optional `session`/`trace` id to the SDK,
  flag outlier calls-per-session, price the redundant iterations.

## 24. Future milestones

- **Simulator (M-opt-17).** A what-if calculator over existing pricing + a feature's
  measured token mix: provider switch, model mix, prompt-cache rate, batch API,
  input/output reduction. Reuses `pricing.py` and the feature's usage — **no new
  architecture**. Directly serves "how much could each save?"
- **Cost regression (M-opt-18).** Flag month-over-month regressions from existing
  data: cost/request up, model changed to a pricier one, cache utilization dropped,
  tokens/request grew. **Note:** this overlaps the design doc's deferred *Slice 4
  (trends & anomaly)* — build it as the cost-focused subset of that slice, not a new
  workstream, and only after the Prove loop (§20) is solid. This is also the
  concrete form of the lifecycle's "institutionalized" state — a guardrail that
  catches a verified win from silently regressing.

## 25. Product KPIs

North-star metric: **verified annualized customer savings**. Supporting:

- measured savings identified · modeled-ceiling savings identified
- applied opportunities · verified opportunities
- projection accuracy (projected vs realized) · verified ROI
- SDK adoption · opportunity action rate (detected → applied)

These are all computable from `optimization_action` + the opportunity model; none
requires new infrastructure.

## 26. Architecture impact

Deliberately minimal — Part II is a computation-and-UI layer, not new plumbing:

- **New tables:** none required. The opportunity model, prioritization, guidance,
  overlap rules, and Overview all **compute over** `inference_cost`, `usage_signal`,
  and `optimization_action`. (A `status`/`dismissed` column on `optimization_action`
  is the only plausible schema touch, and only if apply/verify needs more states.)
- **Backend:** extend `optimize_measured.py` (unified shape, `savings_type`,
  `priority_score`, effort, guidance, exclusion groups) and add one aggregation
  endpoint for the Overview. `optimize.py` and `pricing.py` unchanged in shape.
- **SDK:** unchanged until M-opt-16 (optional `session` id).
- **Frontend:** one new Overview screen; the feature drill-down gains effort +
  priority + guidance on the cards it already renders.
- **Explicitly avoided:** microservices, warehouses, queues, vector DBs, a general
  optimization-graph engine, ML ranking, and any LLM-generated financial number.

## 27. Changelog (2026-07-19 roadmap update)

- Repositioned the workstream as the **AI Cost Optimization Copilot** (Part II),
  layered on the existing attribution product — nothing removed.
- Marked **M-opt-7 (right-sizing)** and **M-opt-8 (arbitrage)** done in the catalog;
  noted the flat heuristic "Model downgrade" is **superseded** by M-opt-7.
- Added the **five-pillar** frame (Observe/Detect/Recommend/Optimize/Prove) mapped to
  existing work, and the **optimization lifecycle** (detected → … → verified).
- Added milestones **M-opt-9..13** (unified opportunity model + `savings_type`;
  deterministic prioritization + per-lever effort; guidance/validation templates +
  the verified state; **Copilot Overview**; overlap/exclusion groups).
- **Re-sequenced** the three new detectors (batch, conditional, agent) out of Batch 1
  to **Batch 3 (M-opt-14..16)** — core-detectors-first, per the detector strategy.
- Added **future** milestones: Simulator (M-opt-17) and Cost regression (M-opt-18,
  positioned as the cost subset of the already-deferred Slice 4).
- Added **Product KPIs** (north star: verified annualized savings) and an
  **Architecture impact** statement (no new tables/infra).
- **Explicit non-goal:** no synthetic tenant "Optimization Score" — measured,
  modeled-ceiling, and verified dollars instead. (A transparent per-opportunity
  `priority_score` is separate and retained.)
