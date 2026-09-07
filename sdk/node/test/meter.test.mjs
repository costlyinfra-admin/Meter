import assert from "node:assert";
import test from "node:test";
import { Meter, wrap } from "../index.mjs";

function meterWithCapture(calls) {
  return new Meter("default-feature", {
    ingestUrl: "https://app.test/api/hook/events",
    token: "tok",
    fetchImpl: (url, opts) => {
      calls.push({ url, opts });
      return Promise.resolve({ ok: true });
    },
  });
}

test("record builds an event and authenticates", async () => {
  const calls = [];
  const m = meterWithCapture(calls);
  m.record({
    provider: "anthropic",
    model: "claude-sonnet-4-6",
    tokensIn: 1200,
    tokensOut: 300,
    featureId: "f1",
  });
  assert.equal(await flush(m), true);
  assert.equal(calls[0].opts.headers.Authorization, "Bearer tok");
  const event = JSON.parse(calls[0].opts.body).events[0];
  assert.equal(event.tokens_in, 1200);
  assert.equal(event.feature_id, "f1");
});

test("recordAnthropic maps usage fields", async () => {
  const calls = [];
  const m = meterWithCapture(calls);
  m.recordAnthropic(
    { model: "claude-haiku-4-5", usage: { input_tokens: 50, output_tokens: 7 } },
    { featureId: "f2" },
  );
  await flush(m);
  const event = JSON.parse(calls[0].opts.body).events[0];
  assert.equal(event.provider, "anthropic");
  assert.equal(event.tokens_in, 50);
  assert.equal(event.tokens_out, 7);
  assert.equal(event.feature_id, "f2");
});

test("unconfigured meter is a no-op", async () => {
  const m = new Meter();
  assert.equal(m.enabled, false);
  m.record({ provider: "openai", model: "gpt-4o", tokensIn: 1, tokensOut: 1 });
  assert.equal(m._queue.length, 0); // nothing queued when unconfigured
  assert.equal(await flush(m), true);
});

// --- wrap() auto-instrumentation ------------------------------------------

// A fake OpenAI-shaped client whose create() resolves like the real SDK.
class OpenAI {
  constructor(resp) {
    this.apiKey = "sk-real";
    this.chat = {
      completions: {
        create: async (args) => {
          this.lastArgs = args;
          return resp;
        },
      },
    };
  }
}

test("wrap records the completion with latency and passes through", async () => {
  const calls = [];
  const meter = new Meter("f1", {
    ingestUrl: "https://app.test/api/hook/events",
    token: "tok",
    metadata: { environment: "prod" },
    fetchImpl: (url, opts) => {
      calls.push({ url, opts });
      return Promise.resolve({ ok: true });
    },
  });
  const resp = { model: "gpt-4o", usage: { prompt_tokens: 80, completion_tokens: 20 } };
  const client = new OpenAI(resp);
  const wrapped = wrap(client, { meter });

  const out = await wrapped.chat.completions.create({ model: "gpt-4o", messages: [] });
  assert.equal(out, resp); // returns the real response, unchanged
  assert.equal(wrapped.apiKey, "sk-real"); // non-instrumented attribute passes through

  await flush(meter); // delivery is batched; force it
  const event = JSON.parse(calls[0].opts.body).events[0];
  assert.equal(event.provider, "openai");
  assert.equal(event.feature_id, "f1");
  assert.equal(event.tokens_in, 80);
  assert.equal(typeof event.latency_ms, "number");
  assert.deepEqual(event.metadata, { environment: "prod" });
});

test("wrap skips a streaming response (no usage)", async () => {
  const calls = [];
  const meter = new Meter("f1", {
    ingestUrl: "https://app.test/api/hook/events",
    token: "tok",
    fetchImpl: (url, opts) => {
      calls.push({ url, opts });
      return Promise.resolve({ ok: true });
    },
  });
  const stream = (async function* () {})(); // async iterator, no usage
  const wrapped = wrap(new OpenAI(stream), { meter });
  const out = await wrapped.chat.completions.create({ stream: true });
  assert.equal(out, stream);
  await flush(meter);
  assert.equal(calls.length, 0); // nothing recorded
});

test("wrap detects the provider from the client class", () => {
  const wrapped = wrap(new OpenAI({ usage: {} }), {
    meter: new Meter(null, { ingestUrl: "u", token: "t", fetchImpl: () => Promise.resolve({}) }),
  });
  assert.equal(typeof wrapped.chat.completions.create, "function");
});

// --- optimize mode (opt spec M-opt-2) --------------------------------------

function optMeter(calls, opts = {}) {
  // salt supplied directly so the optimizer never hits the network in tests.
  return new Meter("f1", {
    ingestUrl: "https://app.test/api/hook/events",
    token: "tok",
    optimize: true,
    salt: "test-salt",
    fetchImpl: (url, o) => {
      calls.push({ url, opts: o });
      return Promise.resolve({ ok: true });
    },
    ...opts,
  });
}

/**
 * Await a flush with the event loop held open.
 *
 * Every timer the SDK owns is unref'd on purpose — a metering hook must never
 * keep a host process alive (the last test in this file asserts exactly that).
 * The consequence for tests is that while one awaits a flush, nothing keeps the
 * loop running: not the abort deadline, not the retry backoff, not flush's own
 * timeout. Whether the test runner holds a reference of its own varies by Node
 * version — it does on 25, it does not on 20, which is how these passed locally
 * and failed in CI. Holding one here makes these tests observe the SDK rather
 * than the runner.
 */
const flush = async (meter, timeoutMs) => {
  const keepAlive = setTimeout(() => {}, timeoutMs ?? 3000);
  try {
    return await (timeoutMs === undefined ? meter.flush() : meter.flush(timeoutMs));
  } finally {
    clearTimeout(keepAlive);
  }
};

const allEvents = (calls) => calls.flatMap((c) => JSON.parse(c.opts.body).events);
// The optimizer resolves its salt asynchronously before the event is queued, so
// give that a turn, then force the batch out.
const settle = async (meter) => {
  await new Promise((r) => setTimeout(r, 20));
  await flush(meter);
};

test("optimize is off by default", async () => {
  const calls = [];
  const meter = meterWithCapture(calls);
  assert.equal(meter._optimizer, null);
  const wrapped = wrap(new OpenAI({ model: "gpt-4o", usage: { prompt_tokens: 5, completion_tokens: 1 } }), { meter });
  await wrapped.chat.completions.create({ model: "gpt-4o", messages: [{ role: "user", content: "hi" }] });
  await settle(meter);
  assert.equal("signal" in JSON.parse(calls[0].opts.body).events[0], false);
});

test("optimize flags a duplicate call", async () => {
  const calls = [];
  const resp = { model: "gpt-4o", usage: { prompt_tokens: 5, completion_tokens: 1 } };
  const meter = optMeter(calls);
  const wrapped = wrap(new OpenAI(resp), { meter });
  const req = { model: "gpt-4o", messages: [{ role: "user", content: "same" }] };

  await wrapped.chat.completions.create({ ...req });
  await settle(meter); // force the first out so the repeat is seen as a repeat
  await wrapped.chat.completions.create({ ...req });
  await settle(meter);

  const events = allEvents(calls);
  assert.equal("signal" in events[0], false); // first call is novel
  assert.equal(events[1].signal.kind, "duplicate");
  assert.equal(events[1].signal.count, 1);
  assert.equal(events[1].signal.fingerprint.length, 64); // salted sha256 hex, no prompt text
});

test("optimize emits prefix summaries on flush", async () => {
  const calls = [];
  const resp = { model: "gpt-4o", usage: { prompt_tokens: 5, completion_tokens: 1 } };
  const meter = optMeter(calls, { optimizeFlushInterval: 0 });
  const wrapped = wrap(new OpenAI(resp), { meter });
  await wrapped.chat.completions.create({
    model: "gpt-4o",
    tools: [{ type: "function", function: { name: "triage", description: "x".repeat(400) } }],
    messages: [{ role: "user", content: "alert 1" }],
  });
  await settle(meter);

  const prefixes = allEvents(calls).filter((e) => e.signal && e.signal.kind === "prefix");
  assert.equal(prefixes.length, 1);
  assert.equal(prefixes[0].signal.count, 1);
  assert.ok(prefixes[0].signal.prefix_tokens > 0);
  assert.equal(prefixes[0].signal.fingerprint.length, 64);
});

test("optimize emits nothing without a salt", async () => {
  const calls = [];
  const resp = { model: "gpt-4o", usage: { prompt_tokens: 5, completion_tokens: 1 } };
  const meter = optMeter(calls, { optimizeFlushInterval: 0, salt: "" });
  const wrapped = wrap(new OpenAI(resp), { meter });
  const req = { model: "gpt-4o", messages: [{ role: "user", content: "x" }] };
  await wrapped.chat.completions.create({ ...req });
  await settle(meter);
  await wrapped.chat.completions.create({ ...req });
  await settle(meter);
  assert.ok(allEvents(calls).every((e) => !("signal" in e)));
});

test("every request carries an abort deadline", async () => {
  // Without a signal a fetch has no timeout of its own, so a sleeping ingest
  // endpoint leaves requests pending in the caller's process indefinitely.
  const calls = [];
  const m = meterWithCapture(calls);
  m.record({ provider: "openai", model: "gpt-4o", tokensIn: 1, tokensOut: 1 });
  await flush(m);
  assert.ok(calls[0].opts.signal, "no abort signal on the POST");
  assert.equal(typeof calls[0].opts.signal.aborted, "boolean");
});

test("a hung endpoint aborts instead of pending forever", async () => {
  // A fetch that never settles on its own must still settle, via the signal.
  const m = new Meter("f", {
    ingestUrl: "https://app.test/api/hook/events",
    token: "tok",
    timeoutMs: 40,
    retryBackoffMs: [0], // the deadline is what's under test, not the backoff
    fetchImpl: (url, opts) =>
      new Promise((_resolve, reject) => {
        opts.signal.addEventListener("abort", () => reject(opts.signal.reason));
        // never resolves otherwise — this is the sleeping-Render case
      }),
  });

  const started = Date.now();
  m.record({ provider: "openai", model: "gpt-4o", tokensIn: 1, tokensOut: 1 });
  await flush(m, 3000);
  assert.ok(Date.now() - started < 3000, "request did not abort");
  assert.equal(m.dropped, 1); // gave up after its attempts, and counted it
});

test("the timeout is configurable and defaults to 5s", () => {
  assert.equal(new Meter("f", { ingestUrl: "u", token: "t" }).timeoutMs, 5000);
  assert.equal(new Meter("f", { ingestUrl: "u", token: "t", timeoutMs: 250 }).timeoutMs, 250);
});

// --- delivery: queue, batching, bounds, retries ----------------------------

test("events are batched into one request", async () => {
  const calls = [];
  const m = meterWithCapture(calls);
  for (let i = 0; i < 20; i += 1) {
    m.record({ provider: "openai", model: "gpt-4o", tokensIn: i, tokensOut: 1 });
  }
  await flush(m);

  assert.equal(calls.length, 1, "20 events should not be 20 requests");
  const events = JSON.parse(calls[0].opts.body).events;
  assert.equal(events.length, 20);
  assert.deepEqual(
    events.map((e) => e.tokens_in),
    [...Array(20).keys()],
  ); // order preserved
});

test("a batch is capped at batchSize", async () => {
  const calls = [];
  const m = new Meter("f", {
    ingestUrl: "https://app.test/api/hook/events",
    token: "tok",
    batchSize: 5,
    fetchImpl: (url, opts) => {
      calls.push({ url, opts });
      return Promise.resolve({ status: 200 });
    },
  });
  for (let i = 0; i < 12; i += 1) m.record({ provider: "openai", model: "gpt-4o", tokensIn: 1 });
  await flush(m);

  assert.deepEqual(
    calls.map((c) => JSON.parse(c.opts.body).events.length),
    [5, 5, 2],
  );
});

test("recording does not wait on the network", async () => {
  // The call path must not pay for a slow — or hung — ingest endpoint.
  const m = new Meter("f", {
    ingestUrl: "https://app.test/api/hook/events",
    token: "tok",
    fetchImpl: () => new Promise(() => {}), // never settles
  });
  const started = Date.now();
  for (let i = 0; i < 1000; i += 1) m.record({ provider: "openai", model: "gpt-4o", tokensIn: 1 });
  assert.ok(Date.now() - started < 300, "recording blocked behind the transport");
});

test("a full queue drops oldest and counts it", async () => {
  const m = new Meter("f", {
    ingestUrl: "https://app.test/api/hook/events",
    token: "tok",
    queueMax: 10,
    fetchImpl: () => new Promise(() => {}), // stalled: nothing drains
  });
  for (let i = 0; i < 200; i += 1) m.record({ provider: "openai", model: "gpt-4o", tokensIn: i });

  assert.ok(m.dropped > 0);
  assert.ok(m._queue.length <= 10, "queue grew past its bound");
});

test("every batch carries an id and retries reuse it", async () => {
  // The property that makes retrying safe: the server applies a batch id once
  // and recognises replays, so a retry cannot double-charge a feature.
  const seen = [];
  const m = new Meter("f", {
    ingestUrl: "https://app.test/api/hook/events",
    token: "tok",
    retryBackoffMs: [0],
    fetchImpl: (url, opts) => {
      seen.push(JSON.parse(opts.body).batch_id);
      return seen.length < 3 ? Promise.reject(new Error("asleep")) : Promise.resolve({ status: 200 });
    },
  });
  m.record({ provider: "openai", model: "gpt-4o", tokensIn: 1 });
  await flush(m, 5000);

  assert.equal(seen.length, 3, "did not retry");
  assert.equal(new Set(seen).size, 1, `retries invented new batch ids: ${new Set(seen).size}`);
  assert.equal(m.dropped, 0);
});

test("a 5xx is retried, a 4xx is not", async () => {
  const mk = (status) => {
    const attempts = [];
    const m = new Meter("f", {
      ingestUrl: "https://app.test/api/hook/events",
      token: "tok",
      retryBackoffMs: [0],
      fetchImpl: () => {
        attempts.push(1);
        return Promise.resolve({ status }); // fetch does NOT reject on 4xx/5xx
      },
    });
    return { m, attempts };
  };

  const server = mk(503);
  server.m.record({ provider: "openai", model: "gpt-4o", tokensIn: 1 });
  await flush(server.m, 5000);
  assert.equal(server.attempts.length, 3, "a 5xx should be retried");

  const client = mk(401); // a bad token fails identically forever
  client.m.record({ provider: "openai", model: "gpt-4o", tokensIn: 1 });
  await flush(client.m, 5000);
  assert.equal(client.attempts.length, 1, "a 4xx should not be retried");
  assert.equal(client.m.dropped, 1);

  const throttled = mk(429); // the one 4xx that invites coming back
  throttled.m.record({ provider: "openai", model: "gpt-4o", tokensIn: 1 });
  await flush(throttled.m, 5000);
  assert.equal(throttled.attempts.length, 3);
});

test("every event is timestamped when it happens, not when it is sent", async () => {
  // The server bills by occurred_at and falls back to arrival time, so a
  // deferred send could otherwise land a 23:59 call in the next month.
  const calls = [];
  const m = meterWithCapture(calls);
  m.record({ provider: "openai", model: "gpt-4o", tokensIn: 1 });
  const recorded = Date.now();
  await new Promise((r) => setTimeout(r, 30));
  await flush(m);

  const stamp = JSON.parse(calls[0].opts.body).events[0].occurred_at;
  assert.match(stamp, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
  assert.ok(Math.abs(Date.parse(stamp) - recorded) < 5000);

  // An explicit occurredAt still wins, so backfilling stays possible.
  m.record({ provider: "openai", model: "gpt-4o", tokensIn: 1, occurredAt: "2026-01-15T10:00:00Z" });
  await flush(m);
  const last = JSON.parse(calls[calls.length - 1].opts.body).events.at(-1);
  assert.equal(last.occurred_at, "2026-01-15T10:00:00Z");
});

test("the batching timer never holds the process open", async () => {
  // An armed setTimeout would keep the event loop alive after the app is done.
  const m = meterWithCapture([]);
  m.record({ provider: "openai", model: "gpt-4o", tokensIn: 1 });
  assert.ok(m._timer, "no timer armed");
  assert.equal(typeof m._timer.hasRef, "function");
  assert.equal(m._timer.hasRef(), false, "the timer would keep the process alive");
  await flush(m);
});
