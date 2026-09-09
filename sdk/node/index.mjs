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
 * **What is never sent.** Prompts, responses, messages, tool arguments, tool
 * results, retrieved documents, exception messages and stack traces. The events
 * this SDK can construct have no field for them, and the server rejects a
 * payload that carries one. What travels is identity, counts, timing and money.
 *
 * **It must never break your agent.** Every path is guarded: a Meter failure
 * degrades to sending nothing. Delivery is batched off your call path over a
 * bounded queue that sheds oldest-first, and `meter.dropped` says if it did.
 */

import { randomUUID } from "node:crypto";

export const VERSION = "1.0.0";

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
  }

  get enabled() {
    return Boolean(this.ingestUrl && this.token && this._fetch);
  }

  /**
   * Instrument a provider client so each call becomes its own trace.
   *
   * `provider` is inferred from the client's constructor, which works for the
   * official SDKs and fails quietly for a wrapper or a test double — so it can
   * be stated outright.
   */
  wrap(client, options = {}) {
    const provider = options.provider ?? detectProvider(client);
    return this._proxy(client, provider, [], {
      featureId: options.featureId ?? this.featureId,
      application: options.application ?? this.application,
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
    try {
      const usage = usageOf(resp);
      if (!usage.provider) usage.provider = provider;
      this._send([
        this._event("span.completed", traceId, {
          span_id: spanId,
          span_kind: "llm",
          operation_name: operation,
          latency_ms: Date.now() - began,
          feature_id: ctx.featureId,
          application: ctx.application,
          ...usage,
        }),
        this._event("trace.completed", traceId, {
          operation_name: operation,
          application: ctx.application,
        }),
      ]);
    } catch {
      /* ignore */
    }
    return resp;
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
    try {
      while (this._queue.length) await this._drain();
      await (this._inflight ?? Promise.resolve());
      return true;
    } catch {
      return false;
    }
  }
}

export default Meter;
