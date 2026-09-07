/**
 * Meter metering hook (Node).
 *
 * Thin, fail-safe wrapper that reports per-call LLM usage to Meter for
 * per-feature cost attribution. Mirrors the Python SDK. Cost is computed server
 * side from Meter's pricing tables — the SDK never sees prices.
 *
 *   - Never throws into the caller (errors are swallowed).
 *   - Recording enqueues and returns; a timer batches and posts, so the call
 *     path never waits on the network.
 *   - Bounded: a capped queue (10,000 events) whose oldest entries are dropped
 *     and counted when it fills. Metering degrades, the application does not.
 *   - No third-party dependencies (Node builtins only: global fetch + crypto, Node >= 18).
 *   - A no-op when no ingest URL/token is configured, so the same code runs
 *     whether or not the hook is enabled.
 *
 * Delivery: batched (up to `batchSize`) and flushed when a batch fills or after
 * `flushIntervalMs`, whichever comes first. `await meter.flush()` forces a send —
 * do that before `process.exit()`, which bypasses the automatic drain.
 *
 * Config (constructor opts or env): METER_INGEST_URL, METER_INGEST_TOKEN.
 */
import { createHash, randomUUID } from "node:crypto";

// Delivery defaults, matching the Python SDK. Batch size is well under the
// server's 10,000-event cap; the interval bounds what a hard kill loses.
const BATCH_SIZE = 50;
const FLUSH_INTERVAL_MS = 5000;
const QUEUE_MAX = 10000;
// Retried on failures that might clear: a timeout, a refused connection, a 5xx —
// or a Render free instance waking from sleep, which takes 30-60s and is the
// case this exists for.
const RETRY_BACKOFF_MS = [1000, 4000, 15000];
const MAX_ATTEMPTS = 3;
const SHUTDOWN_TIMEOUT_MS = 2000;

// Live meters, so one exit hook can drain them all.
const METERS = new Set();

export class Meter {
  constructor(featureId = null, opts = {}) {
    this.featureId = featureId;
    this.ingestUrl = opts.ingestUrl ?? process.env.METER_INGEST_URL ?? null;
    this.token = opts.token ?? process.env.METER_INGEST_TOKEN ?? null;
    // Default tags applied to every event (e.g. environment); per-call metadata
    // is merged on top. Optional — omit for the simplest setup.
    this.metadata = opts.metadata ?? {};
    this.fetchImpl = opts.fetchImpl ?? globalThis.fetch;
    // Every request is bounded. Without this a fetch has no timeout of its own,
    // so a hung or sleeping ingest endpoint leaves requests pending forever and
    // they pile up in the caller's process. Mirrors the Python SDK's 5s default.
    this.timeoutMs = opts.timeoutMs ?? 5000;

    // --- delivery: a bounded queue drained by a timer ---------------------
    this.batchSize = Math.max(1, opts.batchSize ?? BATCH_SIZE);
    this.flushIntervalMs = Math.max(0, opts.flushIntervalMs ?? FLUSH_INTERVAL_MS);
    this.queueMax = Math.max(1, opts.queueMax ?? QUEUE_MAX);
    this.maxAttempts = Math.max(1, opts.maxAttempts ?? MAX_ATTEMPTS);
    this.retryBackoffMs = opts.retryBackoffMs ?? RETRY_BACKOFF_MS;
    /** Events discarded because the queue was full, or undeliverable after every
     *  attempt. Metering degrades visibly rather than silently. */
    this.dropped = 0;
    this._queue = [];
    this._timer = null;
    this._draining = null;
    METERS.add(this);
    // Optimize mode (opt spec §4): measure traffic SHAPE — salted-hash
    // fingerprints and counts, never prompt text — to find duplicate calls and
    // uncached repeated prefixes. Off by default; work is off the call path,
    // bounded, and fail-safe.
    this.salt = opts.salt ?? null;
    this._saltPromise = null;
    this._optimizer = opts.optimize
      ? new Optimizer({
          prefixChars: opts.prefixChars,
          // Its own name: flushInterval now belongs to delivery.
          flushInterval: opts.optimizeFlushInterval,
        })
      : null;
  }

  get enabled() {
    return Boolean(this.ingestUrl && this.token && this.fetchImpl);
  }

  record({
    provider,
    model = "",
    tokensIn = 0,
    tokensOut = 0,
    featureId = null,
    occurredAt = null,
    latencyMs = null,
    metadata = null,
  }) {
    const event = {
      provider,
      model,
      tokens_in: tokensIn | 0,
      tokens_out: tokensOut | 0,
      feature_id: featureId ?? this.featureId,
      // Stamped HERE, not at send time: delivery is deferred, and the server
      // derives the billing month from this field (falling back to arrival).
      // Without it a call made at 23:59 on the last of the month could be posted
      // seconds later and land in the wrong one.
      occurred_at: occurredAt ?? nowIso(),
    };
    if (latencyMs != null) event.latency_ms = latencyMs | 0;
    const merged = { ...this.metadata, ...(metadata ?? {}) };
    if (Object.keys(merged).length) event.metadata = merged;
    return this._send([event]);
  }

  recordAnthropic(response, { featureId = null, model = null, latencyMs = null, metadata = null } = {}) {
    const u = (response && response.usage) || {};
    return this.record({
      provider: "anthropic",
      model: model ?? (response && response.model) ?? "",
      tokensIn: u.input_tokens ?? 0,
      tokensOut: u.output_tokens ?? 0,
      featureId,
      latencyMs,
      metadata,
    });
  }

  recordOpenAI(response, { featureId = null, model = null, latencyMs = null, metadata = null } = {}) {
    const u = (response && response.usage) || {};
    return this.record({
      provider: "openai",
      model: model ?? (response && response.model) ?? "",
      tokensIn: u.prompt_tokens ?? 0,
      tokensOut: u.completion_tokens ?? 0,
      featureId,
      latencyMs,
      metadata,
    });
  }

  // -------------------------------------------------------------------------
  // Delivery: enqueue on the call path, batch and post on a timer
  // -------------------------------------------------------------------------

  /** Queue events. Returns immediately and never rejects. */
  _send(events) {
    if (!this.enabled || !events.length) return;
    try {
      for (const event of events) {
        if (this._queue.length >= this.queueMax) {
          // Full: shed the oldest so the newest always gets in, and so a stalled
          // endpoint can never grow this without limit.
          this._queue.shift();
          this.dropped += 1;
        }
        this._queue.push(event);
      }
      if (this._queue.length >= this.batchSize) this._drain();
      else this._arm();
    } catch {
      // Queueing is on the call path, so it is as fail-safe as sending.
    }
  }

  /** Start the batching timer, if it isn't already running. */
  _arm() {
    if (this._timer || !this._queue.length) return;
    this._timer = setTimeout(() => {
      this._timer = null;
      this._drain();
    }, this.flushIntervalMs);
    // Never hold the process open on metering's account: an armed timer would
    // otherwise keep the event loop alive after the app is done.
    this._timer.unref?.();
  }

  /** Drain the queue, one batch at a time. Serialised: one in flight per meter. */
  _drain() {
    if (this._draining) return this._draining;
    if (this._timer) {
      clearTimeout(this._timer);
      this._timer = null;
    }
    const run = async () => {
      while (this._queue.length) {
        await this._deliver(this._queue.splice(0, this.batchSize));
      }
    };
    this._draining = run()
      .catch(() => {})
      .finally(() => {
        this._draining = null;
        this._arm(); // anything queued while we were sending
      });
    return this._draining;
  }

  /**
   * Deliver one batch, retrying transient failures. Never rejects.
   *
   * The batch id is generated ONCE and reused for every attempt, which is what
   * makes retrying safe: the server applies the first delivery and recognises
   * the rest as replays. Without it a retry after an ambiguous timeout — server
   * committed, response lost — would silently double a feature's cost.
   */
  async _deliver(events) {
    const batchId = randomUUID().replace(/-/g, "");
    for (let attempt = 0; attempt < this.maxAttempts; attempt += 1) {
      const outcome = await this._postOnce(events, batchId);
      if (outcome === "ok") return;
      if (outcome === "drop") {
        this.dropped += events.length;
        return;
      }
      if (attempt + 1 >= this.maxAttempts) break;
      // Jittered, so many processes recovering from one outage don't return in
      // lockstep.
      const base = this.retryBackoffMs[Math.min(attempt, this.retryBackoffMs.length - 1)];
      await new Promise((r) => {
        const t = setTimeout(r, base * (0.5 + Math.random()));
        t.unref?.();
      });
    }
    this.dropped += events.length;
  }

  /** One attempt -> "ok" | "retry" | "drop". Never rejects. */
  async _postOnce(events, batchId) {
    try {
      const res = await this.fetchImpl(this.ingestUrl, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${this.token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ events, batch_id: batchId }),
        signal: this._deadline(),
      });
      // fetch does not reject on 4xx/5xx, so a bad status has to be read off the
      // response — previously any status at all counted as delivered.
      const status = res && typeof res.status === "number" ? res.status : 200;
      if (status < 400) return "ok";
      // 4xx is our fault and fails identically forever: a bad token, a malformed
      // event. Retrying only hammers the endpoint. 429 is the exception.
      if (status < 500 && status !== 429) return "drop";
      return "retry";
    } catch {
      return "retry"; // timeout, DNS, refused, TLS: the endpoint may come back
    }
  }

  /**
   * Send what is queued now. Resolves true if the queue drained, false on
   * timeout. Never rejects.
   *
   * Worth awaiting before `process.exit()`, which bypasses the automatic drain.
   */
  async flush(timeoutMs = SHUTDOWN_TIMEOUT_MS) {
    if (!this.enabled) return true;
    try {
      const drained = this._drain();
      if (!drained) return true;
      let timer;
      const expired = new Promise((resolve) => {
        timer = setTimeout(() => resolve("timeout"), timeoutMs);
        timer.unref?.();
      });
      const result = await Promise.race([drained.then(() => "drained"), expired]);
      clearTimeout(timer);
      return result === "drained" && this._queue.length === 0;
    } catch {
      return false;
    }
  }

  /** An abort signal that fires at the timeout, or undefined if unsupported. */
  _deadline() {
    try {
      return AbortSignal.timeout(this.timeoutMs);
    } catch {
      return undefined; // very old runtime: unbounded, as before, but never throws
    }
  }

  _saltUrl() {
    if (!this.ingestUrl) return null;
    return this.ingestUrl.endsWith("/events")
      ? this.ingestUrl.slice(0, -"/events".length) + "/salt"
      : this.ingestUrl.replace(/\/+$/, "") + "/salt";
  }

  /** Fetch (once) the per-tenant fingerprint salt. Resolves to a salt or null. */
  _ensureSalt() {
    if (this._saltPromise) return this._saltPromise;
    if (this.salt != null) {
      this._saltPromise = Promise.resolve(this.salt);
      return this._saltPromise;
    }
    this._saltPromise = Promise.resolve()
      .then(() =>
        this.fetchImpl(this._saltUrl(), {
          method: "GET",
          headers: { Authorization: `Bearer ${this.token}` },
          signal: this._deadline(),
        }),
      )
      .then((r) => r.json())
      .then((d) => (this.salt = d && d.salt ? d.salt : null))
      .catch(() => (this.salt = null));
    return this._saltPromise;
  }

  /**
   * Record an auto-instrumented (wrap()) call. Token extraction, optimize-mode
   * hashing and the POST all run after the caller's call has returned/resolved,
   * so the request path never pays for them.
   */
  _recordWrapped(provider, request, response, latencyMs) {
    if (!this.enabled) return Promise.resolve(false);
    const build = (salt) => {
      const { model, tokensIn, tokensOut, cacheRead } = extractUsage(provider, response);
      const event = {
        provider,
        model,
        tokens_in: tokensIn | 0,
        tokens_out: tokensOut | 0,
        feature_id: this.featureId,
        latency_ms: latencyMs | 0,
        occurred_at: nowIso(), // see record(): the billing month comes from this
      };
      if (Object.keys(this.metadata).length) event.metadata = { ...this.metadata };
      const extra = [];
      if (this._optimizer) {
        const signal = this._optimizer.onCall(salt, provider, model, request, tokensIn, tokensOut, cacheRead);
        if (signal) event.signal = signal;
        extra.push(...this._optimizer.dueSummaries(this.featureId));
      }
      return this._send([event, ...extra]);
    };
    return this._optimizer ? this._ensureSalt().then(build) : build(null);
  }
}

/** UTC, ISO-8601 with a Z — the shape the ingest endpoint parses. */
function nowIso() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
}

/**
 * Give every live meter a brief chance to drain as the process winds down.
 *
 * `beforeExit` fires when the loop is about to empty and lets us schedule one
 * more turn of async work, which is what a flush needs. It deliberately does NOT
 * fire on `process.exit()` or an uncaught throw — await `meter.flush()` yourself
 * before a hard exit. Timers here are unref'd, so this can never be the reason a
 * process stays alive.
 */
let draining = false;
process.on?.("beforeExit", async () => {
  if (draining) return;
  draining = true;
  try {
    await Promise.all([...METERS].map((m) => m.flush(SHUTDOWN_TIMEOUT_MS).catch(() => false)));
  } finally {
    draining = false;
  }
});

// ---------------------------------------------------------------------------
// wrap() — auto-instrument a provider client (zero code at the call sites)
// ---------------------------------------------------------------------------
const COMPLETION_PATHS = {
  anthropic: [["messages", "create"]],
  openai: [
    ["chat", "completions", "create"],
    ["responses", "create"],
  ],
};

/** Pull { model, tokensIn, tokensOut, cacheRead } from a provider response. */
function extractUsage(provider, resp) {
  const u = (resp && resp.usage) || {};
  if (provider === "anthropic") {
    return {
      model: (resp && resp.model) || "",
      tokensIn: u.input_tokens ?? 0,
      tokensOut: u.output_tokens ?? 0,
      cacheRead: (u.cache_read_input_tokens ?? 0) > 0,
    };
  }
  // openai and OpenAI-compatible hosts
  const details = u.prompt_tokens_details || {};
  return {
    model: (resp && resp.model) || "",
    tokensIn: u.prompt_tokens ?? 0,
    tokensOut: u.completion_tokens ?? 0,
    cacheRead: (details.cached_tokens ?? 0) > 0,
  };
}

function detectProvider(client) {
  const name = ((client && client.constructor && client.constructor.name) || "").toLowerCase();
  if (name.includes("anthropic")) return "anthropic";
  if (name.includes("openai")) return "openai";
  throw new Error("Could not detect the LLM provider; pass { provider } to wrap().");
}

const hasUsage = (r) => Boolean(r && (r.usage || r.usage_metadata));
const isPrefix = (path, full) => path.length <= full.length && path.every((s, i) => s === full[i]);
const onPathPrefix = (provider, path) =>
  (COMPLETION_PATHS[provider] || []).some((full) => isPrefix(path, full));
const isCompletion = (provider, path) =>
  (COMPLETION_PATHS[provider] || []).some((full) => full.length === path.length && isPrefix(path, full));

function makeProxy(target, meter, provider, path = []) {
  return new Proxy(function () {}, {
    get(_t, prop) {
      if (typeof prop !== "string") return Reflect.get(target, prop);
      const real = target[prop];
      const nextPath = [...path, prop];
      if (onPathPrefix(provider, nextPath)) {
        const bound = typeof real === "function" ? real.bind(target) : real;
        return makeProxy(bound, meter, provider, nextPath);
      }
      return typeof real === "function" ? real.bind(target) : real;
    },
    apply(_t, _thisArg, args) {
      if (!isCompletion(provider, path)) return target(...args);
      const start = Date.now();
      const out = target(...args);
      const rec = (resp) => {
        try {
          // args[0] carries the request (messages/system/tools) optimize mode
          // fingerprints; recording runs after the call so the path never pays.
          if (hasUsage(resp)) meter._recordWrapped(provider, args[0] || {}, resp, Date.now() - start);
        } catch {
          /* metering must never throw into the caller */
        }
      };
      if (out && typeof out.then === "function") {
        out.then(rec).catch(() => {}); // record after the promise resolves; return original
        return out;
      }
      rec(out);
      return out;
    },
  });
}

/**
 * Wrap an LLM client so every completion call is metered automatically.
 * Your existing calls are unchanged; each is recorded with its latency after it
 * resolves. Non-completion attributes pass through; streaming responses (no
 * usage) are skipped. Provider is auto-detected; pass { provider } to override.
 * If you pass { meter }, it is used as-is (set featureId/metadata on it).
 */
export function wrap(
  client,
  { featureId = null, provider = null, metadata = null, ingestUrl = null, token = null, meter = null } = {},
) {
  const m = meter ?? new Meter(featureId, { ingestUrl, token, metadata });
  return makeProxy(client, m, provider ?? detectProvider(client));
}

// ---------------------------------------------------------------------------
// Optimize mode — measured optimization signals (opt spec §4). Traffic SHAPE
// only: salted-hash fingerprints and counts, never prompt or response text.
// ---------------------------------------------------------------------------
const sha = (...parts) => createHash("sha256").update(parts.join("\x1f")).digest("hex");

// Deterministic JSON with object keys sorted at every level (array order kept).
function stableStringify(value) {
  return JSON.stringify(value, (_k, v) =>
    v && typeof v === "object" && !Array.isArray(v)
      ? Object.fromEntries(Object.keys(v).sort().map((k) => [k, v[k]]))
      : v,
  );
}

function normalize(request) {
  if (!request || typeof request !== "object") return "";
  const payload = request.messages ?? request.input ?? request.contents ?? null;
  try {
    return stableStringify(payload);
  } catch {
    return String(payload);
  }
}

function staticPrefix(request, prefixChars) {
  let str = "";
  if (request && typeof request === "object" && (request.system != null || request.tools != null)) {
    str = stableStringify([request.system ?? null, request.tools ?? null]);
  }
  if (!str) str = normalize(request).slice(0, prefixChars);
  return { str, prefixTokens: Math.max(0, Math.floor(str.length / 4)) };
}

/**
 * Client-side, bounded signal collector (opt spec §4.1). Node is single-threaded,
 * so no locking is needed. A duplicate LRU emits one-off 'duplicate' signals; a
 * prefix counter map flushes 'prefix' summaries on a timer or at capacity.
 */
class Optimizer {
  constructor({ prefixChars = 2000, flushInterval = 60000, dupCapacity = 5000, prefixCapacity = 512 } = {}) {
    this.prefixChars = prefixChars;
    this.flushInterval = flushInterval; // milliseconds
    this.dupCapacity = dupCapacity;
    this.prefixCapacity = prefixCapacity;
    this.seen = new Map(); // request_fp -> true (insertion-ordered => LRU)
    this.prefixes = new Map(); // prefix_fp -> counts
    this.lastFlush = Date.now();
  }

  onCall(salt, provider, model, request, tokensIn, tokensOut, cacheRead) {
    if (!salt) return null; // no salt -> no signals (never emit unsalted hashes)
    const reqFp = sha(salt, provider, model, normalize(request));
    const { str, prefixTokens } = staticPrefix(request, this.prefixChars);
    const prefixFp = sha(salt, provider, model, str);

    const duplicate = this.seen.has(reqFp);
    if (duplicate) {
      this.seen.delete(reqFp);
      this.seen.set(reqFp, true);
    } else {
      this.seen.set(reqFp, true);
      while (this.seen.size > this.dupCapacity) this.seen.delete(this.seen.keys().next().value);
    }

    const e = this.prefixes.get(prefixFp) || {
      provider,
      model,
      count: 0,
      prefixTokens,
      cached: 0,
      tin: 0,
      tout: 0,
    };
    e.count += 1;
    e.tin += tokensIn | 0;
    e.tout += tokensOut | 0;
    e.prefixTokens = Math.max(e.prefixTokens, prefixTokens);
    if (cacheRead) e.cached += 1;
    this.prefixes.set(prefixFp, e);

    return duplicate ? { kind: "duplicate", fingerprint: reqFp, count: 1 } : null;
  }

  dueSummaries(featureId) {
    const now = Date.now();
    if (now - this.lastFlush < this.flushInterval && this.prefixes.size < this.prefixCapacity) return [];
    this.lastFlush = now;
    const items = this.prefixes;
    this.prefixes = new Map();
    const out = [];
    for (const [fingerprint, e] of items) {
      out.push({
        provider: e.provider,
        model: e.model,
        feature_id: featureId,
        signal: {
          kind: "prefix",
          fingerprint,
          count: e.count,
          prefix_tokens: e.prefixTokens,
          cached_count: e.cached,
          tokens_in: e.tin,
          tokens_out: e.tout,
        },
      });
    }
    return out;
  }
}
