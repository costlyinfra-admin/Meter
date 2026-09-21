# sdk/ — Metering hook (M7)

The optional precision tier (design doc §7.2). A *thin* wrapper around the
customer's AI work that emits an agent run and the steps inside it — the LLM
calls, retrievals, tool calls — with identity, counts and timing. Cost is
computed **server side** from Meter's pricing tables; the SDK never sees prices,
and it never sends prompt or response text.

**Invariant:** the hook is a precision upgrade, never a requirement. Onboarding
and first value work with connectors alone; with no ingest URL/token configured,
every SDK call is a no-op.

- [`python/`](python) — `costlyinfra_meter` (stdlib only). `meter.wrap(client)`
  for the one-line route, `meter.agent(...)` for a run with named steps.
- [`node/`](node) — `costlyinfra-meter` (no deps, Node ≥ 20). The same surface:
  `meter.wrap(client)` and `await meter.agent(...)`.

Both are **fail-safe**: reporting is fire-and-forget and never raises into the
caller, so a metering outage can't break the customer's app.

## Wiring it in

**Recommended — `wrap()` the client once, no per-call code.** Every completion
call is then metered automatically (with latency); your call sites don't change.

Python:

```python
from costlyinfra_meter import Meter

meter = Meter(application="support-agent")                  # rest from ENV
client = meter.wrap(anthropic_client, feature_id="feature-threat-triage")

resp = client.messages.create(model="claude-sonnet-4-6", ...)   # metered automatically
```

Node:

```js
import { Meter } from "costlyinfra-meter";

const meter = new Meter({ application: "support-agent" });        // rest from ENV
const client = meter.wrap(openai, { featureId: "feature-threat-triage" });

const resp = await client.chat.completions.create({ model: "gpt-4o", ... }); // metered
```

Provider is auto-detected. Pass an optional `metadata` (e.g. `{ environment,
customer_id }`) for extra attribution.

**Named steps — when one run does several things.** Use this when a request is
an agent run rather than a single call, or when you would rather not wrap the
client: each step is timed and priced on its own, and the run ties them together.

```python
from costlyinfra_meter import Meter

meter = Meter(application="support-agent")
with meter.agent("resolve-ticket", feature_id="feature-threat-triage") as run:
    run.llm("classify", lambda: anthropic_client.messages.create(...))
    docs = run.tool("retrieve-documents", retrieve_documents)
    run.llm("generate-answer", lambda: anthropic_client.messages.create(...))
```

```js
import { Meter } from "costlyinfra-meter";

const meter = new Meter({ application: "support-agent" });
await meter.agent("resolve-ticket", { featureId: "feature-threat-triage" }, async (run) => {
  await run.llm("classify", () => anthropic.messages.create(...));
  const docs = await run.tool("retrieve-documents", retrieveDocuments);
  return run.llm("generate-answer", () => anthropic.messages.create(...));
});
```

`run.tool`, `run.retrieval`, `run.embedding`, `run.guardrail` and `run.evaluation`
name the other kinds of step. A step that throws is recorded as failed and the
exception is re-raised unchanged.

## Optimize mode (opt-in)

Pass `optimize=True` (Python) / `{ optimize: true }` (Node) to additionally emit
**privacy-safe optimization signals** — salted-hash fingerprints and counts that
let Meter find repeated requests and uncached repeated prompt prefixes instead
of guessing from rules of thumb. Off by default.

```python
from costlyinfra_meter import Meter

meter = Meter(application="support-agent", optimize=True)
client = meter.wrap(
    anthropic_client,
    feature_id="feature-threat-triage",
    customer_id="northwind-financial",   # optional; see "Scope" below
)
```

```js
import { Meter } from "costlyinfra-meter";

const meter = new Meter({ application: "support-agent", optimize: true });
const client = meter.wrap(anthropic, {
  featureId: "feature-threat-triage",
  customerId: "northwind-financial",     // optional; see "Scope" below
});
```

Applies to `wrap()`ed clients. A named step (`run.llm(...)`) is given a function
to call, not a request to look at, so there is nothing there to fingerprint.

**What it sends.** For each call, a salted SHA-256 of the whole request and a
salted SHA-256 of its static head, plus counts and token totals. Two signals come
out of that: `duplicate` (this exact request was seen before) rides on the span
the call was already sending, and `prefix` (many calls share a large static head)
is a bounded counter map flushed as its own span every 60 seconds.

**Scope — `customer_id` / `cache_scope`** (`customerId` / `cacheScope` in Node). Two identical requests are only
interchangeable inside whatever boundary your application would actually reuse a
response across. Pass one and repeats are compared within it, so two customers
sending the same prompt are never reported as one avoidable call. Pass neither
and the repeat is still counted, but marked `unscoped` — Meter will not suggest
the second call could have been served by the first, because nobody has said the
two belong to the same user, tenant or cache. Neither value ever reaches the
provider's API, and neither is transmitted: both are hashed into the
fingerprint.

**Window.** A repeat counts only if it happens within `optimize_window` seconds
(`optimizeWindowMs` in Node), default **10 minutes**, measured from the first
call of a group and never extended by later ones. Beyond that, "you could have
served the first answer" is a claim about freshness that neither the SDK nor
Meter can make.

**What it finds, and what that is worth.** The repeat count is exact. Whether
those repeats were *avoidable* is not measured — that depends on freshness,
authorization, deliberate sampling, external state and your own policy — so
Meter presents the result as a **ceiling** ("Repeated request candidates"), not
as a saving, and prices it at list rate.

**Coverage is per process.** The comparison map lives in memory in one SDK
instance. A second replica, a restart, or a service in the other language starts
empty and cannot see the first occurrences the other one saw, so real repeats
across them are **missed**. Treat the number as a floor on repetition.

**What it never sends.** Prompt text, response text, tool arguments, tool
results, or metadata. Nothing in this path reads a message's content except to
hash it, and the salt is per-tenant and server-held, so a fingerprint cannot be
dictionary-attacked or cross-referenced between tenants. The SDK fetches that
salt once (GET `/api/hook/salt`, same ingest token) **on a thread of its own** —
never in front of your call — and if it cannot get one it emits no signals at
all rather than an unsalted hash.

**Measured, and only where it is.** (Prefix caching.) Where the provider reports
the real size of a cached prefix (Anthropic's `cache_creation_input_tokens`), that is the number
Meter prices and the finding is labelled measured. Where it does not, the SDK
estimates the prefix from request length and says so, and the finding drops to
medium confidence — an estimate is never presented as a measurement.

Both collectors are memory-bounded (an LRU of request fingerprints, a capped
prefix map), all the work is off the request path, and every failure path is
silent: optimize mode can cost you a signal, never a call.

## Config

| Env var                  | What it is                                            |
|--------------------------|-------------------------------------------------------|
| `METER_INGEST_URL`   | e.g. `https://meter.example.com/api/hook/events`  |
| `METER_INGEST_TOKEN` | the per-tenant ingest token (POST `/api/hook/token`)  |

## Tests

```bash
make test-sdk    # python (pytest) + node (node --test)
```
