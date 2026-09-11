/** Consented prompt capture: off by default, nothing kept until the server says
 *  capture is open, text only, and never at metering's expense. */
import assert from "node:assert";
import test from "node:test";
import { Meter } from "../index.mjs";

const URL = "https://app.test/api/hook/events";
const CAPTURE = "https://app.test/api/prompt-capture/samples";
const OPEN = "https://app.test/api/prompt-capture/open";
const SECRET = "SECRET-PROMPT-TEXT";

/** Meter's side of both channels: events, the open check, and samples. */
function server({ open = true, sampleStatus = 200, reason = null, captureDown = false } = {}) {
  const state = { batches: [], samples: [], checks: [], attempts: 0 };
  state.fetch = async (url, opts = {}) => {
    if (url.startsWith(OPEN)) {
      state.checks.push(url);
      return { ok: true, status: 200, json: async () => ({ open }) };
    }
    if (url === CAPTURE) {
      state.attempts += 1;
      if (captureDown) throw new Error("capture endpoint unreachable");
      if (sampleStatus !== 200) {
        return { ok: false, status: sampleStatus, json: async () => ({ reason }) };
      }
      state.samples.push(JSON.parse(opts.body));
      return { ok: true, status: 200, json: async () => ({ stored: true }) };
    }
    state.batches.push(JSON.parse(opts.body));
    return { ok: true, status: 200 };
  };
  state.events = () => state.batches.flatMap((b) => b.events);
  return state;
}

function meter(s, extra = {}) {
  return new Meter({
    application: "support-agent",
    ingestUrl: URL,
    token: "tok",
    fetchImpl: s.fetch,
    flushIntervalMs: 1,
    env: {},
    capturePrompts: true,
    captureSampleRate: 1,
    ...extra,
  });
}

const anthropicReply = (over = {}) => ({
  model: "claude-sonnet-4-6",
  content: [{ type: "text", text: "billing" }],
  usage: { input_tokens: 1200, output_tokens: 12 },
  ...over,
});

const anthropic = (reply = anthropicReply()) => ({ messages: { create: async () => reply } });

const request = (over = {}) => ({
  model: "claude-sonnet-4-6",
  system: `You classify security alerts. ${SECRET}-SYSTEM`,
  messages: [{ role: "user", content: `Alert: ${SECRET}-INPUT` }],
  max_tokens: 200,
  temperature: 0,
  ...over,
});

async function warmed(s = server(), extra = {}, client = anthropic()) {
  const m = meter(s, extra);
  const wrapped = m.wrap(client, {
    provider: "anthropic",
    featureId: "f-1",
    promptId: "classify-alert",
    promptVersion: "v7",
  });
  await wrapped.messages.create(request());
  await m.flush(); // the first call only asks whether capture is open
  return { s, m, wrapped };
}

test("capture is off by default", async () => {
  const s = server();
  const m = new Meter({ ingestUrl: URL, token: "tok", fetchImpl: s.fetch, env: {}, flushIntervalMs: 1 });
  const client = m.wrap(anthropic(), { provider: "anthropic", featureId: "f-1", promptId: "p", promptVersion: "v1" });
  await client.messages.create(request());
  await client.messages.create(request());
  await m.flush();
  assert.equal(m.captureEnabled, false);
  assert.deepEqual([s.checks.length, s.samples.length], [0, 0]);
  assert.ok(s.events().length > 0);
});

test("the first call only asks, and a closed answer sends nothing", async () => {
  const { s, m, wrapped } = await warmed(server({ open: false }));
  assert.equal(s.checks.length, 1);
  for (let i = 0; i < 3; i += 1) await wrapped.messages.create(request());
  await m.flush();
  assert.equal(s.attempts, 0);
  assert.equal(s.checks.length, 1);
});

test("a sample carries the text and the prompt identity", async () => {
  const { s, m, wrapped } = await warmed();
  await wrapped.messages.create(request());
  await m.flush();
  assert.equal(s.samples.length, 1);
  const [sample] = s.samples;
  assert.ok(sample.template.startsWith("You classify security alerts."));
  assert.deepEqual(sample.input, [{ role: "user", text: `Alert: ${SECRET}-INPUT` }]);
  assert.equal(sample.output, "billing");
  assert.deepEqual(
    [sample.feature_id, sample.prompt_id, sample.prompt_version],
    ["f-1", "classify-alert", "v7"],
  );
  assert.deepEqual(sample.parameters, { temperature: 0, max_tokens: 200 });
});

test("tool calls, tool results and images are never read", async () => {
  const { s, m, wrapped } = await warmed();
  await wrapped.messages.create(
    request({
      messages: [
        {
          role: "user",
          content: [
            { type: "text", text: "Look at this alert" },
            { type: "image", source: { data: "IMAGE-SECRET" } },
          ],
        },
        { role: "assistant", content: [{ type: "tool_use", input: { host: "TOOL-SECRET" } }] },
        {
          role: "user",
          content: [
            { type: "tool_result", content: "TOOL-SECRET" },
            { type: "text", text: "What now?" },
          ],
        },
      ],
    }),
  );
  await m.flush();
  const blob = JSON.stringify(s.samples);
  assert.ok(!blob.includes("TOOL-SECRET") && !blob.includes("IMAGE-SECRET"));
  assert.deepEqual(s.samples[0].input, [
    { role: "user", text: "Look at this alert" },
    { role: "user", text: "What now?" },
  ]);
});

test("OpenAI system and developer text is the template; tool messages are skipped", async () => {
  const s = server();
  const m = meter(s);
  const reply = {
    model: "gpt-4o",
    choices: [{ message: { content: "phishing" } }],
    usage: { prompt_tokens: 900, completion_tokens: 5 },
  };
  const client = m.wrap(
    { chat: { completions: { create: async () => reply } } },
    { provider: "openai", featureId: "f-1", promptId: "classify-alert", promptVersion: "v7" },
  );
  const call = {
    model: "gpt-4o",
    messages: [
      { role: "system", content: "You classify alerts." },
      { role: "developer", content: "Answer with one word." },
      { role: "user", content: "Alert text" },
      { role: "tool", content: "TOOL-SECRET" },
    ],
    response_format: { type: "json_object" },
    max_completion_tokens: 300,
  };
  await client.chat.completions.create(call);
  await m.flush();
  await client.chat.completions.create(call);
  await m.flush();
  const [sample] = s.samples;
  assert.equal(sample.template, "You classify alerts.\n\nAnswer with one word.");
  assert.deepEqual(sample.input, [{ role: "user", text: "Alert text" }]);
  assert.deepEqual(sample.parameters, { max_tokens: 300, response_format: "json_object" });
  assert.ok(!JSON.stringify(sample).includes("TOOL-SECRET"));
});

test("metering events still never carry prompt text", async () => {
  const { s, m, wrapped } = await warmed();
  await wrapped.messages.create(request());
  await m.flush();
  assert.equal(s.samples.length, 1);
  assert.ok(!JSON.stringify(s.batches).includes(SECRET));
  const done = s.events().filter((e) => e.event_type === "span.completed").at(-1);
  assert.deepEqual([done.prompt_id, done.prompt_version], ["classify-alert", "v7"]);
});

test("a failing redactor sends nothing", async () => {
  const { s, m, wrapped } = await warmed(server(), {
    redact: () => {
      throw new Error("no");
    },
  });
  await wrapped.messages.create(request());
  await m.flush();
  assert.equal(s.attempts, 0);
  assert.equal(m.captureDropped, 1);
});

test("an oversized sample is dropped, not truncated", async () => {
  const { s, m, wrapped } = await warmed();
  await wrapped.messages.create(request({ system: "x".repeat(64 * 1024 + 1) }));
  await m.flush();
  assert.equal(s.attempts, 0);
  assert.equal(m.captureDropped, 1);
});

test("a refusal closes capture for the feature", async () => {
  const { s, m, wrapped } = await warmed(server({ sampleStatus: 403, reason: "feature_not_enabled" }));
  await wrapped.messages.create(request());
  await m.flush();
  for (let i = 0; i < 3; i += 1) await wrapped.messages.create(request());
  await m.flush();
  assert.equal(s.attempts, 1);
});

test("a broken capture endpoint never touches metering or the caller", async () => {
  const { s, m, wrapped } = await warmed(server({ captureDown: true }));
  const reply = await wrapped.messages.create(request());
  await m.flush();
  assert.equal(reply.content[0].text, "billing");
  assert.equal(m.captureDropped, 1);
  assert.equal(m.dropped, 0);
  assert.ok(s.events().some((e) => e.event_type === "span.completed"));
});

test("capture can be switched on by environment and finds its endpoint", () => {
  const m = new Meter({ ingestUrl: URL, token: "tok", env: { METER_CAPTURE_PROMPTS: "true" } });
  assert.equal(m.captureEnabled, true);
  assert.equal(m.captureUrl, CAPTURE);
  const custom = new Meter({ ingestUrl: "https://app.test/custom", token: "tok", capturePrompts: true, env: {} });
  assert.equal(custom.captureEnabled, false);
});
