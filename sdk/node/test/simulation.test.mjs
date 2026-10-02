// The content-free simulation counters behind "Test this" (EX-1). Mirrors the
// Python SDK's tests/test_simulation.py: the two SDKs must count the same calls
// the same way, or one customer's test would depend on their language.

import assert from "node:assert";
import test from "node:test";
import { _Optimizer, Meter, SIM_TTLS } from "../index.mjs";

const SECRET = "the quick brown fox jumped over the lazy dog";
const REQUEST = { model: "m", messages: [{ content: "hi" }] };
const SUFFIXES = SIM_TTLS.map(([, suffix]) => suffix);

function capture() {
  const state = { batches: [] };
  state.fetch = async (url, opts) => {
    if (url.endsWith("/salt")) return { ok: true, status: 200, json: async () => ({ salt: "pepper" }) };
    state.batches.push(JSON.parse(opts.body));
    return { ok: true, status: 200 };
  };
  state.events = () => state.batches.flatMap((b) => b.events);
  return state;
}

function meter(t = capture(), extra = {}) {
  return new Meter({
    application: "support-agent",
    ingestUrl: "https://app.test/api/hook/events",
    token: "tok",
    featureId: "answer-generation",
    fetchImpl: t.fetch,
    flushIntervalMs: 1,
    optimizeFlushIntervalMs: 0,
    salt: "pepper",
    env: {},
    ...extra,
  });
}

function collector(clock, extra = {}) {
  return new _Optimizer(meter(), { clock: () => clock.t, ...extra });
}

const simulation = (events) =>
  events.map((e) => e.signal).filter((s) => s && s.kind === "simulation");

test("each limit counts the calls a cache that old would have served", () => {
  const clock = { t: 0 };
  const c = collector(clock);
  for (const step of [0, 30_000, 60_000, 3_600_000]) {
    clock.t += step;
    c.onCall("anthropic", "m", REQUEST, {});
  }
  const [sim] = simulation(c.dueSummaries(true));
  assert.equal(sim.calls, 4);
  assert.equal(sim.hits_1m, 1);
  assert.equal(sim.hits_10m, 2);
  assert.equal(sim.hits_1h, 2);
  assert.equal(sim.hits_24h, 3);
});

test("a limit runs from the first call and is never extended", () => {
  const clock = { t: 0 };
  const c = collector(clock);
  c.onCall("anthropic", "m", REQUEST, {});
  for (let i = 0; i < 3; i += 1) {
    clock.t += 40_000;
    c.onCall("anthropic", "m", REQUEST, {});
  }
  const [sim] = simulation(c.dueSummaries(true));
  assert.equal(sim.hits_1m, 2);
});

test("the ten-minute limit agrees with the duplicate detector", () => {
  const clock = { t: 0 };
  const c = collector(clock, { windowMs: 600_000 });
  let duplicates = 0;
  for (const step of [0, 100_000, 450_000, 100_000, 5_000, 2_000_000, 1_000]) {
    clock.t += step;
    if (c.onCall("anthropic", "m", REQUEST, {}) !== null) duplicates += 1;
  }
  const [sim] = simulation(c.dueSummaries(true));
  assert.ok(duplicates > 0);
  assert.equal(sim.hits_10m, duplicates);
});

test("only the calls a cache would have served are priced as hits", () => {
  const clock = { t: 0 };
  const c = collector(clock);
  c.onCall("anthropic", "m", REQUEST, { tokens_in: 1000, tokens_out: 300 });
  clock.t += 10_000;
  c.onCall("anthropic", "m", REQUEST, { tokens_in: 1000, tokens_out: 250 });
  const [sim] = simulation(c.dueSummaries(true));
  assert.deepEqual([sim.tokens_in, sim.tokens_out], [2000, 550]);
  for (const suffix of SUFFIXES) {
    assert.equal(sim[`hit_tokens_in_${suffix}`], 1000);
    assert.equal(sim[`hit_tokens_out_${suffix}`], 250);
  }
});

test("scoped and unscoped calls are reported apart", () => {
  const clock = { t: 0 };
  const c = collector(clock);
  for (let i = 0; i < 2; i += 1) {
    c.onCall("anthropic", "m", REQUEST, {}, null, { customer_id: "acme" });
    c.onCall("anthropic", "m", REQUEST, {});
    clock.t += 5_000;
  }
  const sims = Object.fromEntries(simulation(c.dueSummaries(true)).map((s) => [s.scope_kind, s]));
  assert.deepEqual(Object.keys(sims).sort(), ["explicit", "unscoped"]);
  assert.equal(sims.explicit.calls, 2);
  assert.equal(sims.explicit.hits_1m, 1);
  assert.equal(sims.unscoped.calls, 2);
  assert.equal(sims.unscoped.hits_1m, 1);
});

test("a request shape forgotten early is counted, not hidden", () => {
  const clock = { t: 0 };
  const c = collector(clock, { simCapacity: 2 });
  c.onCall("anthropic", "m", { model: "m", messages: [{ content: "a" }] }, {});
  clock.t += 120_000;
  c.onCall("anthropic", "m", { model: "m", messages: [{ content: "b" }] }, {});
  c.onCall("anthropic", "m", { model: "m", messages: [{ content: "c" }] }, {});
  const [sim] = simulation(c.dueSummaries(true));
  assert.equal(sim.evicted_1m, 0);
  assert.equal(sim.evicted_10m, 1);
  assert.equal(sim.evicted_1h, 1);
  assert.equal(sim.evicted_24h, 1);
});

test("a request that cannot be read is a call but never a hit", () => {
  const clock = { t: 0 };
  const c = collector(clock);
  const unreadable = { model: "m", messages: [{ content: 10n }] };
  c.onCall("anthropic", "m", unreadable, {});
  c.onCall("anthropic", "m", unreadable, {});
  const [sim] = simulation(c.dueSummaries(true));
  assert.equal(sim.calls, 2);
  for (const suffix of SUFFIXES) assert.equal(sim[`hits_${suffix}`], 0);
});

test("a simulation summary is totals only", async () => {
  const t = capture();
  const m = meter(t, { optimize: true });
  class Anthropic {
    constructor() {
      this.messages = {
        create: async () => ({
          model: "claude-sonnet-4-6",
          usage: { input_tokens: 1000, output_tokens: 200 },
        }),
      };
    }
  }
  const client = m.wrap(new Anthropic(), { featureId: "answer-generation" });
  for (let i = 0; i < 2; i += 1) {
    await client.messages.create({ model: "claude-sonnet-4-6", messages: [{ content: SECRET }] });
  }
  const keepAlive = setTimeout(() => {}, 3000);
  try {
    await m.flush();
  } finally {
    clearTimeout(keepAlive);
  }

  const sims = simulation(t.events());
  const expected = new Set(["kind", "scope_kind", "calls", "tokens_in", "tokens_out"]);
  for (const suffix of SUFFIXES) {
    for (const f of ["hits_", "hit_tokens_in_", "hit_tokens_out_", "evicted_"]) {
      expected.add(`${f}${suffix}`);
    }
  }
  assert.ok(sims.length > 0);
  for (const sim of sims) assert.deepEqual(new Set(Object.keys(sim)), expected);
  assert.equal(sims.reduce((n, s) => n + s.calls, 0), 2);
  assert.equal(sims.reduce((n, s) => n + s.hits_10m, 0), 1);
  assert.ok(!JSON.stringify(t.batches).includes(SECRET));
});

test("the one-hour cache lifetime is counted from the same gaps", () => {
  const clock = { t: 0 };
  const c = collector(clock, { cacheWindowMs: 300_000 });
  const request = { system: "static", messages: [{ role: "user" }] };
  for (const step of [0, 400_000, 400_000, 4_000_000]) {
    clock.t += step;
    c.onCall("anthropic", "m", request, {});
  }
  const [prefix] = c
    .dueSummaries(true)
    .map((e) => e.signal)
    .filter((s) => s.kind === "prefix");
  assert.equal(prefix.cache_windows, 4);
  assert.equal(prefix.cache_windows_1h, 2);
});
