// Live tests (EX-3): tagging calls with a test and group, and quality scores.
// Mirrors the Python SDK's tests/test_live.py.

import assert from "node:assert";
import test from "node:test";
import { Meter } from "../index.mjs";

const EXP = "6f1c1b3e-1d1f-4c64-9a0b-5d0b6f1b9a11";
const SECRET = "the quick brown fox jumped over the lazy dog";

function capture() {
  const state = { batches: [] };
  state.fetch = async (url, opts) => {
    state.batches.push(JSON.parse(opts.body));
    return { ok: true, status: 200 };
  };
  state.events = () => state.batches.flatMap((b) => b.events);
  return state;
}

function meter(t) {
  return new Meter({
    application: "support-agent",
    ingestUrl: "https://app.test/api/hook/events",
    token: "tok",
    fetchImpl: t.fetch,
    flushIntervalMs: 1,
    env: {},
  });
}

function client(m, { fail = false, ...options } = {}) {
  class Anthropic {
    constructor() {
      this.messages = {
        create: async () => {
          if (fail) throw new Error("overloaded");
          return { model: "claude-haiku-4-5", usage: { input_tokens: 10, output_tokens: 2 } };
        },
      };
    }
  }
  return m.wrap(new Anthropic(), { featureId: "answer-generation", ...options });
}

const flush = async (m) => {
  const keepAlive = setTimeout(() => {}, 3000);
  try {
    await m.flush();
  } finally {
    clearTimeout(keepAlive);
  }
};

test("every call is tagged with its test and group", async () => {
  const t = capture();
  const m = meter(t);
  await client(m, { experiment: EXP, group: "candidate" }).messages.create({
    model: "claude-haiku-4-5",
    messages: [{ content: SECRET }],
  });
  await flush(m);
  const [done] = t.events().filter((e) => e.event_type === "span.completed");
  assert.deepEqual([done.experiment_id, done.experiment_group], [EXP, "candidate"]);
  assert.ok(!JSON.stringify(t.batches).includes(SECRET));
});

test("a failed call counts against its group", async () => {
  const t = capture();
  const m = meter(t);
  await assert.rejects(
    client(m, { fail: true, experiment: EXP, group: "candidate" }).messages.create({ model: "m" }),
  );
  await flush(m);
  const [failed] = t.events().filter((e) => e.event_type === "span.failed");
  assert.deepEqual([failed.experiment_id, failed.experiment_group], [EXP, "candidate"]);
});

test("an untagged client sends no tag at all", async () => {
  const t = capture();
  const m = meter(t);
  await client(m).messages.create({ model: "m" });
  await flush(m);
  const [done] = t.events().filter((e) => e.event_type === "span.completed");
  assert.ok(!("experiment_id" in done) && !("experiment_group" in done));
});

test("a tagging mistake is the developer's to fix at once", () => {
  const m = meter(capture());
  assert.throws(() => client(m, { experiment: EXP }), /both experiment and group/);
  assert.throws(() => client(m, { group: "control" }), /both experiment and group/);
  assert.throws(() => client(m, { experiment: EXP, group: "treatment" }), /"control" or "candidate"/);
});

test("a score is a number for a group and nothing else", async () => {
  const t = capture();
  const m = meter(t);
  m.score(EXP, "control", 1);
  m.score(EXP, "candidate", 0.5);
  await flush(m);
  const scores = t.events().filter((e) => e.event_type === "experiment.score");
  assert.deepEqual(
    scores.map((s) => [s.experiment_group, s.score]),
    [
      ["control", 1],
      ["candidate", 0.5],
    ],
  );
  assert.deepEqual(Object.keys(scores[0]).sort(), [
    "event_id",
    "event_type",
    "experiment_group",
    "experiment_id",
    "occurred_at",
    "score",
  ]);
});

test("a score that is not a number is refused", () => {
  const m = meter(capture());
  for (const bad of [true, "good", Number.NaN, null]) {
    assert.throws(() => m.score(EXP, "control", bad), /must be a number/);
  }
});
