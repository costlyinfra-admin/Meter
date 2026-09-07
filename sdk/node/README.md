# costlyinfra-meter (Node)

The optional metering hook for [Meter](https://github.com/costlyinfra-admin/Meter) —
a thin, fail-safe wrapper that reports per-call LLM usage so spend can be
attributed **per feature**. No dependencies (Node ≥ 18). Cost is computed
server-side from Meter's pricing tables — the SDK never sees prices, and it
never sends prompt or response content, only token counts and a `featureId`.

It is **fail-safe**: recording appends to an in-memory queue and returns. A timer
batches and posts; nothing on your call path blocks, throws, or touches the
network. If Meter is down, misconfigured, or asleep, your application is
unaffected. With no ingest URL/token configured, every call is a no-op.

It is also **bounded**: a capped queue (10,000 events) whose oldest entries are
dropped and counted on `meter.dropped` when it fills. Metering degrades; your
application does not. The batching timer is `unref`'d, so it never holds your
process open.

## Install

```bash
npm install costlyinfra-meter
```

ESM only — `import` it. In a CommonJS project use a dynamic import
(`const { wrap } = await import("costlyinfra-meter")`); `require()` will not work.

## Use

**Recommended — wrap the client once (no per-call code):**

```js
import { wrap } from "costlyinfra-meter";

const client = wrap(openai, { featureId: "feature-threat-triage" }); // reads ENV

const resp = await client.chat.completions.create({ model: "gpt-4o", ... }); // metered
```

Provider is auto-detected; each call is recorded with its latency. Pass an
optional `metadata` (e.g. `{ environment, customer_id }`) for extra attribution.
Streaming responses use the explicit form below.

**Explicit — one line per call:**

```js
import { Meter } from "costlyinfra-meter";

const meter = new Meter("feature-threat-triage");
const resp = await openai.chat.completions.create({ model: "gpt-4o", ... });
meter.recordOpenAI(resp);        // <- the whole hook
```

Helpers: `recordAnthropic`, `recordOpenAI`, plus the generic
`record({ provider, model, tokensIn, tokensOut, featureId })`.

### Delivery, `flush()` and retries

Events are sent when a batch fills (50) or after `flushIntervalMs` (5000),
whichever comes first. Each request is bounded by a timeout (5s, `{ timeoutMs }`),
so a sleeping endpoint can never leave requests pending.

A failed batch is retried (3 attempts, backing off ~1s / 4s / 15s with jitter) —
enough to ride out a restart or an instance waking from sleep. A 4xx is *not*
retried: a bad token fails the same way forever. 429 is, since it explicitly asks
you to come back. Retrying is safe because every attempt carries the same
`batch_id`, which the server applies once and then recognises — so a retry after
an ambiguous timeout cannot double-charge a feature.

The SDK drains automatically on `beforeExit`. That does **not** fire on
`process.exit()` or an uncaught throw, so await a flush before a hard exit:

```js
await meter.flush();      // true if the queue drained, false on timeout
await meter.flush(1000);  // never waits longer than you allow
```

Tunable: `batchSize`, `flushIntervalMs`, `queueMax`, `timeoutMs`, `maxAttempts`,
`retryBackoffMs`.

### Optimize mode (opt-in)

`new Meter(featureId, { optimize: true })` additionally emits **privacy-safe**
signals — salted hashes and counts, never prompt text — so Meter can surface
*measured* optimization opportunities (duplicate calls, uncached repeated
prefixes). Off by default; work is off the call path, memory-bounded, and
fail-safe. The SDK fetches a per-tenant salt once (`GET /api/hook/salt`, same
ingest token).

## Config

| Env var                  | What it is                                        |
|--------------------------|---------------------------------------------------|
| `METER_INGEST_URL`   | e.g. `https://app.example.com/api/hook/events`    |
| `METER_INGEST_TOKEN` | the per-workspace ingest token from the dashboard |

## License

Apache-2.0. (The Meter server is AGPL-3.0; this client SDK is permissive so
you can embed it in a proprietary app.)
