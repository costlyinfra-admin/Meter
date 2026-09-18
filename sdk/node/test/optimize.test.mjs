/**
 * Optimize mode: measured signals, and the text that must never ride along.
 *
 * The whole point of this mode is that Meter's duplicate-call and prompt-caching
 * findings are *measured* rather than rules of thumb. The whole risk of it is
 * that it is the one part of the SDK that looks at a request body at all — so
 * most of what follows is about what does not leave the process.
 */
import assert from "node:assert";
import test from "node:test";
import { Meter } from "../index.mjs";

const URL = "https://app.test/api/hook/events";
const SECRET = "the quick brown fox jumped over the lazy dog";

/** Records ingest posts; answers the salt endpoint when one is configured. */
function capture({ salt = "pepper" } = {}) {
  const state = { batches: [], saltCalls: 0 };
  state.fetch = async (url, opts) => {
    if (url.endsWith("/salt")) {
      state.saltCalls += 1;
      if (salt === null) return { ok: false, status: 500 };
      return { ok: true, status: 200, json: async () => ({ salt }) };
    }
    state.batches.push(JSON.parse(opts.body));
    return { ok: true, status: 200 };
  };
  state.events = () => state.batches.flatMap((b) => b.events);
  state.signals = (kind) =>
    state
      .events()
      .map((e) => e.signal)
      .filter((s) => s && (kind === undefined || s.kind === kind));
  state.raw = () => JSON.stringify(state.batches);
  return state;
}

function meter(t, extra = {}) {
  return new Meter({
    application: "support-agent",
    ingestUrl: URL,
    token: "tok",
    featureId: "answer-generation",
    fetchImpl: t.fetch,
    flushIntervalMs: 1,
    optimizeFlushIntervalMs: 0, // flush prefix counters every call
    env: {},
    ...extra,
  });
}

const response = (over = {}) => ({
  model: "claude-sonnet-4-6",
  usage: { input_tokens: 1000, output_tokens: 200, ...over },
});

function clientFor(m, resp) {
  class Anthropic {
    constructor() {
      this.messages = { create: async () => resp ?? response() };
    }
  }
  return m.wrap(new Anthropic(), { featureId: "answer-generation" });
}

/** The SDK unrefs its timers; hold the loop open while a flush is awaited. */
const flush = async (m, timeoutMs) => {
  const keepAlive = setTimeout(() => {}, timeoutMs ?? 3000);
  try {
    return await (timeoutMs === undefined ? m.flush() : m.flush(timeoutMs));
  } finally {
    clearTimeout(keepAlive);
  }
};

const settle = () => new Promise((resolve) => setImmediate(resolve));

// --- off by default -------------------------------------------------------
test("a wrapped call sends no signal unless optimize is asked for", async () => {
  const t = capture();
  const m = meter(t);
  await clientFor(m).messages.create({ messages: [{ role: "user" }] });
  await flush(m);

  assert.deepEqual(t.signals(), []);
  assert.equal(t.saltCalls, 0); // and nothing was fetched for it either
});

// --- duplicates -----------------------------------------------------------
test("the same request twice reports the second as avoidable", async () => {
  const t = capture();
  const m = meter(t, { optimize: true, salt: "pepper" });
  const client = clientFor(m);
  const request = { model: "m", messages: [{ role: "user", content: "hi" }] };
  await client.messages.create(request);
  await client.messages.create(request);
  await flush(m);

  const duplicates = t.signals("duplicate");
  assert.equal(duplicates.length, 1); // the repeat, not the original
  assert.equal(duplicates[0].count, 1);
  assert.equal(duplicates[0].fingerprint.length, 64);
});

test("two different requests are not duplicates of each other", async () => {
  const t = capture();
  const m = meter(t, { optimize: true, salt: "pepper" });
  const client = clientFor(m);
  await client.messages.create({ model: "m", messages: [{ content: "one" }] });
  await client.messages.create({ model: "m", messages: [{ content: "two" }] });
  await flush(m);

  assert.deepEqual(t.signals("duplicate"), []);
});

test("a request is the same however its keys are ordered", async () => {
  const t = capture();
  const m = meter(t, { optimize: true, salt: "pepper" });
  const client = clientFor(m);
  await client.messages.create({ messages: [{ role: "user", content: "hi" }], model: "m" });
  await client.messages.create({ model: "m", messages: [{ content: "hi", role: "user" }] });
  await flush(m);

  assert.equal(t.signals("duplicate").length, 1);
});

test("a duplicate rides on the span the call was already sending", async () => {
  const t = capture();
  const m = meter(t, { optimize: true, salt: "pepper" });
  const client = clientFor(m);
  const request = { model: "m", messages: [{ content: "hi" }] };
  await client.messages.create(request);
  await client.messages.create(request);
  await flush(m);

  const carrier = t.events().filter((e) => e.signal?.kind === "duplicate");
  assert.equal(carrier.length, 1);
  // Not an extra event of its own: it is the completed span, with its tokens.
  assert.equal(carrier[0].event_type, "span.completed");
  assert.equal(carrier[0].tokens_in, 1000);
});

// --- prefixes -------------------------------------------------------------
test("a shared static head is summarised as its own span", async () => {
  const t = capture();
  const m = meter(t, { optimize: true, salt: "pepper" });
  const client = clientFor(m);
  for (let i = 0; i < 3; i += 1) {
    await client.messages.create({
      model: "m",
      system: "You are a security analyst. ".repeat(100),
      messages: [{ content: `question ${i}` }],
    });
  }
  await flush(m);

  const prefixes = t.signals("prefix");
  assert.ok(prefixes.length > 0, "no prefix summary was flushed");
  assert.equal(
    prefixes.reduce((sum, p) => sum + p.count, 0),
    3,
  );
  const summary = t.events().find((e) => e.signal?.kind === "prefix");
  assert.equal(summary.event_type, "span.completed");
  assert.equal(summary.operation_name, "optimize.prefix");
  assert.equal(summary.tokens_in, undefined); // only inside the signal, as a sum
});

test("a call served from cache is not counted as a caching opportunity", async () => {
  const t = capture();
  const m = meter(t, { optimize: true, salt: "pepper" });
  await clientFor(m, response({ cache_read_input_tokens: 900 })).messages.create({
    model: "m",
    system: "static",
    messages: [{ content: "hi" }],
  });
  await flush(m);

  assert.equal(t.signals("prefix")[0].cached_count, 1);
});

test("the prefix size is the provider's own count when the provider gives one", async () => {
  const t = capture();
  const m = meter(t, { optimize: true, salt: "pepper" });
  await clientFor(m, response({ cache_creation_input_tokens: 4242 })).messages.create({
    model: "m",
    system: "static block",
    messages: [{ content: "hi" }],
  });
  await flush(m);

  const signal = t.signals("prefix")[0];
  assert.equal(signal.prefix_tokens, 4242);
  assert.equal(signal.prefix_measured, true);
});

test("the prefix size is declared an estimate when the provider gives none", async () => {
  const t = capture();
  const m = meter(t, { optimize: true, salt: "pepper" });
  await clientFor(m).messages.create({
    model: "m",
    system: "s".repeat(400),
    messages: [{ content: "hi" }],
  });
  await flush(m);

  const signal = t.signals("prefix")[0];
  assert.equal(signal.prefix_measured, false);
  // Characters over four, which is why it may not be called measured.
  assert.ok(signal.prefix_tokens > 0 && signal.prefix_tokens < 400);
});

// --- the privacy boundary -------------------------------------------------
test("no part of a request or reply is transmitted", async () => {
  const t = capture();
  const m = meter(t, { optimize: true, salt: "pepper" });
  const client = clientFor(m);
  const request = {
    model: "m",
    system: `SYSTEM ${SECRET}`,
    tools: [{ name: `tool_${SECRET}` }],
    messages: [{ role: "user", content: SECRET }],
    metadata: { user: "alice@example.com" },
  };
  await client.messages.create(request);
  await client.messages.create(request); // and again, to force a duplicate
  await flush(m);

  assert.ok(t.signals("duplicate").length > 0 && t.signals("prefix").length > 0);
  const body = t.raw();
  for (const leak of [SECRET, "alice@example.com", "SYSTEM", "tool_"]) {
    assert.ok(!body.includes(leak), `${leak} reached the wire`);
  }
});

test("a fingerprint is salted so two tenants never agree", async () => {
  const prints = [];
  for (const salt of ["pepper", "paprika"]) {
    const t = capture({ salt });
    const m = meter(t, { optimize: true, salt });
    const client = clientFor(m);
    const request = { model: "m", messages: [{ content: "hi" }] };
    await client.messages.create(request);
    await client.messages.create(request);
    await flush(m);
    prints.push(t.signals("duplicate")[0].fingerprint);
  }
  assert.notEqual(prints[0], prints[1]);
});

test("without a salt nothing is fingerprinted at all", async () => {
  const t = capture({ salt: null }); // the salt endpoint fails
  const m = meter(t, { optimize: true });
  const client = clientFor(m);
  const request = { model: "m", messages: [{ content: "hi" }] };
  for (let i = 0; i < 4; i += 1) {
    await client.messages.create(request);
    await settle();
    await flush(m);
  }

  assert.deepEqual(t.signals(), []);
  // Asked once and then left alone: an unsalted hash is never the fallback.
  assert.equal(t.saltCalls, 1);
});

test("the salt is fetched off the caller's path", async () => {
  const t = capture();
  const m = meter(t, { optimize: true });
  const client = clientFor(m);
  const request = { model: "m", messages: [{ content: "hi" }] };

  // The first call cannot have waited for a salt it had not fetched yet, so it
  // carries no signal — and the call itself still completed normally.
  await client.messages.create(request);
  await flush(m);
  assert.deepEqual(t.signals(), []);
  assert.equal(t.events().at(-1).event_type, "trace.completed");

  await settle();
  assert.equal(m.salt(), "pepper");

  await client.messages.create(request);
  await client.messages.create(request);
  await flush(m);
  assert.ok(t.signals("duplicate").length > 0, "signals never started once the salt arrived");
});

test("a provider call still returns its response when optimizing", async () => {
  const t = capture();
  const m = meter(t, { optimize: true, salt: "pepper" });
  const original = response();
  assert.equal(await clientFor(m, original).messages.create({ messages: [] }), original);
});

test("the map of seen requests cannot grow with traffic", async () => {
  const t = capture();
  const m = meter(t, { optimize: true, salt: "pepper" });
  const collector = m._optimizer;
  collector._dupCapacity = 10;
  for (let i = 0; i < 400; i += 1) {
    collector.onCall("anthropic", "m", { messages: [{ c: i }] }, {});
  }
  assert.equal(collector._seen.size, 10);

  // And the oldest went first, so a request repeated inside the window is still
  // caught while an ancient one is not.
  assert.equal(collector.onCall("anthropic", "m", { messages: [{ c: 399 }] }, {}).kind, "duplicate");
  assert.equal(collector.onCall("anthropic", "m", { messages: [{ c: 0 }] }, {}), null);
});

test("a request shape this cannot read is never called a duplicate", async () => {
  // normalizeRequest reads messages / input / contents. A call with none of
  // them has nothing to compare, and reporting the second such call as a repeat
  // of the first would be a finding about this code, not about the traffic.
  const t = capture();
  const m = meter(t, { optimize: true, salt: "pepper" });
  const client = clientFor(m);
  await client.messages.create({ model: "m", prompt: "a legacy completion call" });
  await client.messages.create({ model: "m", prompt: "a different one entirely" });
  await flush(m);

  assert.deepEqual(t.signals(), []);
});

test("a prefix is attributed to the feature the call was for", async () => {
  const t = capture();
  const m = meter(t, { optimize: true, salt: "pepper" }); // default: answer-generation
  // Named for the provider on purpose: wrap() identifies a client by its type,
  // and messages.create is only an instrumented path for Anthropic.
  class Anthropic {
    constructor() {
      this.messages = { create: async () => response() };
    }
  }
  const client = m.wrap(new Anthropic(), { featureId: "threat-triage" });
  await client.messages.create({ model: "m", system: "static", messages: [{ content: "hi" }] });
  await flush(m);

  const summary = t.events().find((e) => e.signal?.kind === "prefix");
  assert.equal(summary.feature_id, "threat-triage");
});

test("prefix counters are not lost when a short process ends", async () => {
  // Prefix counters leave on a 60-second timer. A script, a batch job or one
  // serverless invocation would take every one of them to the grave — which is
  // most of the traffic this feature exists to measure — so a flush takes them.
  const t = capture();
  const m = meter(t, { optimize: true, salt: "pepper", optimizeFlushIntervalMs: 3_600_000 });
  const client = clientFor(m);
  for (let i = 0; i < 3; i += 1) {
    await client.messages.create({ model: "m", system: "static", messages: [{ c: i }] });
  }
  assert.deepEqual(t.signals("prefix"), []); // the interval is nowhere near elapsed

  await flush(m);
  const prefixes = t.signals("prefix");
  assert.ok(prefixes.length > 0);
  assert.equal(prefixes[0].count, 3);
});
