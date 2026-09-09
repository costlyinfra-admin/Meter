# costlyinfra-meter

Request-level AI economics for [Meter](https://github.com/costlyinfra-admin/Meter).

It answers questions a monthly total cannot: why did *this* agent run cost
$1.42, which step spent it, which agents are running right now, and did last
week's prompt change make every run more expensive.

No dependencies. Node 18+.

## What it never sends

Prompts, responses, messages, tool arguments, tool results, retrieved
documents, error messages and stack traces.

Not truncated, not redacted, not behind a setting — the events this SDK can
construct have no field for them, and the server rejects a payload that carries
one. What travels is identity, counts, timing and money.

## Install

```bash
npm install costlyinfra-meter
```

```bash
export METER_INGEST_URL="https://your-meter/api/hook/events"
export METER_INGEST_TOKEN="…"          # Install SDK page -> Generate token
export METER_APPLICATION="support-agent"
export METER_ENVIRONMENT="production"
export METER_RELEASE_VERSION="2026.9.1"   # optional
```

With no URL or token configured every call is a no-op, so importing this into a
test suite or a local script costs nothing and needs no conditionals.

## One model call

```js
import { Meter } from "costlyinfra-meter";

const meter = new Meter({ application: "support-agent", environment: "production" });
const client = meter.wrap(anthropic, { featureId: "answer-generation" });

const response = await client.messages.create({ model: "claude-sonnet-4-6", messages });
```

The wrapped client behaves exactly like the original. Each call becomes its own
one-span trace; you never mention traces.

If your client is behind a wrapper, name the provider outright:
`meter.wrap(client, { provider: "anthropic" })`.

## A multi-step agent

```js
const result = await meter.agent(
  "resolve-ticket",
  { featureId: "ticket-resolution", customerId: "customer-123" },
  async (run) => {
    const classification = await run.llm("classify", () => anthropic.messages.create(...));
    const documents      = await run.tool("retrieve-documents", retrieveDocuments);
    return run.llm("generate-answer", () => anthropic.messages.create(...));
  },
);
```

Each step resolves to whatever your function resolved to. Latency, status,
tokens, model and provider are recorded automatically. A rejection fails the
step and the run and is re-thrown unchanged — its message is never transmitted.

Besides `llm` and `tool` there are `retrieval`, `embedding`, `guardrail` and
`evaluation`.

Prompt identity, when you version prompts:

```js
await run.llm("generate-answer", call, { promptId: "answer-ticket", promptVersion: "5.0" });
```

## Long-running agents

A run sends a heartbeat every 30 seconds so a slow-but-healthy agent is not
mistaken for a hung one. Heartbeats stop the moment the run ends, and the timer
is `unref`'d so metering can never hold your process open.

Meter derives "stale" at read time from that activity. A stale run has **not**
failed: it may still finish, and it does so normally when it does.

## Queues and workers

```js
// producer
await meter.agent("resolve-ticket", {}, async (run) => {
  await queue.send({ traceContext: run.exportContext() });
});

// worker
await meter.resume(job.traceContext, async (run) => {
  return run.tool("process-document", processDocument);
});
```

Both processes write to one trace. The exported context carries identifiers
only — no token, no customer content: it is designed on the assumption that
anything on a queue eventually gets logged.

## Delivery

Recording appends to an in-memory queue and returns. Batches are posted off your
call path, chained rather than concurrent so one slow request cannot fan out.
If Meter is down or misconfigured, your agent is unaffected.

Bounded: a capped queue (10,000 events) that sheds oldest-first when full, with
the count on `meter.dropped`. Metering degrades visibly; your application does
not.

A batch keeps one id across retries, so a retry after an ambiguous timeout
(server committed, response lost) is recognised as a replay rather than doubling
a run's cost.

Await `flush()` before a short-lived process exits:

```js
await meter.flush();
```

## Upgrading from 0.x

1.0 replaces per-call cost reporting with traces. `meter.record(...)` and the
`record*` helpers are gone; use `meter.wrap(...)` for a single call and
`meter.agent(...)` for a workflow. The constructor now takes an options object
rather than a feature id, `METER_TOKEN` is now `METER_INGEST_TOKEN`, and
`METER_APPLICATION` is new.
