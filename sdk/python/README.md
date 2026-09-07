# costlyinfra-meter

The optional metering hook for [Meter](https://github.com/costlyinfra-admin/Meter) —
a thin, fail-safe wrapper that reports per-call LLM usage so spend can be
attributed **per feature**. Stdlib-only, no dependencies. Cost is computed
server-side from Meter's pricing tables — the SDK never sees prices, and it
never sends prompt or response content, only token counts and a `feature_id`.

It is **fail-safe**: recording appends to an in-memory queue and returns. A
single background worker batches and posts; nothing on your call path blocks,
raises, or touches the network. If Meter is down, misconfigured, or asleep,
your application is unaffected. With no ingest URL/token configured, every call
is a no-op.

It is also **bounded**: one worker thread per meter whatever your traffic, and a
capped queue (10,000 events). If the queue fills — a stalled endpoint, a burst —
the oldest events are dropped and counted on `meter.dropped`. Metering degrades;
your application does not.

### Delivery and `flush()`

Events are sent when a batch fills (50) or after `flush_interval` seconds (5),
whichever comes first. An `atexit` hook flushes on normal shutdown with a short
deadline, so a script that exits immediately still delivers.

Call `meter.flush()` explicitly where the worker may not get scheduled — a
serverless handler that freezes between invocations, or before a hard exit:

```python
meter.flush()          # returns True if the queue drained, False on timeout
meter.flush(timeout=1) # never waits longer than you allow
```

### Retries

A failed batch is retried (3 attempts, backing off ~1s / 4s / 15s with jitter) —
enough to ride out a restart, a blip, or a sleeping instance waking up. A 4xx is
*not* retried: a bad token or a malformed event fails the same way forever, so
retrying only hammers the endpoint. 429 is retried, since it explicitly asks you
to come back.

Retrying is safe because every attempt carries the same `batch_id`. The server
applies a batch once and recognises re-deliveries, so a retry after a timeout you
could not distinguish from a failure cannot double-charge a feature. Events only
count as lost after the final attempt, and then `meter.dropped` says so.

Tunable per meter: `batch_size`, `flush_interval`, `queue_max`, `timeout`,
`max_attempts`, `retry_backoff`.

## Install

Into the virtualenv your application runs in:

```bash
python3 -m pip install "costlyinfra-meter>=1.0"
```

Or add `costlyinfra-meter>=1.0` to `requirements.txt` / `pyproject.toml`, which is
the version that survives a rebuild.

If pip answers `error: externally-managed-environment`, you are outside a
virtualenv — activate your application's environment and run it again.

## Use

**Recommended — wrap the client once (no per-call code):**

```python
from costlyinfra_meter import wrap

client = wrap(anthropic_client, feature_id="feature-threat-triage")  # reads ENV

resp = client.messages.create(model="claude-sonnet-4-6", ...)   # metered automatically
```

Provider is auto-detected; each call is recorded with its latency. Pass an
optional `metadata={…}` (e.g. `environment`, `customer_id`) for extra
attribution. Streaming/async calls use the explicit form below.

**Explicit — one line per call:**

```python
from costlyinfra_meter import Meter

meter = Meter(feature_id="feature-threat-triage")
resp = anthropic_client.messages.create(model="claude-sonnet-4-6", ...)
meter.record_anthropic(resp)   # <- the whole hook
```

Helpers: `record_anthropic`, `record_openai`, `record_gemini`,
`record_openai_compatible`, or the generic `record(provider=…, model=…,
tokens_in=…, tokens_out=…, feature_id=…)`.

### Optimize mode (opt-in)

`Meter(..., optimize=True)` additionally emits **privacy-safe** signals — salted
hashes and counts, never prompt text — so Meter can surface *measured*
optimization opportunities (duplicate calls, uncached repeated prefixes). Off by
default; all work is off the call path, memory-bounded, and fail-safe. The SDK
fetches a per-tenant salt once (`GET /api/hook/salt`, same ingest token).

## Config

| Env var                  | What it is                                            |
|--------------------------|-------------------------------------------------------|
| `METER_INGEST_URL`   | e.g. `https://app.example.com/api/hook/events`        |
| `METER_INGEST_TOKEN` | the per-workspace ingest token from the dashboard     |

## License

Apache-2.0. (The Meter server is AGPL-3.0; this client SDK is permissive so
you can embed it in a proprietary app.)
