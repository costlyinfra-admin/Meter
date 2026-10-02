# Spec — Test before you change: experiments behind every recommendation

> **Status:** draft for review, 2026-10-01. Direction decided by the founder on
> 2026-10-01 (§11); nothing is built yet. It extends [`optimization-opportunities-spec.md`](optimization-opportunities-spec.md)
> (the Copilot, §17–§22) and generalises the replay evaluation that
> [`prompt-optimization-spec.md`](prompt-optimization-spec.md) §7 already ships
> for one lever. The founder's decisions are recorded in §11.

## 0. In plain language

Today Meter finds an opportunity, prices it, and tells the customer how to
check it ("run a quality eval on a sample before switching"). Then the
customer is on their own until they mark it applied and Meter watches the bill.

This adds a **"Test this"** button to each recommendation. It sets up a test
of the change on the customer's own data — before anyone ships it — and feeds
the result back into the recommendation: the saving is re-measured, the
confidence goes up or down, and a change that hurt quality is marked as tried
and failed, not quietly dropped.

There are three ways to test, from cheapest and safest to most convincing:

1. **Simulate** — work out what would have happened from data Meter or the SDK
   already sees. No prompts, no model calls, no cost.
2. **Offline test** — run real or representative inputs through the current
   setup and the proposed one, then compare cost, speed and quality. Costs some
   tokens and runs before anything ships.
3. **Live experiment** — send a small share of real traffic to the change and
   compare the two groups. This is the most convincing test, and the change is
   live while it runs.

The single most important design choice: **by default, tests run on the
customer's side and only numbers come back to Meter.** Meter's promise is
that it never holds prompt or response text, except through the consented,
encrypted prompt-capture channel. A testing feature that needed customers to
send their prompts to Meter would break that promise for everyone who hasn't
opted in.

## 1. What exists today (inspected 2026-10-01)

| Piece | Where | What it gives this feature |
|---|---|---|
| Unified opportunity shape | `optimize_measured.py` `_unify_measured`, opt spec §18 | One record per recommendation: `lever`, `savings_type`, `confidence`, `confidence_reason`, `validation_guidance`, `verification`, `status`, `trail`. The experiment hangs off this. |
| Savings taxonomy | opt spec §18 | `measured` / `modeled_ceiling` / `directional`, never summed together. An experiment's job is mostly to move a number between these honestly. |
| Per-lever validation text | `_LEVER_GUIDANCE` | Plain-text "how to validate" for each lever. Each one becomes the starting point for that lever's test. |
| Prove loop | `_actions`, opt spec §20 (amended) | `applied → measured → verified`, where *verified* needs the bill's cost per unit to have fallen. Experiments sit **before** `applied`. |
| Replay evaluator | `prompt_eval.py`, migration 0054 | A working, tested engine: replay K inputs through two variants on the customer's own provider key, measure tokens/latency/cost with `pricing.py`, deterministic checks, pairwise judge in both orders, Meter's own decision rule, per-org monthly cap ($25), approval before spend, encrypted per-case content. **Only for prompt rewrites, only for consenting features, Anthropic + OpenAI only.** |
| Consented content channel | `prompt_capture.py`, prompt spec §3–§5 | The only way prompt text may reach Meter: double key (org consent + SDK flag), per feature, encrypted, 30-day retention, crypto-shred on withdrawal, audit. |
| Content-free traces | `traces.py` `FORBIDDEN_FIELDS`, `ai_trace` / `ai_span` (0049) | Per-call cost, tokens, latency, status, `prompt_id` / `prompt_version`, `release_version`, `environment`. Enough to compare two groups of live traffic on **cost, speed and errors** without any content. |
| SDK optimize signals | opt spec §4.1 | The SDK fingerprints requests and prefixes **client-side** and sends only monthly aggregates. The server cannot replay timing, so any cache simulation has to run in the SDK. |
| Alerts | `alerts.py`, `alerts_eval.py` | Rules, incidents, delivery. Reusable for "the live experiment is hurting error rate — stop it". |
| Hosting | `docs/deploy.md` | One Docker web service (Render) plus a GitHub Actions cron. **No job queue.** `prompt_eval` runs on a daemon thread inside the web process, which is fine for a 30-case run and fragile beyond that. |

What is **missing**: an experiment record that is not tied to a prompt; any
test for the other levers; a way for results computed elsewhere to come back;
any notion of "these calls were the test group"; and rules for how a test
result changes savings and confidence.

## 2. Principles

1. **Numbers come back, content stays home.** A test run outside Meter returns
   numbers, enums and bounded identifiers only, through a closed schema that
   rejects anything else (the `FORBIDDEN_FIELDS` approach). Running a test
   inside Meter is allowed only through the consented prompt-capture channel.
2. **Quality and cost are two axes, never one number.** No "net value" score
   that trades dollars against quality points. This mirrors invariant 2 (build
   vs inference).
3. **A test changes confidence; only the bill verifies.** A passing test can
   raise a recommendation's confidence and replace its estimate with a
   measured per-unit figure. It can never mark a saving *verified*: that stays
   the Prove loop's job (invariant 5).
4. **A failed test is evidence, not deletion.** The recommendation stays
   visible as "tested — did not hold", with the result attached, and drops out
   of the totals. This is invariant 4's spirit applied to recommendations.
5. **Meter's rule decides, not the judge.** As in `prompt_eval.decide`: models
   and customer metrics produce scores, and a deterministic, published rule
   turns them into pass / fail / inconclusive.
6. **The customer pays for tests knowingly.** Every test that spends tokens
   shows an estimate first, needs approval, and respects the monthly cap. The
   tokens it spends are real inference cost and must show up on the feature as
   such (§7.5).
7. **Reuse, don't build an eval framework.** Meter's value is joining a test
   result to dollars, confidence and the Prove loop. Running test cases and
   scoring answers is a solved problem in open source (§6).

## 3. The flow

```
Recommendation ──"Test this"──▶ Experiment (draft)
                                   │  pre-filled from the opportunity:
                                   │  control = today's setup, candidate = the fix
                                   ▼
                           Customer fills the gaps (§5)
                                   │
              ┌────────────────────┼──────────────────────┐
              ▼                    ▼                      ▼
          Simulate           Offline test           Live experiment
       (SDK dry-run,     (customer-side runner,   (customer's flag tool
        no tokens)        or Meter replay with     splits traffic; SDK tags
                          consent)                 calls with the group)
              └────────────────────┼──────────────────────┘
                                   ▼
                    Results: cost/unit, tokens, latency, errors
                             per group, + quality scores
                                   ▼
                  Meter's decision rule → passed / failed / inconclusive
                                   ▼
          Back into the recommendation: savings re-measured, confidence,
          validation state, priority → applied → Prove loop verifies on the bill
```

An experiment is a small state machine: `draft → ready → running →
completed (passed | failed | inconclusive) | cancelled`. Only one is active per
opportunity at a time. Re-testing after a code change is a new experiment, and
history is kept.

## 4. By optimization type

The risk differs by lever, so the test differs too. For some levers the
question is **"does quality hold?"**; for others quality is not at risk and the
question is **"does the saving really happen?"**.

| Lever | What could go wrong | Cheapest useful test | Strongest test | Needs content? |
|---|---|---|---|---|
| **Cheaper provider** (`provider_switch`) | Hosts can serve the "same" weights at different precision (quantisation), with different context limits, rate limits or latency. | Offline smoke test: ~30 cases through both hosts; deterministic checks + latency. | Live canary at 5–10% on the new host; compare error rate, latency p95, cost/call. | Offline: yes (customer-side). Live: no. |
| **Model right-sizing** (`model_rightsizing`) | Quality drops on the harder share of requests. The headline risk. | Offline eval on the customer's test set or consented samples: deterministic checks + customer metrics + pairwise judge; non-inferiority rule. | Live A/B with the customer's quality signal (task success, thumbs, downstream metric) per group. | Offline: yes. Live: no (scores are numbers). |
| **Prompt caching** (`prompt_caching`) | None on quality: the provider guarantees the same output. The risk is that the cache **doesn't hit** (prefix not byte-identical, traffic too sparse for the TTL), and cache writes cost more. | **Simulate** in the SDK: per prefix fingerprint, the gap between calls, bucketed against the provider's TTLs (5 min, 1 h). Gives a predicted hit rate and net saving. | Live: turn caching on for one group; `cache_read_tokens` per group gives the real hit rate and saving. | No. |
| **Repeated requests** (`duplicate_calls`) | Serving a cached answer that was stale, or meant for a different user. The detector's own guidance names these risks. | **Simulate** in the SDK: would-hit counts at candidate TTLs, with the key scoped as the customer chooses (global / per `customer_ref`). | **Shadow cache:** the app still calls the model, but also looks up the cache and records *would have hit* plus whether the cached and fresh answers matched (exact or customer comparator). Only the counts are reported. | No (the comparison happens client-side). |
| **Prompt rewrite** (`prompt_optimization`) | Quality drops. | Already built: PO-4 replay (consented). Add: customer test cases and customer-side runs. | Ship as a new `prompt_version` to a share of traffic; compare groups. | Offline: yes. Live: no. |
| **Output reduction** (directional) | Truncated or too-terse answers. | Offline: candidate `max_tokens` / brevity instruction on samples; measure output tokens and quality. Turns a directional symptom into a measured figure. | Live A/B on the `prompt_version`. | Offline: yes. |
| **Context reduction** (directional) | Lost facts when retrieval context shrinks. | Offline, customer-side, with RAG metrics (faithfulness, context recall). | Live A/B. | Offline: yes. |
| **Semantic caching** (directional) | Near-duplicate questions get a wrong cached answer. | SDK dry-run with the customer's embedding + threshold: would-hit rate and false-hit rate on a labelled sample. | Shadow cache as above. | Yes, client-side only (embeddings of content). |
| Billing findings (`optimize_billing.py`: unattributed, concentration, growth…) | — | Not experiments; these are data and governance actions. No "Test this". | — | — |

A useful consequence: **the two cheapest experiments need no content at all.**
Simulating caching and repeated requests needs only an SDK dry-run mode that
counts, so it fits Meter's existing promise with no new consent. These are good
first candidates (§10, EX-1).

## 5. What the customer provides

Most of the experiment is pre-filled from the recommendation. The customer
supplies only what Meter cannot know.

| Input | When it's needed | Pre-filled from | Notes |
|---|---|---|---|
| Control and candidate | Always | The opportunity (current model/host/prompt version → proposed one) | Editable, e.g. a different target tier than Meter suggested. |
| Test data | Offline | (a) consented captured samples (if any); (b) the customer's own test set in their repo; (c) synthetic cases they generate | Meter never asks for (b) to be uploaded. The runner reads it locally. |
| Success criteria | Offline, live | Lever defaults: deterministic checks + "not worse beyond a 10% margin" (`prompt_eval.LOSS_MARGIN`) | Which metric is primary; the non-inferiority margin; latency and error guardrails. |
| Quality signal | Live (for right-sizing, rewrites) | None | A numeric score per call or per session from their own system (task success, thumbs, eval score), sent as a number keyed by trace id. |
| Traffic split and duration | Live | 10% for 7 days, or until a minimum count per group | Assignment is done by the customer's flag tool, not Meter. |
| Provider key that can make calls | Meter-hosted offline only | Existing eval keys (`prompt_eval_key`) | Not needed for customer-side runs, which use the customer's own client. |
| Budget | Anything that spends tokens | Monthly evaluation cap ($25 today) | Per-experiment estimate and approval, as PO-4 does. |
| Cache policy | Caching simulations | Lever defaults | TTL candidates and key scope (global / per customer). |

## 6. Reusing open-source tools

Meter should be the place where test results meet dollars, not another eval
framework. Three roles, with a recommendation for each. *(Licences and
maintenance status below are from memory and must be re-checked before any
dependency is taken.)*

### 6.1 Running offline tests on the customer's side

- **Recommended: a thin `meter test` runner in the existing SDKs, plus result
  importers.** The runner reads an experiment spec from Meter (control,
  candidate, criteria; no content), runs the customer's cases through their
  own client and keys, scores them, and uploads aggregates. For customers who
  already run evals, it **imports** results from the tools below instead of
  re-running anything.
- **promptfoo** (MIT): YAML test configs, many providers, assertions including
  `is-json`, `llm-rubric`, `cost` and `latency`, and JSON output. Meter can
  *generate* a promptfoo config from an experiment (control vs candidate as two
  providers or prompts) and read its output file. Best fit for teams without
  an eval habit. Check its current ownership and licence before depending on it.
- **Inspect AI** (MIT, UK AI Security Institute): Python eval framework with
  solvers/scorers and structured logs. A good importer target for more
  engineering-heavy teams.
- **DeepEval** (Apache-2.0): pytest-style test cases and metrics (G-Eval-style
  rubrics, relevancy). Importer target.
- **Ragas** (Apache-2.0): RAG metrics (faithfulness, context precision/recall).
  The natural scorer for **context reduction**.
- **MLflow evaluate** (Apache-2.0) and **Langfuse** (MIT core) datasets and
  experiments: many customers already store eval runs here. Import scores
  only, never their traces (which hold content).
- **The judge is the customer's** (decision 5). A customer-side run that needs
  answers compared uses a model the customer chooses, on the customer's key.
  Meter supplies the judge instructions and the both-orders protocol from
  `prompt_eval.py` and applies its own decision rule to the verdicts, but
  never offers a Meter-hosted judge for these runs: that would mean receiving
  the answers.
- Not recommended as a base: **OpenAI Evals** (largely unmaintained), and
  **lm-evaluation-harness** (public benchmarks, which say little about a
  customer's own feature).

### 6.2 Running offline tests inside Meter (consenting customers only)

- **Extend `prompt_eval.py`, don't replace it.** Generalise the variant from
  "a different system prompt" to "a different system prompt, model or host",
  which covers right-sizing and provider switch with the engine, judge,
  decision rule, cap and audit already built and tested.
- **Hosts:** most open-weight hosts (Together, Fireworks, Groq, DeepInfra)
  speak the OpenAI chat-completions format, so an `openai_compatible` variant
  with a base URL covers provider switch without a new library. **LiteLLM**
  (MIT) would cover far more providers, but it is a large dependency in the
  path that holds customer keys and content. Defer it until a customer needs a
  provider the small adapter can't reach.

### 6.3 Live experiments

- **Traffic assignment: the customer's flag tool, never Meter.** The
  **OpenFeature** standard (Apache-2.0, CNCF), and tools like **GrowthBook**
  (MIT core) or **Unleash** (Apache-2.0), already split traffic. Meter only
  needs each call tagged with which group it was in (§8.2).
- **Quality scores over OpenTelemetry:** Meter already accepts OTLP
  (`otel.py`). OpenTelemetry's GenAI conventions have been adding an
  evaluation-result event (a name, a numeric score, a label); if it has
  stabilised, accept it on the existing route with numbers only.
- **Statistics:** written in plain Python, as the backend has no statistics
  dependency. The few tests needed are sign / binomial tests on paired
  verdicts, Wilson intervals, and a bootstrap interval for cost per call.
  GrowthBook's documentation on sequential testing is a good reference for not
  stopping a live test the moment it looks good.

### 6.4 Cache simulation

- **No server-side tool needed.** It is a counting mode in the SDK's existing
  LRU (opt spec §4.1). **GPTCache** or **RedisVL** (both MIT) are reasonable
  for the semantic-cache dry-run, on the customer's side, using the customer's
  embeddings.

## 7. How results feed back into Meter

### 7.1 A new state on every opportunity

Add `validation` alongside `status`:

`untested` · `simulated` · `tested_offline` · `tested_live` · `failed` · `inconclusive`

plus `experiment_id` of the latest completed experiment. The card shows it as a
badge ("Tested on 412 live calls, 3 Sep") that links to the result.

### 7.2 Savings

| Result | What happens to the figure |
|---|---|
| Simulated | Replaced by the simulated figure (e.g. predicted hit rate × priced cost). Savings type unchanged. |
| Passed offline | Replaced by **measured per-call delta × the feature's real monthly volume**, like prompt spec §9. Moves to **`tested`**. |
| Passed live | Per-call delta measured on real traffic × real volume. Moves to **`tested`**. |
| Failed | Excluded from totals (like `overlaps`). Shown as "tested — did not hold", with the reason. |
| Inconclusive | Figure unchanged; the reason is shown (e.g. "too few cases"). |

**`tested` is a fourth savings type** (decision 2), alongside `measured`,
`modeled_ceiling` and `directional`, and like them it is totalled on its own
and never summed with the others. It sits between a model and a measurement:
the per-call delta was measured, on a sample or on a share of real traffic, but
the full saving has not happened yet. An opportunity leaves `tested` the way
every other one does: applied, then *verified* on the bill by the Prove loop.
A `measured` opportunity that is tested (e.g. prompt caching) keeps its
`measured` type and gains the test as evidence; `tested` is for figures that
were a ceiling or a direction before the test.

### 7.3 Confidence

A deterministic step, with its reason written into `confidence_reason`:
passed live → `high`; passed offline → one step up (capped at `med` for
right-sizing, which a sample cannot fully clear); failed → the opportunity
leaves the ranked list; inconclusive → unchanged. Because
`priority_score = savings × confidence weight × effort weight` (§19), a tested
recommendation rises in the ranking on its own, with no new formula.

### 7.4 Evidence trail

Invariant 3 says every number must be explainable. The experiment's summary
becomes trail entries on the opportunity: groups, counts, cost per call,
latency, score intervals, decision rule and outcome. Per-case detail is kept
only as numbers, plus encrypted content where it came through the consented
channel.

### 7.5 The test's own cost

Tokens spent testing are real inference spend, and they will appear on the
provider's bill. They must land **on the feature, labelled as testing**, not in
Unattributed and not hidden. One way to do it: the runner and the Meter-hosted
replay tag their calls `environment = 'experiment'`; dashboards count it as
inference (it is) and show it separately, so the experiment page can say "this
test cost $3.10 and points to $410/month" (decision 3). Because it is real
money on the bill, it counts toward the budget and the forecast like any other
inference — an assumption, since the decision covered attribution only; a
one-off test is small next to a month's spend, and leaving it out would put the
budget out of step with the invoice. *Needs checking:* where PO-4 replay
spend lands today. It runs on a separate eval key, so it may currently fall
into Unattributed.

### 7.6 The Prove loop gets a prediction to check

When a tested change is marked applied, the experiment's predicted cost per
unit is frozen with the action. *Verified* still requires the bill (§20,
amended), and Meter can now also report **how close the prediction was**. Over
time that is a calibration figure for Meter's own claims, and a strong trust
signal for a CFO buyer.

### 7.7 Elsewhere

- **Alerts:** a live experiment registers guardrail rules (error rate or p95
  latency in the candidate group above the control group by X) on the existing
  alert subsystem, so a bad canary pages someone.
- **Copilot Overview:** a fourth figure, **"tested"**, kept separate from
  measured, modeled and verified (decision 2).
- **Assistant / MCP:** experiment summaries are numbers, so they can be read
  like other figures. Content never is.

## 8. Architecture changes

### 8.1 Data (sketch, all tenant-isolated with RLS like every table)

- `experiment`: tenant, feature, lever, frozen opportunity snapshot (projection,
  confidence), mode (`simulate` / `offline` / `live`), where it runs
  (`customer` / `meter`), control and candidate as **typed fields** (provider,
  model, prompt_version, cache TTL; no free text), criteria, status, decision,
  decision reason, created by, timestamps.
- `experiment_arm_result`: per group: n, cost per unit, tokens in/out, latency
  p50/p95, error rate, quality score mean with interval, check failures.
- `experiment_case_result` (offline): numbers and verdicts per case, keyed by
  a **hash** of the customer's case id. Content only via the existing encrypted
  prompt tables when the run happened inside Meter with consent.
- `prompt_evaluation` becomes one kind of experiment, or links to one. Keep its
  tables; don't migrate them away.

### 8.2 SDK

- **Group tagging:** `meter.wrap(client, feature_id=…, experiment="<id>",
  group="control"|"candidate")`. The server checks that the id belongs to the
  tenant and that the group is one of the two values, then stores them on
  `ai_span`. A closed vocabulary, so it isn't "arbitrary customer metadata".
- **Dry-run counters** for caching and repeated-request simulation, sending
  bucketed counts only.
- **`meter test`** runner and importers (§6.1).

### 8.3 Ingest

`POST /api/experiments/{id}/results`: a closed schema (numbers, enums, hashed
ids), checked against `FORBIDDEN_FIELDS`. It uses a **per-experiment,
write-only token** (like the ingest token, scoped to one experiment), so a CI
job holds no broader power. The tenant comes from the token, never the payload.

### 8.4 Running Meter-hosted tests reliably

Today's daemon thread inside the web service won't survive a restart or the
free tier's sleep. Before Meter-hosted tests grow beyond about 50 cases, add a
**Postgres-backed job table** (claimed with `FOR UPDATE SKIP LOCKED`, resumable
per case), worked by the existing cron or a small worker process. No new
infrastructure service. Customer-side runs don't need this, which is another
reason they are the default.

## 9. Decision rules (defaults, all published)

- **Offline, quality at risk** (right-sizing, rewrites, output/context
  reduction): today's PO-4 rule. No deterministic regressions; worse ≤ better +
  10% of n; n ≥ 20. With customer metrics: the candidate's mean is not below
  control by more than the margin, with 80% confidence.
- **Offline, quality not at risk** (provider switch): deterministic checks
  only, plus latency within the guardrail. n ≥ 20.
- **Live:** a minimum n per group (by default 500 calls or 7 days, whichever is
  later); cost per call lower with the interval excluding zero; error rate and
  p95 latency within guardrails; quality not worse beyond the margin if a
  quality signal exists. Results are not shown as a decision before the
  minimum is reached.
- **Customers may change the rules** (decision 4). Tightening is unremarkable.
  Loosening — a wider margin, a smaller minimum n — is allowed, and the result
  says so wherever it appears: "passed under a relaxed rule (20% margin)". The
  default rule and the one actually applied are both stored on the experiment.
- **Simulated:** no pass/fail on quality (not applicable); it reports a
  predicted figure with the assumptions used (TTLs, key scope).

## 10. Suggested milestones

Each ends with a review, the house rhythm.

- **EX-1 — Experiment record, "Test this", and content-free simulation.** The
  `experiment` table and API; the button and experiment page; the validation
  state, savings and confidence rules (§7.1–7.3); SDK dry-run counters for
  prompt caching and repeated requests. No content, no tokens, no new consent.
  *Why first:* it proves the feedback loop end to end, at zero privacy and cost
  risk.

  ✅ **Built (2026-10-01).** What is true today, and where it differs from the
  plan above:

  - **What can be tested.** Repeated requests (the customer picks how old a
    cached answer may be — 1 minute, 10 minutes, 1 hour or 24 hours — and
    whether only same-customer repeats count) and prompt caching (5-minute vs
    1-hour cache, offered only where the provider sells both).
  - **Counters.** SDK 2.4.0 (Python and Node, identical) sends a totals-only
    simulation summary and a 1-hour cache-write count (`usage_simulation`,
    `usage_signal.cache_windows_1h`, migration 0065). The 10-minute count
    agrees with the duplicate detector by test. **2.4.0 is not published.**
  - **An experiment stores the customer's setting.** The card's figure is
    recomputed each month under it; the experiment keeps what it found when
    it ran (`experiments.py`). A passed simulation replaces the figure and
    keeps the savings type; a failed one keeps the card, out of every total
    and the ranking; under 200 calls it waits.
  - **Deferred to EX-2:** the `tested` savings type and its Overview figure.
    A simulation never produces one, and an always-empty card would only
    confuse.
  - **Ordering rule added:** results are layered on after reconciliation, so
    an applied fix keeps being measured against the figure it was applied on;
    a replaced figure is still bounded by the bill.
  - **Only on the feature page.** The Copilot Overview shows the recomputed
    figures and leaves out failed ones, but has no test badge yet.
  - **Gap found, not fixed here:** the caching detector prices only the
    5-minute cache, so where that would lose money it recommends nothing —
    even when the 1-hour cache would save. There is then no card to test, in
    exactly the case the 1-hour test matters most. The fix is in the detector
    (price both lifetimes, recommend the better), not in testing.
- **EX-2 — Customer-side offline tests, model right-sizing first** (decision 6).
  The results endpoint with scoped tokens; `meter test` with built-in replay
  for right-sizing, then provider switch and output reduction once the loop
  has been reviewed on right-sizing; promptfoo config generation and importers
  (promptfoo, Inspect, DeepEval); test-cost attribution (§7.5).

  ✅ **Built for model right-sizing (2026-10-01).** What is true today, and
  where it differs from the plan above:

  - **The loop.** "Test this" on a right-sizing card: choose the model to
    replace and a cheaper same-vendor one (the recommendation's target by
    default), and the rule's two dials. Meter issues a one-time token
    (`experiment_token`, hashed, 14 days, single use, revoked on cancel or
    replacement) that can fetch that test's spec and post its results — and
    nothing else. Results are a closed, numbers-only shape (`experiment_case`,
    migration 0066); Meter prices the tokens and applies the rule
    (`offline_tests.py`).
  - **The runner** is `meter-test` in the **Python SDK 2.5.0** (`testing.py`,
    standard library only), not `meter test`: `run` replays cases with the
    customer's keys and judge (both orders), `import promptfoo` and
    `import inspect` read those tools' results, `--dry-run` prints what would
    be sent. Formats were checked against promptfoo's type definitions and
    Inspect's log docs. **2.5.0 is not published. Node has no runner yet.**
  - **Not built: the DeepEval importer** — its saved-run format is
    undocumented beyond "the schema Confident AI uses" and records no per-case
    token usage or model, so it could never supply a test's cost. **Not
    built: promptfoo config generation** — the run page gives the import
    command instead; a generated config can follow if customers ask.
  - **Providers.** Anthropic and OpenAI. Gemini models are right-sized but not
    offered a test the runner cannot run.
  - **`tested` savings type** (decision 2): a fourth figure everywhere — its
    own Overview card once non-zero, the ranking, by-feature, MCP tools, and
    the identified-savings sums on the budget panel and Forecast. A
    recommendation covering several models is built part by part, each from
    the latest finished test of that model, and becomes `tested` only when
    every part has been tested and at least one held.
  - **Test spend** (decision 3) is priced from the run's reported tokens,
    judge included, and shown on the feature and the result as testing. It is
    never added to inference: the calls ran on the customer's keys, so they are
    already in the provider's bill, and adding them would count them twice.
  - **Not yet:** provider switch and output reduction (after this review).
- **EX-3 — Live experiments.** SDK group tagging; per-group comparison from
  `ai_span`; quality-score ingest (SDK and OTel); guardrail alerts; the live
  decision rule.

  ✅ **Built for model right-sizing (2026-10-02).** What is true today, and
  where it differs from the plan above:

  - **Tagging.** SDK 2.6.0 (Python and Node): `wrap(client, experiment=...,
    group="control"|"candidate")` tags completed and failed calls;
    OpenTelemetry spans use `meter.experiment_id` / `meter.experiment_group`.
    Only a running live test of the same organization can be named; anything
    else is dropped without costing the batch its spend (migration 0067).
  - **Quality scores.** `meter.score(experiment, group, value)`, a number on
    any scale, kept as daily totals per group. The quality margin is relative
    to the current model's average, so the scale does not matter.
    **Not built: scores over OpenTelemetry** — the convention's
    `gen_ai.evaluation.result` is a log record, Meter has no OTLP logs
    receiver, and the convention is still in development.
  - **The rule** (`live_tests.py`): no verdict before the minimum calls per
    group AND the minimum days (default 500 and 7); guardrails stop it early
    once each group has 100 calls (error rate +1 point, slowest 5% +25%);
    cost per *successful* call lower at 95%; quality not lower than the
    margin at 95% where 30+ scores per group exist. All dials loosenable,
    and a loosened rule is said.
  - **Guardrail alerts.** Each live test registers a system-managed
    `experiment_guardrail` rule (in-app), turned off when the test passes or
    is cancelled, kept on when a guardrail was breached. It cannot be edited
    or duplicated from the alert form. **Routing it to Slack or email is a
    follow-up.** Running tests are re-checked when their page is opened and
    by a scheduled step before the alert evaluation — **which runs daily, slow
    for a canary; hourly is a hosting decision.**
  - **Result.** A pass is tested at high confidence (`tested_live`); each
    model's part of a right-sizing figure follows its latest finished test,
    offline or live.
  - **Not yet:** live tests for caching and repeated requests (need a
    cache-aware cost per call, which spans do not carry).
- **EX-4 — Meter-hosted offline tests for consenting customers.** Generalise
  `prompt_eval` variants to model and host; OpenAI-compatible hosts; the job
  table (§8.4).

  ✅ **Built for model right-sizing on the same provider (2026-10-02).** What
  is true today, and where it differs from the plan above:

  - **The job table** (`eval_job`, migration 0068; `jobs.py`). A run that
    makes model calls inside Meter is written down first, with the captured
    samples it will replay. Each finished case is stored as it completes, so
    a resumed run skips what is done (and storing a case twice is refused by
    the database, not just avoided). A lease, renewed after each case, says
    who holds it; a lapsed lease is taken over **when anyone opens the run**,
    or by a new daily step (`python -m meter.jobs`, 10-minute budget). A run
    abandoned three times is stopped. **Prompt-rewrite evaluations (PO-4)
    moved onto it** — same behaviour, now resumable, decided from the stored
    cases rather than from memory. No new service: the web process and the
    cron are the workers, as §8.4 proposed.
  - **Hosted model tests** (`hosted_tests.py`). `runs_at = 'meter'` on an
    offline right-sizing test. Meter replays up to 100 captured calls to the
    current model (each with its own system prompt) on the current and the
    cheaper model, with the organization's evaluation key; the same checks;
    the judge is the model the prompt-capture consent names (the PO-4 judge),
    both orders. The same numbers-only `experiment_case` rows and the same
    rule (`offline_tests.conclude`); `results_source = 'meter'`. Answers are
    not kept. A refused key (401/403) stops the run as inconclusive rather
    than failing the cheaper model; other provider errors count against the
    call, as on the customer's side. A run stopped part-way decides nothing.
  - **Money.** Estimated from the captured calls' tokens (both models, the
    cheaper one assumed to write as much); held to the PO-4 monthly cap of
    $25, now **shared** by both kinds of hosted run. A running run counts its
    estimate; a finished or cancelled one, what it spent. Cost is counted
    per case as it is spent, so a cancelled test still shows on the feature
    as testing. **The judge's calls are not in the figure** (PO-4 never
    counted them either); the page says so.
  - **Consent.** The prompt-capture terms named replaying examples to test
    *prompts*. They now also name model tests (`CONSENT_VERSION`
    2026-10-02). Because that adds a use and collects nothing new, capture
    carries on under the earlier terms (`CAPTURE_SINCE`); **only hosted model
    tests wait** until someone agrees to the new words (`MODEL_TESTS_SINCE`).
    Start and finish are in the prompt audit log.
  - **The daily step** runs with Meter's judge only if the workflow has the
    `METER_DISCOVERY_*` settings (docs/deploy.md). Without them it skips runs
    whose consent names Meter's model, rather than mistaking a missing
    setting for withdrawn consent; those carry on when opened.
  - **Not built: testing a model on another provider or host.** A captured
    call records which client made it, not which host served it, and model
    names differ between hosts and Meter's price book, so Meter cannot yet
    replay a call faithfully elsewhere or name the right model there. Needs
    a host-aware capture and a model-name mapping first.
  - **Not built:** keeping hosted answers for review (they would need the
    same purge paths as captured samples); counting the judge's spend;
    Gemini. Where hosted replay spend lands on the bill is still the §7.5
    question — it runs on the evaluation key, outside the SDK, so it is
    likely Unattributed.
- **EX-5 — Prediction vs outcome.** Freeze the prediction at apply time; report
  the calibration in the Prove loop and the Overview.

## 11. Decisions (founder, 2026-10-01)

| # | Question | Decision |
|---|---|---|
| 1 | Where tests run by default | **On the customer's side**, returning numbers only. Meter-hosted runs only for features with prompt-capture consent. |
| 2 | What a passed test does to the saving | A new **`tested`** savings type, totalled separately from measured, modeled and verified (§7.2). |
| 3 | Where test spend shows | **On the feature, as inference, labelled as testing** (§7.5). |
| 4 | Can customers loosen the decision rules | **Yes**, and a loosened rule is recorded and shown on the result (§9). |
| 5 | Who judges customer-side runs | **The customer**, with a model and key of their choosing; Meter supplies the protocol and applies its rule (§6.1). |
| 6 | First lever after EX-1 | **Model right-sizing** (§10). |

**Still open:**

- Whether test spend should count toward budgets and forecasts. Assumed yes,
  because it is on the invoice (§7.5).
- Where PO-4 replay spend lands on the bill today (§7.5). To check before EX-2.

## 12. Risks

- **Test cases unlike production.** An offline pass on an easy test set
  overstates safety. Mitigations: show where the cases came from; prefer
  consented samples or a live test for the final word; keep offline passes as
  `modeled_ceiling`.
- **Judge bias.** LLM judges prefer longer answers and their own model family.
  Mitigations: both-orders judging (already built), deterministic checks first,
  and the customer's own metrics winning over the judge where both exist.
- **Peeking at live tests.** Stopping early on a good-looking result produces
  false wins. Mitigation: minimum n and duration before any decision is shown.
- **Scope creep into an eval platform.** Mitigation: Principle 7. Every new
  scoring capability should be an importer before it becomes Meter code.
