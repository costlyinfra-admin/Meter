/**
 * Meter — request-level AI economics.
 *
 * What this measures is not requests in general. It measures AI work: an agent
 * run, and the LLM calls, retrievals, tool calls, guardrails and evaluations
 * inside it. Meter is a cost product, not an APM.
 *
 *   const meter = new Meter({ application: "support-agent" });
 *   const client = meter.wrap(anthropic, { featureId: "answer-generation" });
 *   await client.messages.create(...);            // a one-span trace
 *
 *   await meter.agent("resolve-ticket", { featureId: "f1" }, async (run) => {
 *     await run.llm("classify", () => anthropic.messages.create(...));
 *     const docs = await run.tool("retrieve-documents", retrieveDocuments);
 *     return run.llm("generate-answer", () => anthropic.messages.create(...));
 *   });
 *
 * **What metering never sends.** Prompts, responses, messages, tool arguments,
 * tool results, retrieved documents, exception messages and stack traces. The
 * metering events this SDK constructs have no field for them, and the server
 * rejects a payload that carries one. What travels is identity, counts, timing
 * and money.
 *
 * **The one exception, off by default: consented prompt capture.** For Prompt
 * Optimization, `new Meter({ capturePrompts: true })` lets a wrapped client send
 * a small sample of prompt text (the system prompt, the text of the messages and
 * the text of the reply) for calls named with `promptId` and `promptVersion`. It
 * sends nothing unless your organization has also agreed in Meter and switched
 * capture on for that feature. It never sends tool calls, tool results, images or
 * files, and it travels on its own channel, so it can never slow or break metering.
 *
 * **It must never break your agent.** Every path is guarded: a Meter failure
 * degrades to sending nothing. Delivery is batched off your call path over a
 * bounded queue that sheds oldest-first, and `meter.dropped` says if it did.
 */

import { createHash, randomUUID } from "node:crypto";

export const VERSION = "2.3.0";

const FLUSH_INTERVAL_MS = 2000;
const BATCH_SIZE = 50;
const QUEUE_MAX = 10_000;
const MAX_ATTEMPTS = 3;
const RETRY_BACKOFF_MS = [500, 2000];
/** Well inside a sensible stale threshold, so a working agent is never mistaken
 *  for a hung one. Floored, so a bad config cannot become a denial of service
 *  against the customer's own endpoint. */
const HEARTBEAT_MS = 30_000;
const HEARTBEAT_FLOOR_MS = 1000;

/** Consented prompt capture. 1 call in 100, at most 50 samples a day per prompt:
 *  optimization needs a representative handful, not a copy of the traffic. The
 *  server enforces its own caps whatever these are set to. */
const CAPTURE_SAMPLE_RATE = 0.01;
const CAPTURE_MAX_PER_DAY = 50;
/** Dropped, not truncated, above this: a truncated prompt would later be
 *  evaluated as a different prompt. Matches the server's limit. */
const CAPTURE_MAX_BYTES = 64 * 1024;
const CAPTURE_INFLIGHT_MAX = 100;
/** How long the server's "capture is open for this feature" is trusted. A
 *  withdrawal also takes effect on the very next sample, which the server
 *  re-checks every time. */
const CAPTURE_OPEN_TTL_MS = 5 * 60 * 1000;
/** The call shapes whose text can be told apart from tool calls reliably. */
// -- optimize mode --------------------------------------------------------
/** How much of a request counts as its cacheable static head when the
 *  provider's own static blocks are not separable. */
const PREFIX_CHARS = 2000;
/** Prefix counters are aggregates; they leave on a timer, not per call. */
const OPTIMIZE_FLUSH_INTERVAL_MS = 60_000;
/** Bounds. A fingerprint map that grows with traffic is a leak, not a feature. */
const DUP_CAPACITY = 5000;
const PREFIX_CAPACITY = 512;
/** How long a first call stays a plausible cache hit for a later identical one.
 *  There was no window at all: a request repeated six hours later counted as an
 *  avoidable duplicate, which asserts the first response was still good — a
 *  freshness claim nobody had checked. Measured from the FIRST call of a group
 *  and never extended, so steady traffic cannot keep one supposed cached
 *  response alive forever. See the Python SDK for the full note. */
const DUPLICATE_WINDOW_MS = 600_000;
/**
 * How long a provider keeps a prompt prefix cached after it is written.
 *
 * Caching is not free: writing a prefix into the cache costs MORE than sending
 * it uncached, and only the reads that follow pay that back. So the number of
 * writes decides whether caching this prefix saves money at all, and the only
 * place that can be counted is here, next to the call timestamps.
 *
 * A window opens when a prefix is seen after a gap longer than this — the point
 * at which a provider would have evicted it and the next call would have to
 * write it again. Five minutes is the shortest TTL the priced providers offer
 * and their default, so counting windows at five minutes counts the MOST writes
 * caching could incur, and the resulting saving is the most conservative one.
 */
const CACHE_WINDOW_MS = 300_000;

const CAPTURE_PATHS = [
  ["anthropic", "messages.create"],
  ["openai", "chat.completions.create"],
];

const SPAN_KINDS = new Set([
  "workflow",
  "llm",
  "embedding",
  "retrieval",
  "tool",
  "guardrail",
  "evaluation",
]);

/** Generated here, not by the server: a client-side id lets a span be referred
 *  to before the server has seen it, which is what makes out-of-order delivery,
 *  resumed work in another process, and retry-safe replay all work. */
const newId = () => randomUUID().replace(/-/g, "");
const nowIso = () => new Date().toISOString();

const COMPLETION_PATHS = {
  anthropic: [
    ["messages", "create"],
    ["completions", "create"],
  ],
  openai: [
    ["chat", "completions", "create"],
    ["responses", "create"],
    ["embeddings", "create"],
  ],
  google: [
    ["models", "generateContent"],
    ["generateContent"],
  ],
};

function pathMatches(paths, path) {
  return paths.some((p) => p.length === path.length && p.every((seg, i) => seg === path[i]));
}

function pathIsPrefix(paths, path) {
  return paths.some((p) => p.length >= path.length && path.every((seg, i) => p[i] === seg));
}

function detectProvider(client) {
  const name = (client?.constructor?.name ?? "").toLowerCase();
  for (const provider of ["anthropic", "openai", "mistral", "cohere", "groq"]) {
    if (name.includes(provider)) return provider;
  }
  if (name.includes("google") || name.includes("genai") || name.includes("gemini")) {
    return "google";
  }
  return "openai"; // the OpenAI-compatible shape is the common default
}

const int = (value) => {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? Math.round(n) : 0;
};

/**
 * Tokens, model and provider from a provider response.
 *
 * Read defensively: a shape this does not recognise yields no token fields
 * rather than throwing, and the span is still recorded with its timing and
 * status. Losing a step because a provider changed a key would be worse than
 * losing its token counts.
 */
export function usageOf(resp) {
  const out = {};
  if (!resp || typeof resp !== "object") return out;
  try {
    const model = resp.model ?? resp.modelVersion ?? resp.model_version;
    const usage = resp.usage ?? resp.usageMetadata ?? resp.usage_metadata;
    if (model) out.model = model;
    if (!usage || typeof usage !== "object") return out;

    // Anthropic
    let tin = int(usage.input_tokens ?? usage.inputTokens);
    let tout = int(usage.output_tokens ?? usage.outputTokens);
    let cacheRead = int(usage.cache_read_input_tokens ?? usage.cacheReadInputTokens);
    const cacheWrite = int(usage.cache_creation_input_tokens ?? usage.cacheCreationInputTokens);
    let provider = tin || tout || cacheRead || cacheWrite ? "anthropic" : null;

    if (!tin && !tout) {
      // OpenAI
      tin = int(usage.prompt_tokens ?? usage.promptTokens);
      tout = int(usage.completion_tokens ?? usage.completionTokens);
      const inDetails = usage.prompt_tokens_details ?? usage.promptTokensDetails ?? {};
      const outDetails = usage.completion_tokens_details ?? usage.completionTokensDetails ?? {};
      cacheRead = int(inDetails.cached_tokens ?? inDetails.cachedTokens);
      const reasoning = int(outDetails.reasoning_tokens ?? outDetails.reasoningTokens);
      if (reasoning) out.reasoning_tokens = reasoning;
      if (tin || tout) provider = "openai";
    }

    if (!tin && !tout) {
      // Google
      tin = int(usage.promptTokenCount ?? usage.prompt_token_count);
      tout = int(usage.candidatesTokenCount ?? usage.candidates_token_count);
      cacheRead = int(usage.cachedContentTokenCount ?? usage.cached_content_token_count);
      const reasoning = int(usage.thoughtsTokenCount ?? usage.thoughts_token_count);
      if (reasoning) out.reasoning_tokens = reasoning;
      if (tin || tout) provider = "google";
    }

    if (provider) out.provider = provider;
    if (tin) out.tokens_in = tin;
    if (tout) out.tokens_out = tout;
    if (cacheRead) out.cache_read_tokens = cacheRead;
    if (cacheWrite) out.cache_write_tokens = cacheWrite;
  } catch {
    return out;
  }
  return out;
}

/** One agent or workflow run, and the steps recorded inside it. */
export class AgentRun {
  constructor(meter, { operationName, featureId, customerId, application, traceId, parentSpanId }) {
    this._meter = meter;
    this.operationName = operationName;
    this.traceId = traceId ?? newId();
    this.featureId = featureId ?? null;
    this.customerId = customerId ?? null;
    this.application = application ?? null;
    /** Steps nest under this when the run was resumed from another process, so
     *  work continued in a worker still hangs off the step that queued it. */
    this.parentSpanId = parentSpanId ?? null;
    this._done = false;
    this._timer = null;
  }

  _start() {
    this._meter._emit(
      this._meter._event("trace.started", this.traceId, {
        operation_name: this.operationName,
        feature_id: this.featureId,
        customer_id: this.customerId,
        application: this.application,
      }),
    );
    this._scheduleHeartbeat();
  }

  _scheduleHeartbeat() {
    if (this._done || !this._meter.enabled) return;
    try {
      this._timer = setTimeout(() => {
        if (this._done) return;
        this._meter._emit(
          this._meter._event("trace.heartbeat", this.traceId, {
            application: this.application,
          }),
        );
        this._scheduleHeartbeat();
      }, this._meter._heartbeatMs);
      // Unref'd: metering must never hold a process open. A CLI that finishes
      // its work should exit, not wait on our timer.
      this._timer.unref?.();
    } catch {
      this._timer = null;
    }
  }

  _finish(eventType) {
    // Set BEFORE clearing, so a beat racing us sees the run is over and returns
    // rather than re-arming a timer nothing will cancel.
    this._done = true;
    if (this._timer) {
      try {
        clearTimeout(this._timer);
      } catch {
        /* nothing to do */
      }
      this._timer = null;
    }
    this._meter._emit(
      this._meter._event(eventType, this.traceId, {
        operation_name: this.operationName,
        application: this.application,
      }),
    );
  }

  /**
   * Run `fn`, record it as a step, and return whatever it returned.
   *
   * The step's status follows the call: a rejection makes it `error` and is
   * re-thrown unchanged. Nothing about the error is transmitted.
   */
  async span(kind, operationName, fn, options = {}) {
    const spanKind = SPAN_KINDS.has(kind) ? kind : "tool";
    const spanId = newId();
    const parent = options.parentSpanId ?? this.parentSpanId;
    const identity = {
      prompt_id: options.promptId ?? null,
      prompt_version: options.promptVersion ?? null,
      prompt_hash: options.promptHash ?? null,
    };
    this._emitSpan("span.started", spanId, spanKind, operationName, parent, {});
    const began = Date.now();
    let result;
    try {
      result = await fn();
    } catch (err) {
      this._emitSpan("span.failed", spanId, spanKind, operationName, parent, {
        latency_ms: Date.now() - began,
        ...identity,
      });
      throw err;
    }
    const usage = spanKind === "llm" || spanKind === "embedding" ? usageOf(result) : {};
    this._emitSpan("span.completed", spanId, spanKind, operationName, parent, {
      latency_ms: Date.now() - began,
      feature_id: options.featureId ?? null,
      ...identity,
      ...usage,
    });
    return result;
  }

  llm(operationName, fn, options) {
    return this.span("llm", operationName, fn, options);
  }
  tool(operationName, fn, options) {
    return this.span("tool", operationName, fn, options);
  }
  retrieval(operationName, fn, options) {
    return this.span("retrieval", operationName, fn, options);
  }
  embedding(operationName, fn, options) {
    return this.span("embedding", operationName, fn, options);
  }
  guardrail(operationName, fn, options) {
    return this.span("guardrail", operationName, fn, options);
  }
  evaluation(operationName, fn, options) {
    return this.span("evaluation", operationName, fn, options);
  }

  _emitSpan(eventType, spanId, spanKind, operationName, parent, fields) {
    this._meter._emit(
      this._meter._event(eventType, this.traceId, {
        span_id: spanId,
        parent_span_id: parent,
        span_kind: spanKind,
        operation_name: operationName,
        feature_id: fields.feature_id ?? this.featureId,
        customer_id: this.customerId,
        application: this.application,
        ...fields,
      }),
    );
  }

  /**
   * Identifiers a worker needs to continue this run — and nothing else.
   *
   * No token, no customer content: this is designed to go on a queue, which
   * means assuming it will be logged somewhere.
   */
  exportContext() {
    const context = {
      trace_id: this.traceId,
      parent_span_id: this.parentSpanId,
      application: this.application,
      feature_id: this.featureId,
      environment: this._meter.environment,
      operation_name: this.operationName,
    };
    if (this._meter.releaseVersion) context.release_version = this._meter.releaseVersion;
    return Object.fromEntries(Object.entries(context).filter(([, v]) => v != null));
  }
}

export class Meter {
  constructor(options = {}) {
    const env = options.env ?? process.env;
    this.application = options.application ?? env.METER_APPLICATION ?? "default";
    this.environment = options.environment ?? env.METER_ENVIRONMENT ?? "production";
    this.releaseVersion = options.releaseVersion ?? env.METER_RELEASE_VERSION ?? null;
    this.ingestUrl = options.ingestUrl ?? env.METER_INGEST_URL ?? null;
    this.token = options.token ?? env.METER_INGEST_TOKEN ?? null;
    this.featureId = options.featureId ?? null;

    this._fetch = options.fetchImpl ?? globalThis.fetch;
    this._batchSize = Math.max(1, options.batchSize ?? BATCH_SIZE);
    this._flushIntervalMs = Math.max(0, options.flushIntervalMs ?? FLUSH_INTERVAL_MS);
    this._queueMax = Math.max(1, options.queueMax ?? QUEUE_MAX);
    this._maxAttempts = Math.max(1, options.maxAttempts ?? MAX_ATTEMPTS);
    this._retryBackoffMs = options.retryBackoffMs ?? RETRY_BACKOFF_MS;
    this._heartbeatMs = Math.max(HEARTBEAT_FLOOR_MS, options.heartbeatMs ?? HEARTBEAT_MS);

    this._queue = [];
    this._timer = null;
    this._inflight = null;
    /** Events discarded because the queue was full or delivery failed for good.
     *  Metering degrades visibly rather than silently. */
    this.dropped = 0;

    // Consented prompt capture: the developer's half of a double key. The
    // organization's half lives in Meter, and nothing is sent without both.
    const envCapture = String(env.METER_CAPTURE_PROMPTS ?? "").trim().toLowerCase();
    this.capturePrompts =
      options.capturePrompts ?? ["1", "true", "yes", "on"].includes(envCapture);
    this.captureUrl =
      options.captureUrl ?? env.METER_CAPTURE_URL ?? captureUrlFrom(this.ingestUrl);
    this.captureSampleRate = Math.min(
      1,
      Math.max(0, options.captureSampleRate ?? CAPTURE_SAMPLE_RATE),
    );
    this._redact = options.redact ?? null;
    /** featureId -> { open, until } */
    this._captureOpen = new Map();
    this._captureChecking = new Set();
    /** "featureId|promptId" -> { day, count } */
    this._captureDaily = new Map();
    this._captureCapped = new Map();
    this._captureInflight = new Set();
    /** Samples chosen but not delivered: refused, too large, redacted away or
     *  failed. Calls simply not sampled are not counted. */
    this.captureDropped = 0;

    // Optimize mode: measure the SHAPE of traffic — salted-hash fingerprints
    // and counts, never prompt text — so the duplicate-call and uncached-prefix
    // findings are measured rather than rules of thumb. Off by default.
    this._salt = options.salt ?? null;
    this._saltState = this._salt ? "ready" : "cold";
    this._optimizer = options.optimize
      ? new Optimizer(this, {
          prefixChars: options.prefixChars ?? PREFIX_CHARS,
          flushIntervalMs: options.optimizeFlushIntervalMs ?? OPTIMIZE_FLUSH_INTERVAL_MS,
          windowMs: options.optimizeWindowMs ?? DUPLICATE_WINDOW_MS,
          cacheWindowMs: options.cacheWindowMs ?? CACHE_WINDOW_MS,
        })
      : null;
  }

  get optimizeEnabled() {
    return this._optimizer !== null;
  }

  _saltUrl() {
    if (!this.ingestUrl) return null;
    const base = this.ingestUrl.replace(/\/+$/, "");
    return base.endsWith("/events") ? `${base.slice(0, -"/events".length)}/salt` : `${base}/salt`;
  }

  /**
   * The tenant's fingerprint salt, or null until it arrives.
   *
   * Fetched once, and never awaited on the caller's path — returning null for
   * the first few calls costs a handful of aggregate signals and costs the
   * caller nothing. A failed fetch disables signals for good rather than
   * retrying: without a salt the only alternative is an unsalted hash, and that
   * must never be emitted.
   */
  salt() {
    if (this._saltState === "ready") return this._salt;
    if (this._saltState === "cold") {
      this._saltState = "fetching";
      this._fetchSalt().catch(() => {
        this._saltState = "failed";
      });
    }
    return null;
  }

  async _fetchSalt() {
    if (!this.enabled) {
      this._saltState = "failed";
      return;
    }
    try {
      const resp = await this._fetch(this._saltUrl(), {
        method: "GET",
        headers: { Authorization: `Bearer ${this.token}` },
      });
      const value = resp?.ok ? (await resp.json())?.salt : null;
      if (!value) throw new Error("no salt");
      this._salt = value;
      this._saltState = "ready";
    } catch {
      // Fail safe, and quietly: a customer's application must never learn that
      // Meter could not reach Meter.
      this._saltState = "failed";
    }
  }

  get enabled() {
    return Boolean(this.ingestUrl && this.token && this._fetch);
  }

  /** Only the developer's half: whether a sample is actually sent also needs the
   *  organization's consent for the feature, which the server decides. */
  get captureEnabled() {
    return Boolean(this.enabled && this.capturePrompts && this.captureUrl);
  }

  /**
   * Instrument a provider client so each call becomes its own trace.
   *
   * `provider` is inferred from the client's constructor, which works for the
   * official SDKs and fails quietly for a wrapper or a test double — so it can
   * be stated outright.
   */
  /**
   * Instrument a provider client so each call becomes its own trace.
   *
   * `customerId` / `cacheScope` exist for optimize mode and nothing else. Two
   * identical requests are only interchangeable inside whatever boundary the
   * application would actually reuse a response across; without one stated,
   * Meter records the repeat but will not call it safely reusable. `agent()`
   * has always taken a customer; this is the wrapped-client equivalent.
   * Neither ever reaches the provider's API.
   */
  wrap(client, options = {}) {
    const provider = options.provider ?? detectProvider(client);
    return this._proxy(client, provider, [], {
      featureId: options.featureId ?? this.featureId,
      application: options.application ?? this.application,
      promptId: options.promptId ?? null,
      promptVersion: options.promptVersion ?? null,
      customerId: options.customerId ?? null,
      cacheScope: options.cacheScope ?? null,
    });
  }

  _proxy(target, provider, path, ctx) {
    const paths = COMPLETION_PATHS[provider] ?? [];
    const meter = this;
    return new Proxy(target, {
      get(obj, prop) {
        const value = Reflect.get(obj, prop);
        if (typeof prop !== "string") return value;
        const next = [...path, prop];
        if (pathMatches(paths, next) && typeof value === "function") {
          return (...args) => meter._instrumented(value.bind(obj), next, provider, ctx, args);
        }
        if (pathIsPrefix(paths, next) && value && typeof value === "object") {
          return meter._proxy(value, provider, next, ctx);
        }
        return typeof value === "function" ? value.bind(obj) : value;
      },
    });
  }

  async _instrumented(fn, path, provider, ctx, args) {
    const traceId = newId();
    const spanId = newId();
    const operation = path.join(".") || "completion";
    const began = Date.now();
    try {
      this._send([
        this._event("trace.started", traceId, {
          operation_name: operation,
          feature_id: ctx.featureId,
          application: ctx.application,
        }),
        this._event("span.started", traceId, {
          span_id: spanId,
          span_kind: "llm",
          operation_name: operation,
          feature_id: ctx.featureId,
          application: ctx.application,
        }),
      ]);
    } catch {
      /* metering must never raise into the caller */
    }
    let resp;
    try {
      resp = await fn(...args);
    } catch (err) {
      try {
        this._send([
          this._event("span.failed", traceId, {
            span_id: spanId,
            span_kind: "llm",
            operation_name: operation,
            latency_ms: Date.now() - began,
            feature_id: ctx.featureId,
            application: ctx.application,
            prompt_id: ctx.promptId,
            prompt_version: ctx.promptVersion,
          }),
          this._event("trace.failed", traceId, {
            operation_name: operation,
            application: ctx.application,
          }),
        ]);
      } catch {
        /* ignore */
      }
      throw err;
    }
    let usage = {};
    const latencyMs = Date.now() - began;
    try {
      usage = usageOf(resp);
      if (!usage.provider) usage.provider = provider;
      // Optimize mode, when it is on: fold this call's shape into the collector
      // and attach the duplicate signal, if this request repeats, to the span
      // the call was already sending. A hash and a serialize against an LLM
      // call's latency; the network stays where it was.
      let signal = null;
      let summaries = [];
      if (this._optimizer) {
        signal = this._optimizer.onCall(
          usage.provider,
          // The response's model when it reported one, the request's when it
          // did not. v1 used the response alone and fell back to "", so a
          // streaming call made two DIFFERENT models share one identity.
          usage.model ?? requestModel(args[0]),
          args[0],
          usage,
          ctx.featureId ?? null,
          {
            application: ctx.application ?? this.application,
            feature_id: ctx.featureId ?? null,
            operation,
            environment: this.environment,
            customer_id: ctx.customerId ?? null,
            cache_scope: ctx.cacheScope ?? null,
          },
        );
        summaries = this._optimizer.dueSummaries();
      }
      this._send([
        this._event("span.completed", traceId, {
          span_id: spanId,
          span_kind: "llm",
          operation_name: operation,
          latency_ms: latencyMs,
          feature_id: ctx.featureId,
          application: ctx.application,
          prompt_id: ctx.promptId,
          prompt_version: ctx.promptVersion,
          signal,
          ...usage,
        }),
        this._event("trace.completed", traceId, {
          operation_name: operation,
          application: ctx.application,
        }),
        ...summaries,
      ]);
    } catch {
      /* ignore */
    }
    try {
      // After metering, and separately: a capture problem can cost a sample,
      // never an event, and never the caller's response.
      this._offerSample(provider, path, args, resp, ctx, usage, latencyMs);
    } catch {
      /* ignore */
    }
    return resp;
  }

  // -- consented prompt capture --------------------------------------------
  /**
   * Consider one completed call for a prompt sample. Cheap; never throws.
   *
   * Every reason to say no is checked before anything is kept: the developer's
   * switch, a named prompt, a call shape this SDK can read, the server's word
   * that capture is open for this feature, the sample rate and the daily cap.
   * Reading the request happens on a later tick, off the caller's path.
   */
  _offerSample(provider, path, args, resp, ctx, usage, latencyMs) {
    if (!this.captureEnabled || !(ctx.featureId && ctx.promptId && ctx.promptVersion)) return;
    const route = path.join(".");
    if (!CAPTURE_PATHS.some(([p, r]) => p === provider && r === route)) return;
    if (!usage.tokens_in && !usage.tokens_out) return; // a stream: nothing complete
    const featureId = ctx.featureId;
    const known = this._captureOpen.get(featureId);
    if (!known || known.until <= Date.now()) {
      // Ask first. This call is not sampled: until the server says yes, not a
      // single prompt is kept, even briefly.
      if (!this._captureChecking.has(featureId)) this._track(this._checkCapture(featureId));
      return;
    }
    if (!known.open) return;
    const day = new Date().toISOString().slice(0, 10);
    const key = `${featureId}|${ctx.promptId}`;
    if (this._captureCapped.get(key) === day) return;
    if (Math.random() >= this.captureSampleRate) return;
    const counted = this._captureDaily.get(key);
    const count = counted && counted.day === day ? counted.count : 0;
    if (count >= CAPTURE_MAX_PER_DAY) return;
    this._captureDaily.set(key, { day, count: count + 1 });
    if (this._captureInflight.size >= CAPTURE_INFLIGHT_MAX) {
      this.captureDropped += 1;
      return;
    }
    // A copy of the message list, not of the messages: an application that
    // appends to its conversation after the call must not change what this
    // call is recorded as having sent.
    const request = args[0] && typeof args[0] === "object" ? { ...args[0] } : {};
    if (Array.isArray(request.messages)) request.messages = [...request.messages];
    const item = {
      provider,
      request,
      resp,
      featureId,
      promptId: ctx.promptId,
      promptVersion: ctx.promptVersion,
      usage: { ...usage },
      latencyMs,
      capturedAt: nowIso(),
    };
    this._track(
      new Promise((resolve) => setImmediate(resolve)).then(() => this._deliverSample(item)),
    );
  }

  _track(promise) {
    const tracked = Promise.resolve(promise)
      .catch(() => {})
      .finally(() => this._captureInflight.delete(tracked));
    this._captureInflight.add(tracked);
    return tracked;
  }

  async _checkCapture(featureId) {
    this._captureChecking.add(featureId);
    let open = false;
    try {
      const base = this.captureUrl.replace(/\/[^/]*$/, "");
      const resp = await this._fetch(
        `${base}/open?feature_id=${encodeURIComponent(featureId)}`,
        { method: "GET", headers: { Authorization: `Bearer ${this.token}` } },
      );
      if (resp?.ok) open = Boolean((await resp.json())?.open);
    } catch {
      open = false;
    } finally {
      this._captureOpen.set(featureId, { open, until: Date.now() + CAPTURE_OPEN_TTL_MS });
      this._captureChecking.delete(featureId);
    }
  }

  /** Read, redact, size-check and post one sample. One attempt. Never throws. */
  async _deliverSample(item) {
    let sample;
    try {
      sample = readSample(item.provider, item.request, item.resp);
    } catch {
      sample = null;
    }
    if (!sample) return; // not text this SDK can read: nothing to send, nothing lost
    const model = item.usage.model ?? item.request.model;
    if (typeof model !== "string" || !model) return;
    Object.assign(sample, {
      feature_id: item.featureId,
      prompt_id: item.promptId,
      prompt_version: item.promptVersion,
      provider: item.usage.provider ?? item.provider,
      model,
      tokens_in: item.usage.tokens_in,
      tokens_out: item.usage.tokens_out,
      latency_ms: item.latencyMs,
      captured_at: item.capturedAt,
    });
    for (const [key, value] of Object.entries(sample)) {
      if (value === undefined || value === null) delete sample[key];
    }
    if (this._redact) {
      try {
        sample = await this._redact(sample);
      } catch {
        sample = null; // a failing redactor fails closed: send nothing
      }
      if (!sample || typeof sample !== "object" || Array.isArray(sample)) {
        this.captureDropped += 1;
        return;
      }
    }
    let body;
    try {
      body = JSON.stringify(sample);
    } catch {
      this.captureDropped += 1;
      return;
    }
    if (Buffer.byteLength(body, "utf8") > CAPTURE_MAX_BYTES) {
      this.captureDropped += 1;
      return;
    }
    let status = 0;
    let reason = null;
    try {
      const resp = await this._fetch(this.captureUrl, {
        method: "POST",
        headers: { Authorization: `Bearer ${this.token}`, "Content-Type": "application/json" },
        body,
      });
      if (resp?.ok) return;
      status = resp?.status ?? 0;
      try {
        reason = (await resp.json())?.reason ?? null;
      } catch {
        reason = null;
      }
    } catch {
      status = 0;
    }
    if (status === 403) {
      // Consent is not there for this feature, whatever the cache said.
      this._captureOpen.set(item.featureId, {
        open: false,
        until: Date.now() + CAPTURE_OPEN_TTL_MS,
      });
    } else if (status === 429 || reason === "daily_cap") {
      this._captureCapped.set(
        `${item.featureId}|${item.promptId}`,
        new Date().toISOString().slice(0, 10),
      );
    }
    this.captureDropped += 1;
  }

  /**
   * A multi-step run. The callback receives the run; leaving it ends the trace
   * and stops its heartbeat, on the happy path and on a throw alike, so a
   * crashed agent is recorded as failed rather than left looking hung forever.
   */
  async agent(operationName, options, callback) {
    if (typeof options === "function") {
      callback = options;
      options = {};
    }
    const run = new AgentRun(this, {
      operationName,
      featureId: options.featureId ?? this.featureId,
      customerId: options.customerId ?? null,
      application: options.application ?? this.application,
      traceId: options.traceId,
      parentSpanId: options.parentSpanId,
    });
    run._start();
    try {
      const result = await callback(run);
      run._finish("trace.completed");
      return result;
    } catch (err) {
      // The status, never the error. What went wrong is in the customer's own
      // logs; it is not ours to copy off their machine.
      run._finish("trace.failed");
      throw err;
    }
  }

  /** Continue a trace started elsewhere — a queue worker, another service. */
  async resume(context, callback) {
    const ctx = typeof context === "string" ? JSON.parse(context || "{}") : (context ?? {});
    return this.agent(
      ctx.operation_name ?? "resumed",
      {
        featureId: ctx.feature_id,
        application: ctx.application,
        traceId: ctx.trace_id,
        parentSpanId: ctx.parent_span_id,
      },
      callback,
    );
  }

  _event(eventType, traceId, fields = {}) {
    const event = {
      event_type: eventType,
      event_id: newId(),
      trace_id: traceId,
      application: fields.application ?? this.application,
      environment: this.environment,
      occurred_at: nowIso(),
    };
    if (this.releaseVersion) event.release_version = this.releaseVersion;
    for (const [key, value] of Object.entries(fields)) {
      if (value !== null && value !== undefined && key !== "application") event[key] = value;
    }
    return event;
  }

  _emit(event) {
    this._send([event]);
  }

  _send(events) {
    if (!this.enabled || !events?.length) return;
    try {
      for (const event of events) {
        if (this._queue.length >= this._queueMax) {
          // Shed the oldest so the newest always gets in, and a stalled
          // endpoint can never grow this unboundedly.
          this._queue.shift();
          this.dropped += 1;
        }
        this._queue.push(event);
      }
      if (this._queue.length >= this._batchSize) {
        this._drain();
      } else if (!this._timer) {
        this._timer = setTimeout(() => this._drain(), this._flushIntervalMs);
        this._timer.unref?.();
      }
    } catch {
      /* metering must never raise into the caller's path */
    }
  }

  _drain() {
    if (this._timer) {
      clearTimeout(this._timer);
      this._timer = null;
    }
    if (!this._queue.length) return this._inflight ?? Promise.resolve();
    const batch = this._queue.splice(0, this._batchSize);
    const previous = this._inflight ?? Promise.resolve();
    // Chained, not concurrent: batches leave in the order they were made, and
    // one slow request cannot fan out into many.
    this._inflight = previous.then(() => this._deliver(batch)).catch(() => {});
    return this._inflight;
  }

  /**
   * Deliver one batch, retrying transient failures. Never throws.
   *
   * The batch id is generated ONCE and reused for every attempt. That is what
   * makes retrying safe: the server applies the first delivery and recognises
   * the rest as replays, so a retry after an ambiguous timeout — server
   * committed, response lost — cannot double a run's cost.
   */
  async _deliver(events) {
    const batchId = newId();
    const body = JSON.stringify({ events, batch_id: batchId });
    for (let attempt = 0; attempt < this._maxAttempts; attempt += 1) {
      let outcome = "retry";
      try {
        const resp = await this._fetch(this.ingestUrl, {
          method: "POST",
          headers: {
            Authorization: `Bearer ${this.token}`,
            "Content-Type": "application/json",
          },
          body,
        });
        if (resp?.ok) outcome = "ok";
        // 4xx is our fault and fails identically forever: a bad token, a
        // malformed event. Retrying only hammers the endpoint. 429 is the
        // exception — an explicit invitation to come back.
        else if (resp?.status >= 400 && resp?.status < 500 && resp?.status !== 429) {
          outcome = "drop";
        }
      } catch {
        outcome = "retry"; // timeout, DNS, refused, TLS: it may yet return
      }
      if (outcome === "ok") return;
      if (outcome === "drop") {
        this.dropped += events.length;
        return;
      }
      if (attempt + 1 >= this._maxAttempts) break;
      const base = this._retryBackoffMs[Math.min(attempt, this._retryBackoffMs.length - 1)];
      // Jitter, so many processes recovering from one outage do not return in
      // lockstep.
      await new Promise((resolve) => {
        const t = setTimeout(resolve, base * (0.5 + Math.random()));
        t.unref?.();
      });
    }
    this.dropped += events.length;
  }

  /** Send what is queued now. Worth awaiting before a short-lived process exits. */
  async flush() {
    if (!this.enabled) return true;
    // Counters that have been accumulating but have not reached their interval
    // belong in this flush: a process that does not outlive the interval — a
    // script, a batch job, one serverless invocation — would otherwise take
    // every prefix counter it gathered to the grave.
    if (this._optimizer) {
      try {
        this._send(this._optimizer.dueSummaries(true));
      } catch {
        /* a lost summary is never worth failing a flush over */
      }
    }
    try {
      while (this._queue.length) await this._drain();
      await (this._inflight ?? Promise.resolve());
      // Capture last: a pending check can start a sample, so settle until quiet.
      while (this._captureInflight.size) await Promise.all([...this._captureInflight]);
      return true;
    } catch {
      return false;
    }
  }
}

// ---------------------------------------------------------------------------
// Consented prompt capture: reading a call's text, and nothing else
// ---------------------------------------------------------------------------
/** The capture endpoint beside a standard ingest URL, or null. */
// ---------------------------------------------------------------------------
// Optimize mode — measured optimization signals
// ---------------------------------------------------------------------------
// Traffic SHAPE only: salted-hash fingerprints and counts. No prompt text, no
// response text, no tool arguments — nothing here reads a request's content
// except to hash it, salted with a secret the database does not contain.
// ---------------------------------------------------------------------------
// Canonical request identity (v2)
// ---------------------------------------------------------------------------
// The Node half of the rule documented in the Python SDK. The two must produce
// byte-identical output for the same request, so a company running both can be
// told the truth about traffic that spans them.
//
// v1 hashed provider, model and messages only, so a call differing solely in
// temperature, system, tools, max_tokens, response_format, seed or stop looked
// identical and was reported as an avoidable repeat. All seven change output.
//
// The rule is an EXCLUSION, not an allowlist: every serializable request field
// is part of the identity except transport and credential settings. A parameter
// nobody here has heard of changes the fingerprint rather than being ignored.
//
// Anything unreadable — a cycle, a class instance, a payload past the bounds —
// throws, and the caller emits no signal for that call. No truncation, no
// String() fallback: both turn "unreadable" into "identical".
//
// Nothing here is transmitted. The canonical form is hashed and discarded.

/** Bumped when the canonical form changes; hashed in, so versions cannot mix. */
export const CANON_VERSION = "v2";

/** Reaches the transport, never the model. Credentials included deliberately. */
const TRANSPORT_FIELDS = new Set([
  "api_key", "apiKey", "auth", "authorization", "headers", "extra_headers",
  "extraHeaders", "extra_query", "extraQuery", "timeout", "request_timeout",
  "requestTimeout", "max_retries", "maxRetries", "http_client", "httpClient",
  "client", "default_headers", "defaultHeaders", "organization", "project_id",
  "projectId", "base_url", "baseURL", "baseUrl", "user_agent", "userAgent",
  "signal", "fetch", "dispatcher",
]);

const MAX_DEPTH = 40;
const MAX_NODES = 20_000;
const MAX_CANON_BYTES = 1_000_000;

/** This request cannot be read safely, so it is not a duplicate candidate. */
export class UnsupportedRequest extends Error {}

function canonicalValue(value, depth, state) {
  state.nodes += 1;
  if (depth > MAX_DEPTH || state.nodes > MAX_NODES) {
    throw new UnsupportedRequest("request is too deeply nested or too large to compare");
  }

  if (value === null) return null;
  const t = typeof value;
  if (t === "string" || t === "boolean") return value;
  if (t === "number") {
    if (!Number.isFinite(value)) throw new UnsupportedRequest("request contains a non-finite number");
    return value; // integral floats are already integers here; see the Python note
  }
  // `undefined` is a field that was not set. Absent and null are different
  // arguments to a provider, so they stay different here: undefined is dropped
  // by the object walk below, null is kept.
  if (t === "undefined") return undefined;
  if (t === "bigint" || t === "function" || t === "symbol") {
    throw new UnsupportedRequest(`request contains an unreadable ${t}`);
  }

  if (Array.isArray(value)) {
    if (state.seen.has(value)) throw new UnsupportedRequest("request contains a cycle");
    state.seen.add(value);
    try {
      // A hole or an explicit undefined inside an array becomes null, which is
      // what JSON does with it; there is no way to express "absent" in a list.
      return value.map((item) => {
        const out = canonicalValue(item, depth + 1, state);
        return out === undefined ? null : out;
      });
    } finally {
      state.seen.delete(value);
    }
  }

  if (t === "object") {
    if (state.seen.has(value)) throw new UnsupportedRequest("request contains a cycle");
    // Provider SDKs hand back model objects. toJSON is the documented, lossless
    // conversion; a bare class instance is unreadable rather than guessed at.
    if (typeof value.toJSON === "function") {
      state.seen.add(value);
      try {
        return canonicalValue(value.toJSON(), depth + 1, state);
      } catch (err) {
        if (err instanceof UnsupportedRequest) throw err;
        throw new UnsupportedRequest("request contains an object that cannot be read");
      } finally {
        state.seen.delete(value);
      }
    }
    const proto = Object.getPrototypeOf(value);
    if (proto !== Object.prototype && proto !== null) {
      throw new UnsupportedRequest(`request contains an unreadable ${value.constructor?.name ?? "object"}`);
    }
    state.seen.add(value);
    try {
      const out = {};
      for (const key of Object.keys(value)) {
        const item = canonicalValue(value[key], depth + 1, state);
        if (item !== undefined) out[key] = item; // absent stays absent
      }
      return out;
    } finally {
      state.seen.delete(value);
    }
  }

  throw new UnsupportedRequest(`request contains an unreadable ${t}`);
}

/** Serialize with sorted keys and no whitespace — Python's json.dumps with
 *  sort_keys, ensure_ascii=False and (",", ":") produces the same bytes. */
function canonicalJson(value) {
  if (value === null) return "null";
  const t = typeof value;
  if (t === "number") {
    // Python writes an integral float as an int because JavaScript cannot tell
    // 1.0 from 1; both sides therefore emit "1".
    return Number.isInteger(value) ? String(value) : JSON.stringify(value);
  }
  if (t === "boolean") return value ? "true" : "false";
  if (t === "string") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  const keys = Object.keys(value).sort();
  return `{${keys.map((k) => `${JSON.stringify(k)}:${canonicalJson(value[k])}`).join(",")}}`;
}

/** The request's identity as a string, or throw UnsupportedRequest. */
/** The model the caller asked for, when the response did not say. */
function requestModel(request) {
  return request && typeof request.model === "string" ? request.model : "";
}

/**
 * The boundary inside which two identical requests could be interchangeable.
 *
 * Not a label on the finding — part of its identity. Two customers sending the
 * same prompt are not one avoidable call, and neither are two environments or
 * two features that happen to share wording. Hashed with the tenant's salt, so
 * no raw application, feature or customer name leaves the process.
 */
const SCOPE_FIELDS = [
  "application", "feature_id", "operation", "environment", "customer_id", "cache_scope",
];

function scopeKey(scope) {
  return SCOPE_FIELDS.map((f) => `${f}=${scope?.[f] ?? ""}`).join("\u001e");
}

/**
 * Whether the caller stated a boundary a response could be reused in.
 *
 * Without one a repeat is still a repeat, but nobody has said the two calls
 * belong to the same user, tenant or cache — so Meter must not imply the second
 * could have served the first. Absent scope is reported as absent, never as
 * permission.
 */
function scopeIsExplicit(scope) {
  return Boolean(scope?.customer_id || scope?.cache_scope);
}

export function canonicalRequest(request) {
  if (!request || typeof request !== "object" || Array.isArray(request)) {
    throw new UnsupportedRequest("request is not an object");
  }
  const body = {};
  for (const key of Object.keys(request)) {
    if (!TRANSPORT_FIELDS.has(key)) body[key] = request[key];
  }
  if (Object.keys(body).length === 0) {
    throw new UnsupportedRequest("request has no comparable fields");
  }
  const canonical = canonicalValue(body, 0, { nodes: 0, seen: new Set() });
  const text = canonicalJson(canonical);
  if (Buffer.byteLength(text, "utf8") > MAX_CANON_BYTES) {
    throw new UnsupportedRequest("request is too large to compare");
  }
  return text;
}

function sha(...parts) {
  return createHash("sha256").update(parts.join("\u001f"), "utf8").digest("hex");
}

/** A stable string for the parts of a request that make two calls identical. */
function normalizeRequest(request) {
  if (!request || typeof request !== "object") return "";
  const payload = request.messages ?? request.input ?? request.contents;
  // Not a call shape this knows how to compare. "" rather than "null", which is
  // what a serializer gives and which every unreadable call would then share.
  if (payload === undefined || payload === null) return "";
  try {
    return stableJson(payload);
  } catch {
    return String(payload);
  }
}

/** JSON with object keys in a fixed order, so two equal requests hash equal. */
function stableJson(value) {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    const keys = Object.keys(value).sort();
    return `{${keys.map((k) => `${JSON.stringify(k)}:${stableJson(value[k])}`).join(",")}}`;
  }
  return JSON.stringify(value ?? null);
}

/**
 * The cacheable static head of a request, and an estimate of its size.
 *
 * Prefers the explicitly static blocks — the system instruction and the tool
 * definitions — and otherwise falls back to the leading slice of the request.
 * The token count is characters over four, which is an estimate and is reported
 * as one: the provider's own figure replaces it wherever the provider gives one.
 *
 * The system instruction used to be read only from `request.system`, which is
 * Anthropic's spelling. On OpenAI that found nothing, and when `tools` was
 * present the first branch still fired — so the prefix became the tool
 * definitions ALONE, with the system prompt (usually the larger block) left out
 * of both the estimated size and the fingerprint. Every OpenAI call sharing a
 * toolset hashed alike however different its instructions were.
 */
function systemOf(request) {
  for (const key of ["system", "system_instruction", "systemInstruction"]) {
    const value = request[key];
    if (value !== undefined && value !== null) return value;
  }
  const messages = request.messages;
  if (!Array.isArray(messages)) return null;
  const leading = [];
  for (const message of messages) {
    // The LEADING run only. A system turn further down is not part of a
    // prefix, and a cache is a prefix.
    if (!message || typeof message !== "object") break;
    if (message.role !== "system" && message.role !== "developer") break;
    leading.push(message);
  }
  return leading.length > 0 ? leading : null;
}

function staticPrefix(request, prefixChars) {
  let staticPart = "";
  if (request && typeof request === "object") {
    const system = systemOf(request);
    const { tools } = request;
    if (system !== null || tools !== undefined) {
      try {
        staticPart = stableJson([system ?? null, tools ?? null]);
      } catch {
        staticPart = `${system}${tools}`;
      }
    }
  }
  if (!staticPart) staticPart = normalizeRequest(request).slice(0, prefixChars);
  return [staticPart, Math.max(0, Math.floor(staticPart.length / 4))];
}

/**
 * Bounded signal collection for one meter.
 *
 * Both structures have a hard ceiling, because a map keyed by request shape
 * grows with traffic: the duplicate set is an LRU (a Map, whose iteration order
 * is insertion order) and the prefix map flushes at capacity as well as on its
 * timer.
 */
class Optimizer {
  constructor(meter, options = {}) {
    this._m = meter;
    this._prefixChars = options.prefixChars ?? PREFIX_CHARS;
    this._flushIntervalMs = options.flushIntervalMs ?? OPTIMIZE_FLUSH_INTERVAL_MS;
    this._dupCapacity = options.dupCapacity ?? DUP_CAPACITY;
    this._prefixCapacity = options.prefixCapacity ?? PREFIX_CAPACITY;
    this._windowMs = options.windowMs ?? DUPLICATE_WINDOW_MS;
    this._cacheWindowMs = options.cacheWindowMs ?? CACHE_WINDOW_MS;
    this._seen = new Map();
    this._prefixes = new Map();
    // Last time each prefix was seen, kept OUTSIDE this._prefixes because it
    // must survive the flush that empties it. A window count restarted every
    // flush interval would report one write per minute of traffic and call
    // caching a loss.
    this._prefixLast = new Map();
    this._lastFlush = Date.now();
  }

  /**
   * Fold one call in; return a 'duplicate' signal if this request repeats.
   *
   * Two fields of `usage` matter: a cache READ says this call was already
   * served from cache and must not be counted as a caching opportunity, and a
   * cache WRITE is the provider's own measurement of the static prefix — the
   * only place a real token count for it can come from.
   */
  onCall(provider, model, request, usage = {}, featureId = null, scope = null) {
    const salt = this._m.salt();
    if (!salt) return null; // never emit an unsalted hash
    const modelName = model ?? "";
    // The whole request, not just its messages, and scoped to the boundary a
    // response could actually be reused in. Unreadable requests produce no
    // candidate rather than degrading: a truncated or stringified payload
    // compares equal to things it is not.
    let requestFp = null;
    try {
      requestFp = sha(
        salt, CANON_VERSION, scopeKey(scope), provider, modelName, canonicalRequest(request),
      );
    } catch (err) {
      if (!(err instanceof UnsupportedRequest)) throw err;
    }
    // Prefix detection keeps its own, unchanged normalisation: "is this the
    // same call" and "do these calls share a head" are different questions.
    const [staticPart, estimated] = staticPrefix(request, this._prefixChars);
    const prefixFp = sha(salt, provider, modelName, staticPart);
    // A prefix belongs to the feature whose call it came from, not to the
    // meter's default: `wrap({ featureId })` may name a different one.
    const feature = featureId ?? this._m.featureId ?? null;
    const key = `${prefixFp}\u001f${feature ?? ""}`;
    const measured = int(usage.cache_write_tokens);
    const cacheRead = int(usage.cache_read_tokens);

    // performance.now(), not Date.now(): a monotonic clock cannot move backwards
    // under an NTP correction and turn an expiry into a negative age.
    const now = performance.now();
    let duplicate = false;
    if (requestFp !== null) {
      const opened = this._seen.get(requestFp);
      if (opened !== undefined && now - opened <= this._windowMs) {
        duplicate = true;
        // Recency for eviction only. The group's start time is NOT refreshed,
        // so steady repeating traffic cannot keep one supposed cached response
        // alive indefinitely.
        this._seen.delete(requestFp);
        this._seen.set(requestFp, opened);
      } else {
        // First sighting, or the window has closed and this opens a new group —
        // an expired repeat is not an avoidable call.
        this._seen.delete(requestFp);
        this._seen.set(requestFp, now);
      }
      while (this._seen.size > this._dupCapacity) {
        this._seen.delete(this._seen.keys().next().value);
      }
    }

    let entry = this._prefixes.get(key);
    if (!entry) {
      entry = {
        provider,
        model: modelName,
        fingerprint: prefixFp,
        featureId: feature,
        count: 0,
        cached: 0,
        writes: 0,
        windows: 0,
        tin: 0,
        tout: 0,
        estimated: 0,
        measuredSum: 0,
        measuredN: 0,
      };
      this._prefixes.set(key, entry);
    }
    entry.count += 1;
    entry.tin += int(usage.tokens_in);
    entry.tout += int(usage.tokens_out);
    // A no-op fold, kept explicit: the fingerprint IS the static block, so
    // every call in this group has the same character estimate.
    entry.estimated = Math.max(entry.estimated, estimated);
    if (measured) {
      // Summed and counted, not maximised. What the provider actually caches
      // varies between calls sharing a static block — the breakpoint moves,
      // and the conversation in front of it grows — so the largest figure of
      // the month is the group's most expensive member, not its typical one. A
      // mean also survives being folded again server-side; a max of means does
      // not.
      entry.measuredSum += measured;
      entry.measuredN += 1;
    }
    if (cacheRead) entry.cached += 1;
    // The provider says it WROTE this prefix, so caching is already on here and
    // this call is the unavoidable cost of keeping it warm — not an opportunity
    // to enable something.
    if (measured) entry.writes += 1;
    // How many times a cache would have had to be written if one were in use:
    // once per gap longer than the provider's TTL. Counted per process, and a
    // provider's cache is account-wide, so replicas each count a window the
    // account only paid for once — an OVERCOUNT of writes, which understates
    // the saving rather than inflating it.
    const lastSeen = this._prefixLast.get(prefixFp);
    // >=, not >: at exactly the TTL the entry is on the boundary, and assuming
    // it survived would be assuming a saving. Assume the write.
    if (lastSeen === undefined || now - lastSeen >= this._cacheWindowMs) {
      entry.windows += 1;
    }
    this._prefixLast.delete(prefixFp);
    this._prefixLast.set(prefixFp, now);
    while (this._prefixLast.size > this._prefixCapacity) {
      this._prefixLast.delete(this._prefixLast.keys().next().value);
    }

    return duplicate
      ? {
          kind: "duplicate",
          fingerprint: requestFp,
          count: 1,
          fingerprint_version: CANON_VERSION,
          // Whether anybody said these two calls belong to the same user,
          // tenant or cache. The server will not present an unscoped repeat as
          // a safe reuse.
          scope_kind: scopeIsExplicit(scope) ? "explicit" : "unscoped",
        }
      : null;
  }

  /**
   * Prefix counters as spans, when the timer elapses or the map fills up.
   *
   * Each summary is a completed span of its own. The server does not cost it —
   * the calls it summarises were each metered when they happened — it only
   * folds the counts into the tenant's signal store.
   */
  dueSummaries(force = false) {
    const now = Date.now();
    const due =
      force ||
      now - this._lastFlush >= this._flushIntervalMs ||
      this._prefixes.size >= this._prefixCapacity;
    if (!due) return [];
    this._lastFlush = now;
    const items = this._prefixes;
    this._prefixes = new Map();

    const events = [];
    for (const entry of items.values()) {
      // The estimate and the measurements travel in SEPARATE fields. They used
      // to share one, folded server-side with GREATEST, which cannot tell them
      // apart: a process that never saw a cache creation sent its character
      // count, and if that was the larger number it won and was then labelled
      // as the provider's own.
      const measured = entry.measuredN > 0;
      events.push(
        this._m._event("span.completed", newId(), {
          span_id: newId(),
          span_kind: "llm",
          operation_name: "optimize.prefix",
          provider: entry.provider,
          model: entry.model || null,
          feature_id: entry.featureId,
          signal: {
            kind: "prefix",
            fingerprint: entry.fingerprint,
            count: entry.count,
            cached_count: entry.cached,
            tokens_in: entry.tin,
            tokens_out: entry.tout,
            // Always the estimate now, whatever else was seen.
            prefix_tokens: entry.estimated,
            prefix_measured: measured,
            // The provider's own counts, summed and counted so the server can
            // hold a mean across every process reporting this prefix.
            prefix_tokens_sum: entry.measuredSum,
            prefix_tokens_n: entry.measuredN,
            // What caching costs, not just what it saves: calls the provider
            // already wrote to cache, and the number of writes enabling it
            // would need.
            write_calls: entry.writes,
            cache_windows: entry.windows,
          },
        }),
      );
    }
    return events;
  }
}

function captureUrlFrom(ingestUrl) {
  if (!ingestUrl) return null;
  const base = String(ingestUrl).replace(/\/+$/, "");
  return base.endsWith("/hook/events")
    ? `${base.slice(0, -"/hook/events".length)}/prompt-capture/samples`
    : null;
}

/**
 * The text in a message's content. Only blocks whose type is text are read.
 *
 * That is the whole privacy boundary for tool data: a tool_use, tool_result,
 * image or document block is never looked into, because nothing here asks for
 * anything but a text block's `text`.
 */
function textOf(content) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .filter((block) => block && ["text", "input_text", "output_text"].includes(block.type))
    .map((block) => block.text)
    .filter((text) => typeof text === "string" && text)
    .join("\n\n");
}

const isNumber = (value) => typeof value === "number" && Number.isFinite(value);

/**
 * {template, input, output, parameters} for one call, or null.
 *
 * The template is the instruction text: Anthropic's `system`, or OpenAI's system
 * and developer messages. Tool-role messages are skipped outright. A call with no
 * instruction text, or no message text, is not a prompt this can optimize.
 */
function readSample(provider, request, resp) {
  const parameters = {};
  for (const name of ["temperature", "top_p", "max_tokens"]) {
    if (isNumber(request[name])) parameters[name] = request[name];
  }
  const input = [];
  let template = "";
  let output = "";
  if (provider === "anthropic") {
    template = textOf(request.system);
    for (const message of request.messages ?? []) {
      if (message?.role !== "user" && message?.role !== "assistant") continue;
      const text = textOf(message.content);
      if (text) input.push({ role: message.role, text });
    }
    output = textOf(resp?.content);
  } else if (provider === "openai") {
    if (!("max_tokens" in parameters) && isNumber(request.max_completion_tokens)) {
      parameters.max_tokens = request.max_completion_tokens;
    }
    if (typeof request.response_format?.type === "string") {
      parameters.response_format = request.response_format.type;
    }
    const instructions = [];
    for (const message of request.messages ?? []) {
      const role = message?.role;
      // Tool results and function results are never read.
      if (!["system", "developer", "user", "assistant"].includes(role)) continue;
      const text = textOf(message.content);
      if (!text) continue;
      if (role === "system" || role === "developer") instructions.push(text);
      else input.push({ role, text });
    }
    template = instructions.join("\n\n");
    output = textOf(resp?.choices?.[0]?.message?.content);
  } else {
    return null;
  }
  if (!template || !input.length) return null;
  return { template, input, output, parameters };
}

export default Meter;
