# costlyinfra-meter

Request-level AI economics for [Meter](https://github.com/costlyinfra-admin/Meter).

It answers questions a monthly total cannot: why did *this* agent run cost
$1.42, which step spent it, which agents are running right now, and did last
week's prompt change make every run more expensive.

Stdlib-only, no dependencies.

## What it never sends

Prompts, responses, messages, tool arguments, tool results, retrieved
documents, exception messages and stack traces.

Not truncated, not redacted, not behind a setting — the events this SDK can
construct have no field for them, and the server rejects a payload that carries
one. What travels is identity, counts, timing and money: which prompt version,
how many tokens, how long, how much.

## Install

```bash
pip install costlyinfra-meter
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

```python
from costlyinfra_meter import Meter

meter = Meter(application="support-agent", environment="production")
client = meter.wrap(anthropic_client, feature_id="answer-generation")

response = client.messages.create(model="claude-sonnet-4-6", messages=messages)
```

The wrapped client behaves exactly like the original — same arguments, same
return value. Each call becomes its own one-span trace; you never mention
traces.

If your client is behind a wrapper or a subclass, name the provider outright:
`meter.wrap(client, provider="anthropic")`.

## A multi-step agent

```python
with meter.agent(
    "resolve-ticket",
    feature_id="ticket-resolution",
    customer_id="customer-123",
) as run:
    classification = run.llm("classify", lambda: anthropic_client.messages.create(...))
    documents      = run.tool("retrieve-documents", retrieve_documents)
    answer         = run.llm("generate-answer", lambda: anthropic_client.messages.create(...))
```

Each step returns whatever your function returned. Latency, status, tokens,
model and provider are recorded automatically. An exception fails the step and
the run and is re-raised unchanged — its message is never transmitted.

Besides `llm` and `tool` there are `retrieval`, `embedding`, `guardrail` and
`evaluation`, which record the same way and are separated in reporting.

Prompt identity, when you version prompts:

```python
run.llm("generate-answer", call, prompt_id="answer-ticket", prompt_version="5.0")
```

## Long-running agents

A run sends a heartbeat every 30 seconds so a slow-but-healthy agent is not
mistaken for a hung one. Heartbeats stop the moment the run ends — success,
failure or cancellation — and the timer is cancelled rather than abandoned.

Meter derives "stale" at read time from that activity. A stale run has **not**
failed: it may still finish, and it does so normally when it does.

## Queues and workers

```python
# producer
with meter.agent("resolve-ticket") as run:
    queue.send("continue-agent", trace_context=run.export_context())

# worker
with meter.resume(job.trace_context) as run:
    run.tool("process-document", process_document)
```

Both processes write to one trace. The exported context carries identifiers
only — trace id, parent span id, application, feature, environment, release. No
token, no customer content: it is designed on the assumption that anything on a
queue eventually gets logged.

## Delivery

Recording appends to an in-memory queue and returns. One background worker
batches and posts; nothing on your call path blocks, raises or touches the
network. If Meter is down or misconfigured, your agent is unaffected.

Bounded: one worker thread per meter whatever your traffic, and a capped queue
(10,000 events). If it fills — a stalled endpoint, a burst — the oldest events
are dropped and counted on `meter.dropped`. Metering degrades visibly; your
application does not.

A batch keeps one id across retries, so a retry after an ambiguous timeout
(server committed, response lost) is recognised as a replay rather than
doubling a run's cost.

Call `flush()` where the worker may not get scheduled — a serverless handler, a
script about to exit:

```python
meter.flush()          # True if the queue drained, False on timeout
```

An `atexit` hook flushes on normal shutdown.

## Upgrading from 0.x

1.0 replaces per-call cost reporting with traces. `meter.record(...)`,
`record_anthropic(...)` and the other `record_*` methods are gone; use
`meter.wrap(...)` for a single call and `meter.agent(...)` for a workflow.
`METER_TOKEN` is now `METER_INGEST_TOKEN`, and `METER_APPLICATION` is new and
required for anything to be grouped sensibly.
