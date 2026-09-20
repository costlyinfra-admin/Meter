/**
 * Optimize mode: measured signals, and the text that must never ride along.
 *
 * The whole point of this mode is that Meter's duplicate-call and prompt-caching
 * findings are *measured* rather than rules of thumb. The whole risk of it is
 * that it is the one part of the SDK that looks at a request body at all — so
 * most of what follows is about what does not leave the process.
 */
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { CANON_VERSION, canonicalRequest, Meter, UnsupportedRequest } from "../index.mjs";

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

test("a call shape v1 could not read is now compared properly", async () => {
  // v1 read messages / input / contents and gave up on anything else, so a
  // legacy completion call was dropped entirely. The canonical form reads the
  // whole request, so these compare on their merits.
  const t = capture();
  const m = meter(t, { optimize: true, salt: "pepper" });
  const client = clientFor(m);
  await client.messages.create({ model: "m", prompt: "a legacy completion call" });
  await client.messages.create({ model: "m", prompt: "a different one entirely" });
  await flush(m);
  assert.deepEqual(t.signals("duplicate"), []);

  await client.messages.create({ model: "m", prompt: "a legacy completion call" });
  await flush(m);
  assert.equal(t.signals("duplicate").length, 1);
});

test("a request this cannot read is never called a duplicate", async () => {
  // Unreadable must never degrade into identical. v1's String() fallback turned
  // distinct objects into one shared string, so they matched.
  const t = capture();
  const m = meter(t, { optimize: true, salt: "pepper" });
  const client = clientFor(m);

  const cyclic = { role: "user" };
  cyclic.self = cyclic;
  for (let i = 0; i < 2; i += 1) {
    await client.messages.create({ model: "m", messages: [cyclic] });
  }
  await flush(m);
  assert.deepEqual(t.signals("duplicate"), []);

  class Opaque {
    toString() {
      return "obj";
    }
  }
  for (let i = 0; i < 2; i += 1) {
    await client.messages.create({ model: "m", messages: [new Opaque()] });
  }
  await flush(m);
  assert.deepEqual(t.signals("duplicate"), []);
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

// --- request identity (v2) -------------------------------------------------
// Every one of these changes what the model returns. v1 hashed only the
// messages, so all of them produced the SAME fingerprint as the base request.
const OUTPUT_AFFECTING = [
  ["temperature", { temperature: 0.9 }],
  ["system prompt", { system: "You are terse." }],
  ["tools", { tools: [{ name: "search" }] }],
  ["tool choice", { tool_choice: "required" }],
  ["max tokens", { max_tokens: 32 }],
  ["stop sequences", { stop: ["STOP"] }],
  ["response format", { response_format: { type: "json_object" } }],
  ["seed", { seed: 7 }],
  ["reasoning effort", { reasoning_effort: "high" }],
  ["conversation state", { previous_response_id: "resp_42" }],
  ["a parameter nobody here has heard of", { future_provider_knob: "on" }],
];

test("a field that changes the output changes the identity", () => {
  const base = { model: "m", messages: [{ role: "user", content: "hi" }] };
  for (const [label, extra] of OUTPUT_AFFECTING) {
    assert.notEqual(canonicalRequest(base), canonicalRequest({ ...base, ...extra }), label);
  }
});

test("credentials and transport settings are not part of identity", () => {
  const base = { model: "m", messages: [{ content: "hi" }] };
  const noisy = {
    ...base, api_key: "sk-secret", timeout: 30, maxRetries: 9,
    headers: { "X-Trace": "abc" }, baseURL: "https://example",
  };
  assert.equal(canonicalRequest(base), canonicalRequest(noisy));
  assert.ok(!canonicalRequest(noisy).includes("sk-secret"));
});

test("absent is not null", () => {
  assert.notEqual(
    canonicalRequest({ messages: [], stop: null }),
    canonicalRequest({ messages: [] }),
  );
});

test("oversized and cyclic requests fail safely rather than matching", () => {
  const cyclic = {};
  cyclic.self = cyclic;
  assert.throws(() => canonicalRequest({ messages: [cyclic] }), UnsupportedRequest);

  let deep = { end: true };
  for (let i = 0; i < 80; i += 1) deep = { n: deep };
  assert.throws(() => canonicalRequest({ messages: [deep] }), UnsupportedRequest);

  class Opaque {}
  assert.throws(() => canonicalRequest({ messages: [new Opaque()] }), UnsupportedRequest);
  assert.throws(() => canonicalRequest({ temperature: NaN }), UnsupportedRequest);
});

test("the golden vectors match this implementation, byte for byte", () => {
  // The same file the Python suite reads. If the two ever disagree, a company
  // running both languages is told two different stories about one request.
  const here = dirname(fileURLToPath(import.meta.url));
  const golden = JSON.parse(
    readFileSync(join(here, "..", "..", "golden", "request-fingerprints.json"), "utf8"),
  );
  assert.equal(golden.canon_version, CANON_VERSION);
  assert.ok(golden.vectors.length >= 10);
  for (const vector of golden.vectors) {
    assert.equal(canonicalRequest(vector.request), vector.canonical, vector.name);
  }
});

// --- comparison scope ------------------------------------------------------
test("two customers sending the same prompt are not one avoidable call", async () => {
  const t = capture();
  const m = meter(t, { optimize: true, salt: "pepper" });
  class Anthropic {
    constructor() {
      this.messages = { create: async () => response() };
    }
  }
  const request = { model: "m", messages: [{ content: "hi" }] };
  await m.wrap(new Anthropic(), { featureId: "f", customerId: "acme" }).messages.create(request);
  await m.wrap(new Anthropic(), { featureId: "f", customerId: "globex" }).messages.create(request);
  await flush(m);

  assert.deepEqual(t.signals("duplicate"), []);
});

test("an unscoped repeat is recorded but not called safe", async () => {
  const t = capture();
  const m = meter(t, { optimize: true, salt: "pepper" });
  const request = { model: "m", messages: [{ content: "hi" }] };
  const client = clientFor(m); // no customerId anywhere
  await client.messages.create(request);
  await client.messages.create(request);
  await flush(m);

  assert.equal(t.signals("duplicate")[0].scope_kind, "unscoped");
  assert.equal(t.signals("duplicate")[0].fingerprint_version, "v2");
});

// --- the window ------------------------------------------------------------
test("a repeat after the window is not an avoidable call", async () => {
  const t = capture();
  const m = meter(t, { optimize: true, salt: "pepper", optimizeWindowMs: 50 });
  const client = clientFor(m);
  const request = { model: "m", messages: [{ content: "hi" }] };

  await client.messages.create(request);
  await client.messages.create(request);
  await flush(m);
  assert.equal(t.signals("duplicate").length, 1, "the repeat inside the window");

  await new Promise((r) => setTimeout(r, 80)); // past the group's start
  await client.messages.create(request);
  await flush(m);
  assert.equal(t.signals("duplicate").length, 1, "an expired repeat is not avoidable");
});
