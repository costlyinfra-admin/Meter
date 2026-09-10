/** The Node SDK: lifecycle, propagation, privacy, and never breaking the agent. */
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { Meter, usageOf, VERSION } from "../index.mjs";

const URL = "https://app.test/api/hook/events";

/** A fetch that records what would have been posted. */
function capture({ fail = 0, status = 0 } = {}) {
  const state = { batches: [], calls: 0 };
  state.fetch = async (url, opts) => {
    state.calls += 1;
    state.batches.push(JSON.parse(opts.body));
    if (state.calls <= fail) {
      if (status) return { ok: false, status };
      throw new Error("network");
    }
    return { ok: true, status: 200 };
  };
  state.events = () => state.batches.flatMap((b) => b.events);
  state.of = (kind) => state.events().filter((e) => e.event_type === kind);
  return state;
}

function meter(t, extra = {}) {
  return new Meter({
    application: "support-agent",
    environment: "production",
    ingestUrl: URL,
    token: "tok",
    fetchImpl: t.fetch,
    flushIntervalMs: 1,
    env: {},
    ...extra,
  });
}

/** A response shaped like Anthropic's. */
const response = (over = {}) => ({
  model: "claude-sonnet-4-6",
  usage: { input_tokens: 1000, output_tokens: 200, ...over },
});

class Anthropic {
  constructor() {
    this.messages = { create: async () => response() };
  }
}

// --- the simple case ------------------------------------------------------
test("a wrapped call produces a complete one-span trace", async () => {
  const t = capture();
  const m = meter(t);
  await m.wrap(new Anthropic(), { featureId: "answer-generation" }).messages.create({});
  await m.flush();

  assert.deepEqual(
    t.events().map((e) => e.event_type),
    ["trace.started", "span.started", "span.completed", "trace.completed"],
  );
  const done = t.of("span.completed")[0];
  assert.equal(done.tokens_in, 1000);
  assert.equal(done.tokens_out, 200);
  assert.equal(done.model, "claude-sonnet-4-6");
  assert.equal(done.feature_id, "answer-generation");
  assert.equal(done.span_kind, "llm");
  assert.equal(new Set(t.events().map((e) => e.trace_id)).size, 1);
});

test("a wrapped client returns the provider response untouched", async () => {
  const t = capture();
  const original = response();
  const client = new Anthropic();
  client.messages.create = async () => original;
  assert.equal(await meter(t).wrap(client).messages.create({}), original);
});

test("a wrapped call that throws is recorded and re-thrown", async () => {
  const t = capture();
  const m = meter(t);
  const client = new Anthropic();
  client.messages.create = async () => {
    throw new Error("upstream 500: SECRET-DETAIL");
  };
  await assert.rejects(() => m.wrap(client).messages.create({}), /upstream 500/);
  await m.flush();

  const kinds = t.events().map((e) => e.event_type);
  assert.deepEqual(kinds.slice(-2), ["span.failed", "trace.failed"]);
  // The error's text is the customer's; it is not ours to copy.
  assert.ok(!JSON.stringify(t.batches).includes("SECRET-DETAIL"));
});

test("a method off the instrumented path is passed straight through", async () => {
  const t = capture();
  const client = new Anthropic();
  client.countTokens = async () => 42;
  assert.equal(await meter(t).wrap(client).countTokens(), 42);
  await meter(t).flush();
  assert.equal(t.events().length, 0);
});

// --- multi-step agents ----------------------------------------------------
test("an agent run records its steps in order", async () => {
  const t = capture();
  const m = meter(t);
  await m.agent("resolve-ticket", { featureId: "f1", customerId: "customer-123" }, async (run) => {
    await run.llm("classify", async () => response());
    await run.tool("retrieve-documents", async () => ["doc"]);
    return run.llm("generate-answer", async () => response());
  });
  await m.flush();

  const kinds = t.events().map((e) => e.event_type);
  assert.equal(kinds[0], "trace.started");
  assert.equal(kinds.at(-1), "trace.completed");
  const completed = t.of("span.completed");
  assert.deepEqual(
    completed.map((e) => e.operation_name),
    ["classify", "retrieve-documents", "generate-answer"],
  );
  assert.deepEqual(
    completed.map((e) => e.span_kind),
    ["llm", "tool", "llm"],
  );
  // A tool call has no tokens; only the model calls cost anything.
  assert.equal(completed[1].tokens_in, undefined);
  assert.ok(completed.every((e) => e.customer_id === "customer-123"));
});

test("an agent returns its callback's value", async () => {
  const m = meter(capture());
  const out = await m.agent("resolve", {}, async (run) => run.tool("fetch", async () => ({ rows: 3 })));
  assert.deepEqual(out, { rows: 3 });
});

test("a failing step fails its span and the run, and re-throws", async () => {
  const t = capture();
  const m = meter(t);
  await assert.rejects(
    () =>
      m.agent("resolve", {}, async (run) =>
        run.tool("explode", async () => {
          throw new Error("boom");
        }),
      ),
    /boom/,
  );
  await m.flush();
  assert.equal(t.of("span.failed").length, 1);
  assert.equal(t.of("trace.failed").length, 1);
  assert.equal(t.of("trace.completed").length, 0);
});

// --- heartbeats -----------------------------------------------------------
test("a short run sends no heartbeat at all", async () => {
  const t = capture();
  const m = meter(t, { heartbeatMs: 1000 });
  await m.agent("quick", {}, async () => null);
  await m.flush();
  assert.equal(t.of("trace.heartbeat").length, 0);
  assert.deepEqual(
    t.events().map((e) => e.event_type),
    ["trace.started", "trace.completed"],
  );
});

test("a long run heartbeats, and stops the moment it ends", async () => {
  const t = capture();
  const m = meter(t, { heartbeatMs: 1000 });
  await m.agent("slow", {}, async () => new Promise((r) => setTimeout(r, 1200)));
  await m.flush();
  const beats = t.of("trace.heartbeat").length;
  assert.ok(beats >= 1, "it beat while working");

  await new Promise((r) => setTimeout(r, 1400));
  await m.flush();
  // A completed run must never keep claiming to be alive.
  assert.equal(t.of("trace.heartbeat").length, beats);
});

test("the heartbeat interval has a floor", () => {
  // A pathological config must not become a denial of service against the
  // customer's own ingest endpoint.
  assert.ok(meter(capture(), { heartbeatMs: 1 })._heartbeatMs >= 1000);
});

// --- context propagation --------------------------------------------------
test("exported context carries identifiers and nothing else", async () => {
  const t = capture();
  const m = meter(t, { token: "super-secret-token" });
  let context;
  await m.agent("resolve", { featureId: "f1", customerId: "customer-123" }, async (run) => {
    context = run.exportContext();
  });
  assert.equal(context.application, "support-agent");
  assert.equal(context.feature_id, "f1");
  // A context goes on a queue, which means assuming it gets logged.
  const blob = JSON.stringify(context);
  assert.ok(!blob.includes("super-secret-token"));
  assert.ok(!blob.includes("customer-123"));
});

test("resuming continues the same trace in another worker", async () => {
  const t = capture();
  const m = meter(t);
  let context;
  await m.agent("resolve-ticket", { featureId: "f1" }, async (run) => {
    context = run.exportContext();
    await run.tool("enqueue", async () => null);
  });
  await m.resume(context, async (run) => run.tool("process-document", async () => "done"));
  await m.flush();

  // Both "processes" wrote to one trace, which is the whole point.
  assert.equal(new Set(t.events().map((e) => e.trace_id)).size, 1);
});

test("resumed work hangs off the step that queued it", async () => {
  const t = capture();
  const m = meter(t);
  let context;
  await m.agent("resolve", {}, async (run) => {
    context = { ...run.exportContext(), parent_span_id: "queueing-step" };
  });
  await m.resume(context, async (run) => run.tool("process", async () => null));
  await m.flush();

  const child = t.of("span.completed").find((e) => e.operation_name === "process");
  assert.equal(child.parent_span_id, "queueing-step");
});

test("a context survives JSON round-tripping", async () => {
  const t = capture();
  const m = meter(t);
  let context;
  await m.agent("resolve", {}, async (run) => {
    context = run.exportContext();
  });
  await m.resume(JSON.stringify(context), async (run) => {
    assert.equal(run.traceId, context.trace_id);
  });
});

// --- privacy --------------------------------------------------------------
test("no prompt or response content is ever serialized", async () => {
  const t = capture();
  const m = meter(t);
  await m
    .wrap(new Anthropic())
    .messages.create({ messages: [{ role: "user", content: "MY-SECRET-PROMPT" }] });
  await m.agent("resolve", {}, async (run) =>
    run.tool("search", async () => ["MY-SECRET-DOCUMENT"]),
  );
  await m.flush();

  const wire = JSON.stringify(t.batches);
  for (const secret of ["MY-SECRET-PROMPT", "MY-SECRET-DOCUMENT"]) {
    assert.ok(!wire.includes(secret), secret);
  }
});

test("prompt identity travels without the prompt", async () => {
  const t = capture();
  const m = meter(t);
  await m.agent("resolve", {}, async (run) =>
    run.llm("answer", async () => response(), {
      promptId: "answer-ticket",
      promptVersion: "5.0",
      promptHash: "abc123",
    }),
  );
  await m.flush();
  const done = t.of("span.completed")[0];
  assert.equal(done.prompt_id, "answer-ticket");
  assert.equal(done.prompt_version, "5.0");
  assert.equal(done.prompt_hash, "abc123");
});

test("every field sent is one the server allows", async () => {
  const t = capture();
  const m = meter(t);
  await m.agent("resolve", { customerId: "c1" }, async (run) =>
    run.llm("answer", async () =>
      response({ cache_read_input_tokens: 800, cache_creation_input_tokens: 200 }),
    ),
  );
  await m.flush();

  const allowed = new Set([
    "event_type", "event_id", "trace_id", "span_id", "parent_span_id", "span_kind",
    "operation_name", "application", "feature_id", "provider", "model", "tokens_in",
    "tokens_out", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
    "latency_ms", "prompt_id", "prompt_version", "prompt_hash", "environment",
    "release_version", "customer_id", "occurred_at",
  ]);
  for (const event of t.events()) {
    for (const key of Object.keys(event)) assert.ok(allowed.has(key), `unexpected field ${key}`);
  }
});

// --- token extraction -----------------------------------------------------
test("anthropic cache tokens are extracted", () => {
  const usage = usageOf(
    response({ cache_read_input_tokens: 6100, cache_creation_input_tokens: 1200 }),
  );
  assert.equal(usage.cache_read_tokens, 6100);
  assert.equal(usage.cache_write_tokens, 1200);
  assert.equal(usage.provider, "anthropic");
});

test("openai reasoning tokens are extracted", () => {
  const usage = usageOf({
    model: "gpt-4o",
    usage: {
      prompt_tokens: 900,
      completion_tokens: 120,
      prompt_tokens_details: { cached_tokens: 400 },
      completion_tokens_details: { reasoning_tokens: 64 },
    },
  });
  assert.equal(usage.tokens_in, 900);
  assert.equal(usage.cache_read_tokens, 400);
  assert.equal(usage.reasoning_tokens, 64);
  assert.equal(usage.provider, "openai");
});

test("google token counts are extracted", () => {
  const usage = usageOf({
    modelVersion: "gemini-2.5-flash",
    usageMetadata: { promptTokenCount: 500, candidatesTokenCount: 90 },
  });
  assert.equal(usage.tokens_in, 500);
  assert.equal(usage.provider, "google");
});

test("an unfamiliar response still records the step", async () => {
  const t = capture();
  const m = meter(t);
  await m.agent("x", {}, async (run) => run.llm("call", async () => ({ weird: true })));
  await m.flush();
  // Timing and status survive even when tokens cannot be read.
  const done = t.of("span.completed")[0];
  assert.equal(done.operation_name, "call");
  assert.ok(typeof done.latency_ms === "number");
});

test("latency is measured without the caller doing anything", async () => {
  const t = capture();
  const m = meter(t);
  await m.agent("x", {}, async (run) =>
    run.tool("slow", () => new Promise((r) => setTimeout(r, 40))),
  );
  await m.flush();
  assert.ok(t.of("span.completed")[0].latency_ms >= 30);
});

// --- delivery -------------------------------------------------------------
test("unconfigured is a silent no-op", async () => {
  const m = new Meter({ application: "x", env: {} });
  assert.equal(m.enabled, false);
  const out = await m.agent("resolve", {}, async (run) => run.tool("work", async () => 42));
  assert.equal(out, 42);
  assert.equal(await m.flush(), true);
});

test("a broken endpoint never reaches the caller", async () => {
  const m = meter(
    { fetch: async () => { throw new Error("refused"); } },
    { maxAttempts: 1, retryBackoffMs: [0] },
  );
  const out = await m.agent("resolve", {}, async (run) => run.tool("work", async () => "value"));
  assert.equal(out, "value");
  await m.flush();
  assert.ok(m.dropped > 0, "degradation is visible, not silent");
});

test("a retry reuses one batch id so the server can dedupe", async () => {
  const t = capture({ fail: 1 });
  const m = meter(t, { retryBackoffMs: [0] });
  await m.agent("resolve", {}, async (run) => run.llm("answer", async () => response()));
  await m.flush();
  assert.ok(t.calls >= 2);
  // Identical id across attempts is what makes an ambiguous timeout safe.
  assert.equal(new Set(t.batches.map((b) => b.batch_id)).size, 1);
});

test("a rejected batch is dropped rather than hammered", async () => {
  const t = capture({ fail: 99, status: 400 });
  const m = meter(t, { retryBackoffMs: [0] });
  await m.agent("resolve", {}, async () => null);
  await m.flush();
  // A 400 fails identically forever; retrying only hurts the endpoint.
  assert.equal(t.calls, 1);
  assert.ok(m.dropped > 0);
});

test("the queue is bounded and sheds oldest first", () => {
  const m = meter(capture(), { queueMax: 5, batchSize: 1000, flushIntervalMs: 60_000 });
  for (let i = 0; i < 50; i += 1) {
    m._send([{ event_type: "trace.heartbeat", trace_id: `t${i}` }]);
  }
  assert.ok(m._queue.length <= 5);
  assert.ok(m.dropped >= 45);
  // The newest always gets in.
  assert.equal(m._queue.at(-1).trace_id, "t49");
});

test("ids are generated client-side and are unique", async () => {
  const t = capture();
  const m = meter(t);
  const ids = new Set();
  await m.agent("a", {}, async (run) => ids.add(run.traceId));
  await m.agent("b", {}, async (run) => ids.add(run.traceId));
  assert.equal(ids.size, 2);
  assert.ok([...ids][0].length >= 16);
});

test("configuration comes from the documented environment", () => {
  const m = new Meter({
    env: {
      METER_INGEST_URL: URL,
      METER_INGEST_TOKEN: "env-token",
      METER_APPLICATION: "document-review",
      METER_ENVIRONMENT: "staging",
      METER_RELEASE_VERSION: "2026.9.1",
    },
    fetchImpl: async () => ({ ok: true }),
  });
  assert.equal(m.enabled, true);
  assert.equal(m.application, "document-review");
  assert.equal(m.environment, "staging");
  assert.equal(m.releaseVersion, "2026.9.1");
});

test("environment and release ride on every event", async () => {
  const t = capture();
  const m = meter(t, { environment: "staging", releaseVersion: "2026.9.1" });
  await m.agent("resolve", {}, async (run) => run.tool("x", async () => null));
  await m.flush();
  assert.ok(t.events().every((e) => e.environment === "staging"));
  assert.ok(t.events().every((e) => e.release_version === "2026.9.1"));
});

test("the version the SDK reports is the version the package ships", () => {
  // Not a hardcoded number, which has to be edited on every release and says
  // nothing when it is: the point is that the constant a customer quotes in a
  // bug report matches what npm actually served them.
  // Resolved with path helpers rather than `new URL(...)`: this file declares
  // its own module-scope `URL` for the ingest endpoint, which shadows the
  // global constructor.
  const here = dirname(fileURLToPath(import.meta.url));
  const pkg = JSON.parse(readFileSync(join(here, "..", "package.json"), "utf8"));
  assert.equal(VERSION, pkg.version);
});
