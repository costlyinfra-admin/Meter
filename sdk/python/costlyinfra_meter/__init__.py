"""Meter — request-level AI economics.

What this measures is not requests in general. It measures AI work: an agent
run, and the LLM calls, retrievals, tool calls, guardrails and evaluations
inside it. Meter is a cost product, not an APM, and instrumenting a database
query here would be noise.

Two ways in, and the simple one needs no concepts:

    meter = Meter(application="support-agent")
    client = meter.wrap(anthropic_client, feature_id="answer-generation")
    client.messages.create(...)          # -> a one-span trace, automatically

    with meter.agent("resolve-ticket", feature_id="ticket-resolution") as run:
        run.llm("classify", lambda: anthropic_client.messages.create(...))
        docs = run.tool("retrieve-documents", retrieve_documents)
        run.llm("generate-answer", lambda: anthropic_client.messages.create(...))

**What metering never sends.** Prompts, responses, messages, tool arguments,
tool results, retrieved documents, exception messages and stack traces. The
metering events this SDK constructs have no field for them, and the server
refuses a payload that carries one. What travels is identity, counts, timing and
money: which prompt version, how many tokens, how long, how much.

**The one exception, off by default: consented prompt capture.** For Prompt
Optimization, `Meter(capture_prompts=True)` lets a wrapped client send a small
sample of prompt text — the system prompt, the text of the messages and the text
of the reply — for calls named with a `prompt_id` and `prompt_version`. It sends
nothing unless your organization has also agreed in Meter and switched capture on
for that feature. It never sends tool calls, tool results, images or files, and
it travels on its own channel, so it can never slow or break metering.

**It must never break your agent.** Every path here is wrapped: a Meter failure
degrades to sending nothing. Delivery happens on a background worker off your
request path, over a bounded queue that sheds oldest-first rather than growing,
and `meter.dropped` tells you if it ever came to that.
"""

from __future__ import annotations

import atexit
import datetime as dt
import hashlib
import json
import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import OrderedDict, deque
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Callable, NamedTuple, Optional

__version__ = "2.3.0"

FLUSH_INTERVAL = 2.0
BATCH_SIZE = 50
QUEUE_MAX = 10_000
MAX_ATTEMPTS = 3
RETRY_BACKOFF = (0.5, 2.0)
SHUTDOWN_TIMEOUT = 3.0
#: How often a long-running trace says "still here". Well inside a sensible
#: stale threshold (the server's default is 10 minutes), so an agent that is
#: genuinely working is never mistaken for one that has hung.
HEARTBEAT_INTERVAL = 30.0

#: Consented prompt capture. 1 call in 100, at most 50 samples a day per prompt:
#: optimization needs a representative handful, not a copy of the traffic. The
#: server enforces its own caps whatever these are set to.
CAPTURE_SAMPLE_RATE = 0.01
CAPTURE_MAX_PER_DAY = 50
#: Larger samples are dropped, not truncated: a truncated prompt would later be
#: evaluated as a different prompt. Matches the server's limit.
CAPTURE_MAX_BYTES = 64 * 1024
CAPTURE_QUEUE_MAX = 100

# -- optimize mode ---------------------------------------------------------
#: How much of a request counts as its cacheable static head when the provider's
#: own static blocks (system prompt, tool definitions) are not separable.
PREFIX_CHARS = 2000
#: Prefix counters are aggregates; they leave on a timer, not per call.
OPTIMIZE_FLUSH_INTERVAL = 60.0
#: Bounds. A fingerprint map that grows with traffic is a leak, not a feature.
DUP_CAPACITY = 5000
PREFIX_CAPACITY = 512
#: How long a first call stays a plausible cache hit for a later identical one.
#:
#: There was no window at all: a request repeated six hours later counted as an
#: avoidable duplicate, which asserts that the first response was still good —
#: a claim about freshness nobody had checked. Ten minutes is the documented
#: starting point, short enough that "you could have served the first answer" is
#: usually true and long enough to catch real retry storms and fan-out.
#:
#: Measured from the FIRST call of a group, never extended by later ones, so
#: steady repeating traffic cannot keep one supposed cached response alive
#: forever.
DUPLICATE_WINDOW = 600.0
#: How long a provider keeps a prompt prefix cached after it is written.
#:
#: Caching is not free: writing a prefix into the cache costs MORE than sending
#: it uncached, and only the reads that follow pay that back. So the number of
#: writes decides whether caching this prefix saves money at all, and the only
#: place that can be counted is here, next to the call timestamps.
#:
#: A window opens when a prefix is seen after a gap longer than this — the point
#: at which a provider would have evicted it and the next call would have to
#: write it again. Five minutes is the shortest TTL the priced providers offer
#: and their default, so counting windows at five minutes counts the MOST writes
#: caching could incur, and the resulting saving is the most conservative one.
CACHE_WINDOW = 300.0
#: How long the server's answer to "is capture open for this feature" is trusted.
#: A withdrawal takes effect within this window at the latest, and on the very
#: next sample, because the server re-checks consent every time.
CAPTURE_OPEN_TTL = 300.0

_METERS: set = set()

SPAN_KINDS = ("workflow", "llm", "embedding", "retrieval", "tool", "guardrail", "evaluation")


def _now_iso() -> str:
    """An event timestamp, with sub-second precision.

    The precision is not cosmetic. Spans are ordered by when they started, and
    most agent steps finish in well under a second — at whole-second resolution
    an entire workflow shares one timestamp and its waterfall renders in
    arbitrary order, which is the one thing that view exists to show.
    """
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _new_id() -> str:
    """Generated here, not by the server.

    A client-side id is what lets a span be referred to before the server has
    seen it — which is what makes out-of-order delivery, resumed work in another
    process, and retry-safe replay all work.
    """
    return uuid.uuid4().hex


class Meter:
    """The client. One per process is plenty.

    Unconfigured (no ingest URL or token) it is a no-op: every method still
    works and returns your value, and nothing is sent. That is deliberate —
    importing Meter into a test suite or a local script should cost nothing and
    require no conditionals at the call sites.
    """

    def __init__(
        self,
        *,
        application: Optional[str] = None,
        environment: Optional[str] = None,
        release_version: Optional[str] = None,
        ingest_url: Optional[str] = None,
        token: Optional[str] = None,
        feature_id: Optional[str] = None,
        timeout: float = 5.0,
        flush_interval: float = FLUSH_INTERVAL,
        batch_size: int = BATCH_SIZE,
        queue_max: int = QUEUE_MAX,
        max_attempts: int = MAX_ATTEMPTS,
        retry_backoff: tuple = RETRY_BACKOFF,
        heartbeat_interval: float = HEARTBEAT_INTERVAL,
        transport: Optional[Any] = None,
        capture_prompts: Optional[bool] = None,
        capture_url: Optional[str] = None,
        capture_sample_rate: float = CAPTURE_SAMPLE_RATE,
        redact: Optional[Callable[[dict], Optional[dict]]] = None,
        optimize: bool = False,
        prefix_chars: int = PREFIX_CHARS,
        optimize_flush_interval: float = OPTIMIZE_FLUSH_INTERVAL,
        optimize_window: float = DUPLICATE_WINDOW,
        salt: Optional[str] = None,
    ):
        self.application = application or os.environ.get("METER_APPLICATION") or "default"
        self.environment = environment or os.environ.get("METER_ENVIRONMENT") or "production"
        self.release_version = release_version or os.environ.get("METER_RELEASE_VERSION")
        self.ingest_url = ingest_url or os.environ.get("METER_INGEST_URL")
        self.token = token or os.environ.get("METER_INGEST_TOKEN")
        self.feature_id = feature_id
        self.timeout = timeout
        self._transport = transport
        self._heartbeat_interval = max(1.0, float(heartbeat_interval))

        self._batch_size = max(1, int(batch_size))
        self._flush_interval = max(0.0, float(flush_interval))
        self._queue_max = max(1, int(queue_max))
        self._max_attempts = max(1, int(max_attempts))
        self._retry_backoff = tuple(retry_backoff) or (1.0,)
        self._queue: deque = deque()
        self._cv = threading.Condition()
        self._worker: Optional[threading.Thread] = None
        self._worker_pid: Optional[int] = None
        self._sending = False
        self._flush_now = False
        #: Events discarded because the queue was full or delivery failed for
        #: good. Metering degrades visibly rather than silently.
        self.dropped = 0

        # Consented prompt capture: the developer's half of a double key. The
        # organization's half lives in Meter, and nothing is sent without both.
        env_capture = (os.environ.get("METER_CAPTURE_PROMPTS") or "").strip().lower()
        self.capture_prompts = (
            bool(capture_prompts) if capture_prompts is not None
            else env_capture in ("1", "true", "yes", "on")
        )
        self.capture_url = (
            capture_url or os.environ.get("METER_CAPTURE_URL")
            or _capture_url_from(self.ingest_url)
        )
        self.capture_sample_rate = min(1.0, max(0.0, float(capture_sample_rate)))
        self._redact = redact
        self._capture_queue: deque = deque()
        self._capture_checks: set = set()
        self._capture_checking: set = set()
        #: feature_id -> (open, trusted_until_monotonic)
        self._capture_open: dict = {}
        #: (feature_id, prompt_id) -> (utc_date, samples_offered)
        self._capture_daily: dict = {}
        #: (feature_id, prompt_id) -> utc_date the server said "enough for today"
        self._capture_capped: dict = {}
        #: Samples chosen but not delivered: refused, too large, redacted away or
        #: failed. Calls simply not sampled are not counted.
        self.capture_dropped = 0

        # Optimize mode: measure the SHAPE of traffic — salted-hash fingerprints
        # and counts, never prompt text — so the duplicate-call and uncached-
        # prefix findings are measured rather than rules of thumb. Off by
        # default, bounded in memory, and never on the caller's thread.
        self._salt = salt
        self._salt_state = "ready" if salt else "cold"
        self._optimizer = (
            _Optimizer(
                self,
                prefix_chars=prefix_chars,
                flush_interval=optimize_flush_interval,
                window=optimize_window,
            )
            if optimize
            else None
        )
        _METERS.add(self)

    @property
    def enabled(self) -> bool:
        return bool(self.ingest_url and self.token)

    @property
    def capture_enabled(self) -> bool:
        """True when this process may offer prompt samples at all.

        Only the developer's half. Whether a sample is actually sent also needs
        the organization's consent for the feature, which the server decides.
        """
        return bool(self.enabled and self.capture_prompts and self.capture_url)

    # -- the two entry points ---------------------------------------------
    def wrap(self, client: Any, *, feature_id: Optional[str] = None,
             application: Optional[str] = None, provider: Optional[str] = None,
             prompt_id: Optional[str] = None, prompt_version: Optional[str] = None,
             customer_id: Optional[str] = None, cache_scope: Optional[str] = None) -> Any:
        """Instrument a provider client so each call becomes its own trace.

        For the common case — one model call, no workflow around it — explicit
        trace management would be ceremony with no payoff. A wrapped call opens
        a trace, records one LLM span, and closes it.

        `provider` is inferred from the client's type, which works for the
        official SDKs and fails quietly for a wrapper, a subclass or a test
        double — so it can be stated outright. Passing it wrong instruments
        nothing rather than instrumenting the wrong thing.

        `customer_id` / `cache_scope` exist for optimize mode and nothing else.
        Two identical requests are only interchangeable inside whatever boundary
        the application would actually reuse a response across; without one
        stated, Meter records the repeat but will not call it safely reusable.
        `agent()` has always taken a customer; this is the wrapped-client
        equivalent. Neither ever reaches the provider's API.
        """
        return _Wrapped(client, self, provider or _detect_provider(client),
                        feature_id=feature_id or self.feature_id,
                        application=application or self.application,
                        prompt_id=prompt_id, prompt_version=prompt_version,
                        customer_id=customer_id, cache_scope=cache_scope)

    @contextmanager
    def agent(self, operation_name: str, *, feature_id: Optional[str] = None,
              customer_id: Optional[str] = None, application: Optional[str] = None,
              trace_id: Optional[str] = None,
              parent_span_id: Optional[str] = None) -> Iterator[AgentRun]:
        """A multi-step run. Steps recorded inside it become its spans.

        The context manager is the point: leaving the block ends the trace and
        stops its heartbeat, on the happy path and on an exception alike, so a
        crashed agent is recorded as failed rather than left looking hung
        forever.
        """
        run = AgentRun(
            self,
            operation_name=operation_name,
            feature_id=feature_id or self.feature_id,
            customer_id=customer_id,
            application=application or self.application,
            trace_id=trace_id,
            parent_span_id=parent_span_id,
        )
        run._start()
        try:
            yield run
        except BaseException:
            # The status, never the exception. What went wrong is in the
            # customer's own logs; it is not ours to copy off their machine.
            run._finish("trace.failed")
            raise
        else:
            run._finish("trace.completed")

    @contextmanager
    def resume(self, context: Any, **overrides: Any) -> Iterator[AgentRun]:
        """Continue a trace started elsewhere — a queue worker, another service.

        The exported context carries identifiers only, so passing it through a
        message broker moves no customer data and no credentials.
        """
        ctx = context if isinstance(context, dict) else json.loads(context or "{}")
        with self.agent(
            overrides.get("operation_name") or ctx.get("operation_name") or "resumed",
            feature_id=overrides.get("feature_id", ctx.get("feature_id")),
            customer_id=overrides.get("customer_id", ctx.get("customer_id")),
            application=overrides.get("application", ctx.get("application")),
            trace_id=ctx.get("trace_id"),
            parent_span_id=ctx.get("parent_span_id"),
        ) as run:
            yield run

    # -- event construction ------------------------------------------------
    def _event(self, event_type: str, trace_id: str, **fields: Any) -> dict:
        event = {
            "event_type": event_type,
            "event_id": _new_id(),
            "trace_id": trace_id,
            "application": fields.pop("application", None) or self.application,
            "environment": self.environment,
            "occurred_at": _now_iso(),
        }
        if self.release_version:
            event["release_version"] = self.release_version
        for key, value in fields.items():
            if value is not None:
                event[key] = value
        return event

    def _emit(self, event: dict) -> None:
        self._send([event])

    def _emit_all(self, events: list) -> None:
        if events:
            self._send(events)

    # -- delivery ----------------------------------------------------------
    def _send(self, events: list) -> None:
        """Queue events. Returns immediately; never raises."""
        if not self.enabled or not events:
            return
        try:
            with self._cv:
                self._reset_after_fork_locked()
                for event in events:
                    if len(self._queue) >= self._queue_max:
                        # Shed the oldest so the newest always gets in, and a
                        # stalled endpoint can never grow this unboundedly.
                        self._queue.popleft()
                        self.dropped += 1
                    self._queue.append(event)
                self._ensure_worker_locked()
                self._cv.notify()
        except Exception:
            pass  # metering must never raise into the caller's path

    def _reset_after_fork_locked(self) -> None:
        """Start clean in a forked child.

        Threads do not survive fork, so a child inherits a worker that will
        never run and a copy of the parent's queue the parent is still going to
        send. Under pre-fork servers that would mean metering nothing per worker
        and duplicating whatever was in flight.
        """
        pid = os.getpid()
        if self._worker_pid is not None and self._worker_pid != pid:
            self._queue.clear()
            self._worker = None
            self._worker_pid = None
            self._sending = False
            self._flush_now = False
            self._capture_queue.clear()
            self._capture_checks.clear()
            self._capture_checking.clear()

    def _ensure_worker_locked(self) -> None:
        if self._worker is not None:
            return
        worker = _spawn(self._run)
        if worker is not None:
            self._worker = worker
            self._worker_pid = os.getpid()

    def _run(self) -> None:
        while True:
            with self._cv:
                while not (self._queue or self._capture_queue or self._capture_checks):
                    self._cv.wait()
                batch: list = []
                if self._queue:
                    deadline = time.monotonic() + self._flush_interval
                    while len(self._queue) < self._batch_size and not self._flush_now:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        self._cv.wait(remaining)
                    batch = [
                        self._queue.popleft()
                        for _ in range(min(len(self._queue), self._batch_size))
                    ]
                checks = list(self._capture_checks)
                self._capture_checks.clear()
                self._capture_checking.update(checks)
                samples = list(self._capture_queue)
                self._capture_queue.clear()
                self._sending = True
            try:
                # Metering first, always: capture is optional and must never be
                # the reason a batch of events waits.
                if batch:
                    self._deliver(batch)
                for feature_id in checks:
                    self._check_capture(feature_id)
                for item in samples:
                    self._deliver_sample(item)
            finally:
                with self._cv:
                    self._sending = False
                    if not self._queue:
                        self._flush_now = False
                    self._cv.notify_all()

    def flush(self, timeout: float = SHUTDOWN_TIMEOUT) -> bool:
        """Send what is queued now. True if it drained within `timeout`."""
        if not self.enabled:
            return True
        # Counters that have been accumulating but have not reached their
        # interval belong in this flush; atexit calls this, and after it there
        # is no later flush for them to make.
        if self._optimizer is not None:
            try:
                self._emit_all(self._optimizer.due_summaries(force=True))
            except Exception:
                pass  # a lost summary is never worth failing a flush over
        try:
            deadline = time.monotonic() + max(0.0, timeout)
            with self._cv:
                if not self._pending_locked():
                    return True
                self._reset_after_fork_locked()
                self._ensure_worker_locked()
                if self._worker is None:
                    return False
                self._flush_now = True
                self._cv.notify_all()
                while self._pending_locked():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._cv.wait(remaining)
            return True
        except Exception:
            return False

    def _deliver(self, events: list) -> None:
        """Deliver one batch, retrying transient failures. Never raises.

        The batch id is generated ONCE and reused for every attempt. That is
        what makes retrying safe: the server applies the first delivery and
        recognises the rest as replays, so a retry after an ambiguous timeout —
        server committed, response lost — cannot double a run's cost.
        """
        if not self.enabled or not events:
            return
        batch_id = _new_id()
        for attempt in range(self._max_attempts):
            outcome = self._post_once(events, batch_id)
            if outcome != "retry":
                if outcome == "drop":
                    self.dropped += len(events)
                return
            if attempt + 1 >= self._max_attempts:
                break
            delay = self._retry_backoff[min(attempt, len(self._retry_backoff) - 1)]
            # Jitter, so many processes recovering from one outage do not
            # return in lockstep.
            time.sleep(delay * (0.5 + random.random()))
        self.dropped += len(events)

    def _post_once(self, events: list, batch_id: str) -> str:
        """One attempt -> "ok" | "retry" | "drop". Never raises."""
        body = json.dumps({"events": events, "batch_id": batch_id}).encode("utf-8")
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        try:
            if self._transport is not None:
                self._transport(self.ingest_url, headers, body)
                return "ok"
            req = urllib.request.Request(self.ingest_url, data=body, headers=headers, method="POST")
            urllib.request.urlopen(req, timeout=self.timeout).read()
            return "ok"
        except urllib.error.HTTPError as exc:
            # 4xx is our fault and fails identically forever: a bad token, a
            # malformed event. Retrying only hammers the endpoint. 429 is the
            # exception — it is an explicit invitation to come back.
            if 400 <= exc.code < 500 and exc.code != 429:
                return "drop"
            return "retry"
        except Exception:
            return "retry"  # timeout, DNS, refused, TLS: it may yet return

    def _pending_locked(self) -> bool:
        return bool(
            self._queue or self._sending or self._capture_queue
            or self._capture_checks or self._capture_checking
        )

    # -- consented prompt capture -------------------------------------------
    def _offer_sample(self, provider: str, path: tuple, kwargs: dict, resp: Any, *,
                      feature_id: Optional[str], prompt_id: Optional[str],
                      prompt_version: Optional[str], usage: dict, latency_ms: int) -> None:
        """Consider one completed call for a prompt sample. Cheap; never raises.

        Every reason to say no is checked before anything is kept: the
        developer's switch, a named prompt, a call shape this SDK can read, the
        server's word that capture is open for this feature, the sample rate and
        the daily cap. Reading the request happens later, on the worker, off the
        caller's path.
        """
        if not self.capture_enabled or not (feature_id and prompt_id and prompt_version):
            return
        if (provider, path) not in _CAPTURE_PATHS:
            return
        if not usage.get("tokens_in") and not usage.get("tokens_out"):
            return  # a stream or an unfamiliar response: nothing complete to sample
        now = time.monotonic()
        with self._cv:
            self._reset_after_fork_locked()
            known = self._capture_open.get(feature_id)
            if known is None or known[1] <= now:
                # Ask first. This call is not sampled: until the server says yes,
                # not a single prompt is kept, even briefly, in memory.
                if feature_id not in self._capture_checking:
                    self._capture_checks.add(feature_id)
                    self._ensure_worker_locked()
                    self._cv.notify()
                return
            if not known[0]:
                return
            day = dt.datetime.now(dt.timezone.utc).date()
            key = (feature_id, prompt_id)
            if self._capture_capped.get(key) == day:
                return
            if random.random() >= self.capture_sample_rate:
                return
            counted_day, count = self._capture_daily.get(key, (day, 0))
            if counted_day != day:
                count = 0
            if count >= CAPTURE_MAX_PER_DAY:
                return
            self._capture_daily[key] = (day, count + 1)
            if len(self._capture_queue) >= CAPTURE_QUEUE_MAX:
                self.capture_dropped += 1
                return
            # A copy of the message list, not of the messages: an application
            # that appends to its conversation after the call must not change
            # what this call is recorded as having sent.
            messages = kwargs.get("messages")
            snapshot = dict(kwargs)
            if isinstance(messages, (list, tuple)):
                snapshot["messages"] = list(messages)
            self._capture_queue.append({
                "provider": provider,
                "kwargs": snapshot,
                "resp": resp,
                "feature_id": feature_id,
                "prompt_id": prompt_id,
                "prompt_version": prompt_version,
                "usage": dict(usage),
                "latency_ms": latency_ms,
                "captured_at": _now_iso(),
            })
            self._ensure_worker_locked()
            self._cv.notify()

    def _check_capture(self, feature_id: str) -> None:
        """Ask the server whether capture is open for a feature. Never raises."""
        is_open = False
        try:
            base = self.capture_url.rsplit("/", 1)[0]
            url = f"{base}/open?feature_id={urllib.parse.quote(feature_id)}"
            status, body = self._request("GET", url, None)
            if status == 200:
                is_open = bool(json.loads(body.decode() or "{}").get("open"))
        except Exception:
            is_open = False
        with self._cv:
            self._capture_open[feature_id] = (is_open, time.monotonic() + CAPTURE_OPEN_TTL)
            self._capture_checking.discard(feature_id)
            self._cv.notify_all()

    def _deliver_sample(self, item: dict) -> None:
        """Read, redact, size-check and post one sample. One attempt. Never raises."""
        try:
            sample = _read_sample(item["provider"], item["kwargs"], item["resp"])
        except Exception:
            sample = None
        if sample is None:
            return  # not text this SDK can read: nothing to send, nothing lost
        usage = item["usage"]
        model = usage.get("model") or item["kwargs"].get("model")
        if not isinstance(model, str) or not model:
            return
        sample.update({
            "feature_id": item["feature_id"],
            "prompt_id": item["prompt_id"],
            "prompt_version": item["prompt_version"],
            "provider": usage.get("provider") or item["provider"],
            "model": model,
            "tokens_in": usage.get("tokens_in"),
            "tokens_out": usage.get("tokens_out"),
            "latency_ms": item["latency_ms"],
            "captured_at": item["captured_at"],
        })
        sample = {k: v for k, v in sample.items() if v is not None}
        if self._redact is not None:
            try:
                sample = self._redact(sample)
            except Exception:
                sample = None  # a failing redactor fails closed: send nothing
            if not isinstance(sample, dict):
                self.capture_dropped += 1
                return
        try:
            body = json.dumps(sample).encode("utf-8")
        except (TypeError, ValueError):
            self.capture_dropped += 1
            return
        if len(body) > CAPTURE_MAX_BYTES:
            self.capture_dropped += 1
            return
        status, payload = self._request("POST", self.capture_url, body)
        if status == 200:
            return
        reason = None
        try:
            reason = json.loads(payload.decode() or "{}").get("reason")
        except Exception:
            pass
        with self._cv:
            if status == 403:
                # Consent is not there for this feature, whatever the cache said.
                self._capture_open[item["feature_id"]] = (
                    False, time.monotonic() + CAPTURE_OPEN_TTL
                )
            elif status == 429 or reason == "daily_cap":
                self._capture_capped[(item["feature_id"], item["prompt_id"])] = (
                    dt.datetime.now(dt.timezone.utc).date()
                )
            self.capture_dropped += 1

    # -- optimize mode -----------------------------------------------------
    @property
    def optimize_enabled(self) -> bool:
        return self._optimizer is not None

    def _salt_url(self) -> Optional[str]:
        """The salt endpoint beside this meter's ingest URL, or None."""
        if not self.ingest_url:
            return None
        base = self.ingest_url.rstrip("/")
        if base.endswith("/events"):
            return base[: -len("/events")] + "/salt"
        return base + "/salt"

    def salt(self) -> Optional[str]:
        """The tenant's fingerprint salt, or None until it arrives.

        Fetched once, on a thread of its own. v1 fetched it inline on the first
        wrapped call, which put an HTTP round trip in front of a customer's
        request — the one thing optimize mode promises never to do. Returning
        None until it lands costs a few early signals, which are aggregates, and
        costs the caller nothing.

        A failed fetch disables signals permanently rather than retrying: without
        a salt the only alternative is an unsalted hash, and that must never be
        emitted.
        """
        if self._salt_state == "ready":
            return self._salt
        if self._salt_state == "cold":
            self._salt_state = "fetching"
            if _spawn(self._fetch_salt) is None:
                self._salt_state = "failed"
        return None

    def _fetch_salt(self) -> None:
        if not self.enabled:
            self._salt_state = "failed"
            return
        try:
            url = self._salt_url()
            status, body = self._request("GET", url, None)
            if status != 200:
                raise ValueError(status)
            value = json.loads(body).get("salt")
            if not value:
                raise ValueError("no salt")
            self._salt = value
            self._salt_state = "ready"
        except Exception:
            # Fail safe, and quietly: optimize mode is an extra, and a customer's
            # application must never learn that Meter could not reach Meter.
            self._salt_state = "failed"

    def _request(self, method: str, url: str, body: Optional[bytes]) -> tuple:
        """One exchange on the capture channel -> (status, body). Never raises.

        Unlike metering this does not retry: a sample is one of many, and a
        missed one costs nothing a later one will not provide.
        """
        headers = {"Authorization": f"Bearer {self.token}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            if self._transport is not None:
                result = self._transport(url, headers, body)
                if isinstance(result, tuple):
                    status, data = result
                    if not isinstance(data, (bytes, bytearray)):
                        data = json.dumps(data).encode()
                    return int(status), bytes(data)
                if result is None:
                    return 200, b""
                if isinstance(result, (bytes, bytearray)):
                    return 200, bytes(result)
                return 200, json.dumps(result).encode()
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, exc.read() or b""
            except Exception:
                return exc.code, b""
        except Exception:
            return 0, b""


class AgentRun:
    """One agent or workflow run, and the steps recorded inside it."""

    def __init__(self, meter: Meter, *, operation_name: str, feature_id: Optional[str],
                 customer_id: Optional[str], application: Optional[str],
                 trace_id: Optional[str] = None, parent_span_id: Optional[str] = None):
        self._meter = meter
        self.operation_name = operation_name
        self.trace_id = trace_id or _new_id()
        self.feature_id = feature_id
        self.customer_id = customer_id
        self.application = application
        #: Steps nest under this when the run was resumed from another process,
        #: so work continued in a worker still hangs off the step that queued it.
        self.parent_span_id = parent_span_id
        self._heartbeat: Optional[threading.Timer] = None
        self._done = threading.Event()

    # -- lifecycle ---------------------------------------------------------
    def _start(self) -> None:
        self._meter._emit(
            self._meter._event(
                "trace.started", self.trace_id,
                operation_name=self.operation_name,
                feature_id=self.feature_id,
                customer_id=self.customer_id,
                application=self.application,
            )
        )
        self._schedule_heartbeat()

    def _schedule_heartbeat(self) -> None:
        """Say "still here" while the run is long.

        Without this a slow-but-healthy agent looks identical to a hung one.
        The timer is a daemon so it can never hold the process open, and it
        re-arms itself only while the run is unfinished.
        """
        if self._done.is_set() or not self._meter.enabled:
            return
        try:
            timer = threading.Timer(self._meter._heartbeat_interval, self._beat)
            timer.daemon = True
            timer.start()
            self._heartbeat = timer
        except Exception:
            self._heartbeat = None  # cannot start a thread: degrade, never raise

    def _beat(self) -> None:
        if self._done.is_set():
            return
        self._meter._emit(self._meter._event("trace.heartbeat", self.trace_id,
                                             application=self.application))
        self._schedule_heartbeat()

    def _finish(self, event_type: str) -> None:
        # Set BEFORE cancelling, so a beat racing us sees the run is over and
        # returns rather than re-arming a timer nothing will ever cancel.
        self._done.set()
        timer, self._heartbeat = self._heartbeat, None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
        self._meter._emit(
            self._meter._event(event_type, self.trace_id,
                               operation_name=self.operation_name,
                               application=self.application)
        )

    # -- steps -------------------------------------------------------------
    def span(self, kind: str, operation_name: str, fn: Callable[[], Any], *,
             feature_id: Optional[str] = None, prompt_id: Optional[str] = None,
             prompt_version: Optional[str] = None, prompt_hash: Optional[str] = None,
             parent_span_id: Optional[str] = None) -> Any:
        """Run `fn`, record it as a step, and return whatever it returned.

        The step's status follows the call: an exception makes it `error` and is
        re-raised unchanged. Nothing about the exception is transmitted.
        """
        if kind not in SPAN_KINDS:
            kind = "tool"
        span_id = _new_id()
        parent = parent_span_id or self.parent_span_id
        self._emit_span("span.started", span_id, kind, operation_name, parent=parent)
        began = time.perf_counter()
        try:
            result = fn()
        except BaseException:
            self._emit_span(
                "span.failed", span_id, kind, operation_name, parent=parent,
                latency_ms=int((time.perf_counter() - began) * 1000),
                prompt_id=prompt_id, prompt_version=prompt_version, prompt_hash=prompt_hash,
            )
            raise
        latency_ms = int((time.perf_counter() - began) * 1000)
        usage = _usage_of(result) if kind in ("llm", "embedding") else {}
        self._emit_span(
            "span.completed", span_id, kind, operation_name, parent=parent,
            latency_ms=latency_ms, feature_id=feature_id,
            prompt_id=prompt_id, prompt_version=prompt_version, prompt_hash=prompt_hash,
            **usage,
        )
        return result

    def llm(self, operation_name: str, fn: Callable[[], Any], **kw: Any) -> Any:
        """A model call. Tokens and model are read from the response."""
        return self.span("llm", operation_name, fn, **kw)

    def tool(self, operation_name: str, fn: Callable[[], Any], **kw: Any) -> Any:
        """A tool call. Its arguments and result are never looked at."""
        return self.span("tool", operation_name, fn, **kw)

    def retrieval(self, operation_name: str, fn: Callable[[], Any], **kw: Any) -> Any:
        return self.span("retrieval", operation_name, fn, **kw)

    def embedding(self, operation_name: str, fn: Callable[[], Any], **kw: Any) -> Any:
        return self.span("embedding", operation_name, fn, **kw)

    def guardrail(self, operation_name: str, fn: Callable[[], Any], **kw: Any) -> Any:
        return self.span("guardrail", operation_name, fn, **kw)

    def evaluation(self, operation_name: str, fn: Callable[[], Any], **kw: Any) -> Any:
        return self.span("evaluation", operation_name, fn, **kw)

    def _emit_span(self, event_type: str, span_id: str, kind: str, operation_name: str, *,
                   parent: Optional[str] = None, **fields: Any) -> None:
        self._meter._emit(
            self._meter._event(
                event_type, self.trace_id,
                span_id=span_id, parent_span_id=parent, span_kind=kind,
                operation_name=operation_name,
                feature_id=fields.pop("feature_id", None) or self.feature_id,
                customer_id=self.customer_id,
                application=self.application,
                **fields,
            )
        )

    # -- propagation -------------------------------------------------------
    def export_context(self) -> dict:
        """Identifiers a worker needs to continue this run — and nothing else.

        No token, no customer content, no credentials: this is designed to be
        put on a queue, which means assuming it will be logged somewhere.
        """
        context = {
            "trace_id": self.trace_id,
            "parent_span_id": self.parent_span_id,
            "application": self.application,
            "feature_id": self.feature_id,
            "environment": self._meter.environment,
            "operation_name": self.operation_name,
        }
        if self._meter.release_version:
            context["release_version"] = self._meter.release_version
        return {k: v for k, v in context.items() if v is not None}


# ---------------------------------------------------------------------------
# Response introspection
# ---------------------------------------------------------------------------
def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _usage_of(resp: Any) -> dict:
    """Tokens, model and provider from a provider response.

    Shapes differ per provider and are all read defensively: a response this
    does not recognise yields no token fields rather than an exception, and the
    span is still recorded with its timing and status.
    """
    out: dict = {}
    if resp is None:
        return out
    try:
        model = _attr(resp, "model", None) or _attr(resp, "model_version", None)
        usage = _attr(resp, "usage", None)
        if usage is None:
            usage = _attr(resp, "usage_metadata", None)
        if usage is None:
            return {"model": model} if model else {}

        # Anthropic
        tin = _int(_attr(usage, "input_tokens", None))
        tout = _int(_attr(usage, "output_tokens", None))
        cache_read = _int(_attr(usage, "cache_read_input_tokens", None))
        cache_write = _int(_attr(usage, "cache_creation_input_tokens", None))
        provider = "anthropic" if (tin or tout or cache_read or cache_write) else None

        if not tin and not tout:  # OpenAI
            tin = _int(_attr(usage, "prompt_tokens", None))
            tout = _int(_attr(usage, "completion_tokens", None))
            details = _attr(usage, "prompt_tokens_details", {}) or {}
            cache_read = _int(_attr(details, "cached_tokens", None))
            out_details = _attr(usage, "completion_tokens_details", {}) or {}
            reasoning = _int(_attr(out_details, "reasoning_tokens", None))
            if reasoning:
                out["reasoning_tokens"] = reasoning
            if tin or tout:
                provider = "openai"

        if not tin and not tout:  # Google
            tin = _int(_attr(usage, "prompt_token_count", None))
            tout = _int(_attr(usage, "candidates_token_count", None))
            cache_read = _int(_attr(usage, "cached_content_token_count", None))
            reasoning = _int(_attr(usage, "thoughts_token_count", None))
            if reasoning:
                out["reasoning_tokens"] = reasoning
            if tin or tout:
                provider = "google"

        if model:
            out["model"] = model
        if provider:
            out["provider"] = provider
        if tin:
            out["tokens_in"] = tin
        if tout:
            out["tokens_out"] = tout
        if cache_read:
            out["cache_read_tokens"] = cache_read
        if cache_write:
            out["cache_write_tokens"] = cache_write
    except Exception:
        return out  # an unfamiliar response is not a reason to lose the span
    return out


_COMPLETION_PATHS = {
    "anthropic": {("messages", "create"), ("completions", "create")},
    "openai": {("chat", "completions", "create"), ("responses", "create"),
               ("embeddings", "create")},
    "google": {("models", "generate_content"), ("generate_content",)},
}


def _detect_provider(client: Any) -> str:
    """Best-effort provider from the client's type.

    Checks the whole MRO, not just the concrete class: a client is often held
    behind a thin wrapper or subclass whose own name says nothing. When nothing
    matches, the OpenAI-compatible shape is the common default — and `wrap(...,
    provider=...)` exists for when that guess is wrong.
    """
    for klass in type(client).__mro__:
        module = (getattr(klass, "__module__", "") or "").lower()
        name = (getattr(klass, "__name__", "") or "").lower()
        for provider in ("anthropic", "openai", "mistral", "cohere", "groq"):
            if provider in module or provider in name:
                return provider
        if "google" in module or "genai" in module or "gemini" in name or "google" in name:
            return "google"
    return "openai"


def _has_usage(resp: Any) -> bool:
    return _attr(resp, "usage", None) is not None or _attr(resp, "usage_metadata", None) is not None


# ---------------------------------------------------------------------------
# Consented prompt capture: reading a call's text, and nothing else
# ---------------------------------------------------------------------------
#: The call shapes whose text can be told apart from tool calls reliably.
_CAPTURE_PATHS = {
    ("anthropic", ("messages", "create")),
    ("openai", ("chat", "completions", "create")),
}


def _capture_url_from(ingest_url: Optional[str]) -> Optional[str]:
    """The capture endpoint beside a standard ingest URL, or None."""
    if not ingest_url:
        return None
    base = ingest_url.rstrip("/")
    if base.endswith("/hook/events"):
        return base[: -len("/hook/events")] + "/prompt-capture/samples"
    return None


def _text_of(content: Any) -> str:
    """The text in a message's content. Only blocks whose type is text are read.

    That is the whole privacy boundary for tool data: a tool_use, tool_result,
    image or document block is never looked into, because nothing here asks
    for anything but a text block's `text`.
    """
    if isinstance(content, str):
        return content
    parts: list = []
    if isinstance(content, (list, tuple)):
        for block in content:
            if _attr(block, "type", None) in ("text", "input_text", "output_text"):
                text = _attr(block, "text", None)
                if isinstance(text, str) and text:
                    parts.append(text)
    return "\n\n".join(parts)


def _read_sample(provider: str, kwargs: dict, resp: Any) -> Optional[dict]:
    """{template, input, output, parameters} for one call, or None.

    The template is the instruction text: Anthropic's `system`, or OpenAI's
    system and developer messages. Tool-role messages are skipped outright. A
    call with no instruction text, or no message text, is not a prompt this can
    optimize and yields None.
    """
    params: dict = {}
    for name in ("temperature", "top_p", "max_tokens"):
        value = kwargs.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            params[name] = value

    messages: list = []
    if provider == "anthropic":
        template = _text_of(kwargs.get("system"))
        for message in kwargs.get("messages") or []:
            role = _attr(message, "role", None)
            if role in ("user", "assistant"):
                text = _text_of(_attr(message, "content", None))
                if text:
                    messages.append({"role": role, "text": text})
        output = _text_of(_attr(resp, "content", None))
    elif provider == "openai":
        limit = kwargs.get("max_completion_tokens")
        if "max_tokens" not in params and isinstance(limit, int) and not isinstance(limit, bool):
            params["max_tokens"] = limit
        fmt = kwargs.get("response_format")
        fmt_type = _attr(fmt, "type", None) if fmt is not None else None
        if isinstance(fmt_type, str):
            params["response_format"] = fmt_type
        instructions: list = []
        for message in kwargs.get("messages") or []:
            role = _attr(message, "role", None)
            if role not in ("system", "developer", "user", "assistant"):
                continue  # tool results, function results: never read
            text = _text_of(_attr(message, "content", None))
            if not text:
                continue
            if role in ("system", "developer"):
                instructions.append(text)
            else:
                messages.append({"role": role, "text": text})
        template = "\n\n".join(instructions)
        choices = _attr(resp, "choices", None) or []
        reply = _attr(choices[0], "message", None) if choices else None
        output = _text_of(_attr(reply, "content", None)) if reply is not None else ""
    else:
        return None

    if not template or not messages:
        return None
    return {"template": template, "input": messages, "output": output, "parameters": params}


class _Wrapped:
    """A transparent proxy that records the completion call and returns the real
    response unchanged. Anything off the instrumented path is handed straight
    back, so a wrapped client behaves exactly like the original."""

    def __init__(self, target: Any, meter: Meter, provider: str, *, path: tuple = (),
                 feature_id: Optional[str] = None, application: Optional[str] = None,
                 prompt_id: Optional[str] = None, prompt_version: Optional[str] = None,
                 customer_id: Optional[str] = None, cache_scope: Optional[str] = None):
        object.__setattr__(self, "_t", target)
        object.__setattr__(self, "_m", meter)
        object.__setattr__(self, "_p", provider)
        object.__setattr__(self, "_path", path)
        object.__setattr__(self, "_feature", feature_id)
        object.__setattr__(self, "_app", application)
        object.__setattr__(self, "_prompt", prompt_id)
        object.__setattr__(self, "_version", prompt_version)
        object.__setattr__(self, "_customer", customer_id)
        object.__setattr__(self, "_cache_scope", cache_scope)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._t, name)
        new_path = self._path + (name,)
        paths = _COMPLETION_PATHS.get(self._p, set())
        if any(p[: len(new_path)] == new_path for p in paths):
            return _Wrapped(attr, self._m, self._p, path=new_path,
                            feature_id=self._feature, application=self._app,
                            prompt_id=self._prompt, prompt_version=self._version,
                            customer_id=self._customer, cache_scope=self._cache_scope)
        return attr

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._t, name, value)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if self._path not in _COMPLETION_PATHS.get(self._p, set()):
            return self._t(*args, **kwargs)
        began = time.perf_counter()
        trace_id, span_id = _new_id(), _new_id()
        operation = ".".join(self._path) or "completion"
        meter = self._m
        try:
            meter._send([
                meter._event("trace.started", trace_id, operation_name=operation,
                             feature_id=self._feature, application=self._app),
                meter._event("span.started", trace_id, span_id=span_id, span_kind="llm",
                             operation_name=operation, feature_id=self._feature,
                             application=self._app),
            ])
        except Exception:
            pass
        try:
            resp = self._t(*args, **kwargs)
        except BaseException:
            latency = int((time.perf_counter() - began) * 1000)
            try:
                meter._send([
                    meter._event("span.failed", trace_id, span_id=span_id, span_kind="llm",
                                 operation_name=operation, latency_ms=latency,
                                 feature_id=self._feature, application=self._app,
                                 prompt_id=self._prompt, prompt_version=self._version),
                    meter._event("trace.failed", trace_id, operation_name=operation,
                                 application=self._app),
                ])
            except Exception:
                pass
            raise
        latency = int((time.perf_counter() - began) * 1000)
        try:
            # A stream or a coroutine has no usage yet; recording it as a
            # completed call would report zero tokens for real spend.
            usage = _usage_of(resp) if _has_usage(resp) else {}
            usage.setdefault("provider", self._p)
            # Optimize mode, when it is on: fold this call's shape into the
            # collector and attach the duplicate signal, if this request repeats,
            # to the span the call was already sending. A hash and a JSON dump
            # against an LLM call's latency; the network stays where it was.
            signal, summaries = None, []
            if meter._optimizer is not None:
                feature = self._feature or meter.feature_id
                signal = meter._optimizer.on_call(
                    usage.get("provider") or self._p,
                    # The response's model when it reported one, the request's
                    # when it did not. v1 used the response alone and fell back
                    # to "", so a streaming call — or any response shape
                    # `_usage_of` does not recognise — made two DIFFERENT models
                    # share one identity.
                    usage.get("model") or _request_model(kwargs),
                    kwargs,
                    usage,
                    feature,
                    scope=_Scope(
                        application=self._app or meter.application,
                        feature_id=feature,
                        operation=operation,
                        environment=meter.environment,
                        customer_id=self._customer,
                        cache_scope=self._cache_scope,
                    ),
                )
                summaries = meter._optimizer.due_summaries()
            meter._send([
                meter._event("span.completed", trace_id, span_id=span_id, span_kind="llm",
                             operation_name=operation, latency_ms=latency,
                             feature_id=self._feature, application=self._app,
                             prompt_id=self._prompt, prompt_version=self._version,
                             signal=signal, **usage),
                meter._event("trace.completed", trace_id, operation_name=operation,
                             application=self._app),
                *summaries,
            ])
        except Exception:
            pass  # metering must never raise into the caller
        try:
            # After metering, and separately: a capture problem can cost a
            # sample, never an event, and never the caller's response.
            meter._offer_sample(self._p, self._path, kwargs, resp,
                                feature_id=self._feature, prompt_id=self._prompt,
                                prompt_version=self._version, usage=usage, latency_ms=latency)
        except Exception:
            pass
        return resp


def wrap(client: Any, meter: Meter, *, feature_id: Optional[str] = None) -> Any:
    """Module-level convenience for `meter.wrap(client)`."""
    return meter.wrap(client, feature_id=feature_id)


# ---------------------------------------------------------------------------
# Process lifetime
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Optimize mode — measured optimization signals
# ---------------------------------------------------------------------------
# Traffic SHAPE only: salted-hash fingerprints and counts. No prompt text, no
# response text, no tool arguments, no metadata — nothing here reads a message's
# content except to hash it, and the hash is salted with a secret the database
# does not contain.
#
# Two signals, both of which the server has always known how to store:
#   'duplicate' — this exact request was seen before, so one of the two calls
#                 was avoidable. Rides on the span the call already sends.
#   'prefix'    — many calls share a large static head that is not being cached.
#                 A bounded counter map, flushed on a timer as its own span.
# ---------------------------------------------------------------------------
# Canonical request identity (v2)
# ---------------------------------------------------------------------------
# What makes two model calls "the same call". Getting this wrong in either
# direction is expensive: too loose and Meter tells a customer to cache
# responses that legitimately differ; too strict and it finds nothing.
#
# v1 hashed the provider, the model and the messages, and nothing else. So a
# call differing only in `temperature`, `system`, `tools`, `max_tokens`,
# `response_format`, `seed` or `stop` produced an identical fingerprint and was
# reported as an avoidable repeat. All seven of those change the output.
#
# THE RULE IS AN EXCLUSION, NOT AN ALLOWLIST. Every serializable field of the
# request is part of its identity except a short list of transport and
# credential settings that provably cannot reach the model. A field nobody here
# has heard of is therefore included rather than ignored — a new provider
# parameter changes the fingerprint on the day it ships, instead of silently
# collapsing two different calls into a match.
#
# ANYTHING UNREADABLE SKIPS THE CANDIDATE. A value this cannot serialize —
# a file handle, an SDK sentinel, a cyclic structure, a payload past the size
# bound — raises, and the caller emits no signal for that call. There is no
# truncation and no `str()` fallback: both turn "I could not read this" into
# "these two calls are identical", which is the one answer that must never be
# guessed.
#
# NOTHING HERE IS EVER TRANSMITTED. The canonical form is hashed with the
# tenant's salt and discarded; the digest is what leaves the process.

#: Bumped when the canonical form changes. It is hashed in, so identities from
#: different versions cannot collide, and it rides on the signal so the server
#: can tell a v1 row (messages only) from a v2 one (the whole request).
CANON_VERSION = "v2"

#: Settings that reach the transport and not the model. Everything else in a
#: request body is treated as output-affecting. Credentials are here because a
#: fingerprint must not be derived from one, and because rotating a key would
#: otherwise change every identity the process has.
_TRANSPORT_FIELDS = frozenset({
    "api_key", "auth", "authorization", "headers", "extra_headers", "extra_query",
    "timeout", "request_timeout", "max_retries", "http_client", "client",
    "default_headers", "organization", "project_id", "base_url", "user_agent",
})

#: Bounds on traversal. A request past any of them is unreadable rather than
#: truncated: a truncated payload compares equal to every other payload with the
#: same first N bytes, which is a false match by construction.
_MAX_DEPTH = 40
_MAX_NODES = 20_000
_MAX_CANON_BYTES = 1_000_000


class UnsupportedRequest(Exception):
    """This request cannot be read safely, so it is not a duplicate candidate."""


def _canonical(value, depth: int, state: dict):
    """A JSON-safe copy of `value`, or raise. Cycles and bounds checked here."""
    state["nodes"] += 1
    if depth > _MAX_DEPTH or state["nodes"] > _MAX_NODES:
        raise UnsupportedRequest("request is too deeply nested or too large to compare")

    if value is None or isinstance(value, str):
        return value
    # bool before int: bool IS an int in Python, and True must not become 1.
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise UnsupportedRequest("request contains a non-finite number")
        # 1.0 and 1 are the same argument to every provider, and JavaScript
        # cannot tell them apart at all. Normalising here is what lets the two
        # SDKs agree byte for byte.
        if value.is_integer() and abs(value) < 1e15:
            return int(value)
        return value

    if isinstance(value, (list, tuple)):
        marker = id(value)
        if marker in state["seen"]:
            raise UnsupportedRequest("request contains a cycle")
        state["seen"].add(marker)
        try:
            return [_canonical(item, depth + 1, state) for item in value]
        finally:
            state["seen"].discard(marker)

    if isinstance(value, dict):
        marker = id(value)
        if marker in state["seen"]:
            raise UnsupportedRequest("request contains a cycle")
        state["seen"].add(marker)
        try:
            out = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise UnsupportedRequest("request has a non-string key")
                out[key] = _canonical(item, depth + 1, state)
            return out
        finally:
            state["seen"].discard(marker)

    # Provider SDKs hand back model objects rather than dicts. Two conversions
    # are supported explicitly, because both are documented, lossless and
    # widely used; anything else is unreadable rather than guessed at.
    dump = getattr(value, "model_dump", None)  # pydantic v2
    if callable(dump):
        try:
            return _canonical(dump(mode="json"), depth + 1, state)
        except UnsupportedRequest:
            raise
        except Exception as exc:
            raise UnsupportedRequest("request contains an object that cannot be read") from exc
    as_dict = getattr(value, "to_dict", None)  # google-genai and friends
    if callable(as_dict):
        try:
            return _canonical(as_dict(), depth + 1, state)
        except UnsupportedRequest:
            raise
        except Exception as exc:
            raise UnsupportedRequest("request contains an object that cannot be read") from exc

    raise UnsupportedRequest(f"request contains an unreadable {type(value).__name__}")


def canonical_request(request: dict) -> str:
    """The request's identity as a string, or raise `UnsupportedRequest`.

    Keys are sorted, message order and text are kept exactly, and the output is
    byte-identical to the Node SDK's for the same request — `separators` and
    `ensure_ascii` are both set for that reason, not for looks.
    """
    if not isinstance(request, dict):
        raise UnsupportedRequest("request is not a mapping")
    body = {k: v for k, v in request.items() if k not in _TRANSPORT_FIELDS}
    if not body:
        raise UnsupportedRequest("request has no comparable fields")
    state = {"nodes": 0, "seen": set()}
    canonical = _canonical(body, 0, state)
    try:
        text = json.dumps(
            canonical,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise UnsupportedRequest("request could not be serialized") from exc
    if len(text.encode("utf-8")) > _MAX_CANON_BYTES:
        raise UnsupportedRequest("request is too large to compare")
    return text


class _Scope(NamedTuple):
    """The boundary inside which two identical requests could be interchangeable.

    Not a label on the finding — part of its identity. Two customers sending the
    same prompt are not one avoidable call, and neither are two environments or
    two features that happen to share wording. All of it is hashed with the
    tenant's salt, so no raw application, feature or customer name is ever in a
    fingerprint that leaves the process.
    """

    application: Optional[str] = None
    feature_id: Optional[str] = None
    operation: Optional[str] = None
    environment: Optional[str] = None
    customer_id: Optional[str] = None
    cache_scope: Optional[str] = None

    def key(self) -> str:
        return "\x1e".join(
            f"{name}={value or ''}"
            for name, value in zip(self._fields, self)
        )

    @property
    def explicit(self) -> bool:
        """Whether the caller stated a boundary a response could be reused in.

        Without one, a repeat is still a repeat — but nobody has said the two
        calls belong to the same user, tenant or cache, so Meter must not imply
        the second could have served the first. Absent scope is reported as
        absent, never as permission.
        """
        return bool(self.customer_id or self.cache_scope)


def _request_model(request: dict) -> str:
    """The model the caller asked for, when the response did not say."""
    if isinstance(request, dict):
        model = request.get("model")
        if isinstance(model, str):
            return model
    return ""


def _sha(*parts: str) -> str:
    h = hashlib.sha256()
    h.update("\x1f".join(parts).encode("utf-8"))
    return h.hexdigest()


def _normalize(request: dict) -> str:
    """A stable string for the parts of a request that make two calls identical."""
    if not isinstance(request, dict):
        return ""
    payload = request.get("messages")
    if payload is None:
        payload = request.get("input")  # OpenAI Responses API
    if payload is None:
        payload = request.get("contents")  # Google generate_content
    if payload is None:
        # Not a call shape this knows how to compare. "" rather than "null",
        # which is what json.dumps would give and which every unreadable call
        # would then share.
        return ""
    try:
        return json.dumps(payload, sort_keys=True, default=str)
    except Exception:
        return str(payload)


def _system_of(request: dict):
    """The system instruction, wherever this particular API keeps it.

    Anthropic and Gemini put it at the top level under different names. OpenAI
    has no top-level field at all: it is the leading run of system/developer
    turns in `messages`.
    """
    for key in ("system", "system_instruction", "systemInstruction"):
        value = request.get(key)
        if value is not None:
            return value
    messages = request.get("messages")
    if not isinstance(messages, list):
        return None
    leading = []
    for message in messages:
        # The LEADING run only. A system turn further down is not part of a
        # prefix, and a cache is a prefix.
        if not isinstance(message, dict) or message.get("role") not in ("system", "developer"):
            break
        leading.append(message)
    return leading or None


def _static_prefix(request: dict, prefix_chars: int) -> tuple:
    """The cacheable static head of a request, and an estimate of its size.

    Prefers the explicitly static blocks — the system instruction and the tool
    definitions — and otherwise falls back to the leading slice of the request.
    The token count is characters over four, which is an estimate and is
    reported as one: `on_call` replaces it with the provider's own figure
    wherever the provider reports it.

    The system instruction used to be read only from `request["system"]`, which
    is Anthropic's spelling. On OpenAI that found nothing, and when `tools` was
    present the first branch still fired — so the prefix became the tool
    definitions ALONE, with the system prompt (usually the larger block) left
    out of both the estimated size and the fingerprint. Every OpenAI call
    sharing a toolset hashed alike however different its instructions were.
    """
    static = ""
    if isinstance(request, dict):
        system = _system_of(request)
        tools = request.get("tools")
        if system is not None or tools is not None:
            try:
                static = json.dumps([system, tools], sort_keys=True, default=str)
            except Exception:
                static = f"{system}{tools}"
    if not static:
        static = _normalize(request)[:prefix_chars]
    return static, max(0, len(static) // 4)


class _Optimizer:
    """Bounded, thread-safe signal collection for one meter.

    Both structures have a hard ceiling, because a map keyed by request shape
    grows with traffic: the duplicate set is an LRU and the prefix map flushes
    at capacity as well as on its timer.
    """

    def __init__(
        self,
        meter: Meter,
        *,
        prefix_chars: int = PREFIX_CHARS,
        flush_interval: float = OPTIMIZE_FLUSH_INTERVAL,
        dup_capacity: int = DUP_CAPACITY,
        prefix_capacity: int = PREFIX_CAPACITY,
        window: float = DUPLICATE_WINDOW,
        cache_window: float = CACHE_WINDOW,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._m = meter
        self._prefix_chars = int(prefix_chars)
        self._flush_interval = float(flush_interval)
        self._dup_capacity = int(dup_capacity)
        self._prefix_capacity = int(prefix_capacity)
        self._window = float(window)
        self._cache_window = float(cache_window)
        # Injected so a test can move time without patching the stdlib clock,
        # which `mock.patch` would do process-wide — the delivery threads read
        # `time.monotonic` too, and handing them a frozen clock makes flushes
        # hang or fire at random depending on scheduling.
        self._clock = clock
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._prefixes: dict = {}
        # Last time each prefix was seen, kept OUTSIDE self._prefixes because it
        # must survive the flush that empties it. A window count restarted every
        # flush interval would report one write per minute of traffic and call
        # caching a loss.
        self._prefix_last: OrderedDict = OrderedDict()
        self._last_flush = time.monotonic()
        self._lock = threading.Lock()

    def on_call(self, provider: str, model: str, request: dict, usage: dict,
                feature_id: Optional[str] = None,
                scope: Optional[_Scope] = None) -> Optional[dict]:
        """Fold one call in; return a 'duplicate' signal if this request repeats.

        `usage` is what the response reported. Two fields matter here: a cache
        READ says this call was already served from cache and must not be
        counted as a caching opportunity, and a cache WRITE is the provider's
        own measurement of the static prefix — the only place a real token count
        for it can come from.
        """
        salt = self._m.salt()
        if not salt:
            return None  # never emit an unsalted hash
        model = model or ""
        scope = scope or _Scope()
        # The whole request, not just its messages, and scoped to the boundary a
        # response could actually be reused in. Unreadable requests raise rather
        # than degrade: a truncated or stringified payload compares equal to
        # things it is not.
        try:
            canonical = canonical_request(request)
        except UnsupportedRequest:
            request_fp = None
        else:
            request_fp = _sha(
                salt, CANON_VERSION, scope.key(), provider, model, canonical
            )
        # Prefix detection keeps its own, unchanged normalisation. The two
        # questions are different — "is this the same call" versus "do these
        # calls share a head" — and one must not move when the other does.
        static, estimated = _static_prefix(request, self._prefix_chars)
        prefix_fp = _sha(salt, provider, model, static)
        feature = feature_id or self._m.feature_id
        measured = int(usage.get("cache_write_tokens") or 0)
        cache_read = int(usage.get("cache_read_tokens") or 0)
        now = self._clock()  # never the wall clock: it can move backwards
        with self._lock:
            duplicate = False
            if request_fp is not None:
                opened = self._seen.get(request_fp)
                if opened is not None and (now - opened) <= self._window:
                    duplicate = True
                    # Recency for eviction only. The group's start time is NOT
                    # refreshed, so steady repeating traffic cannot keep one
                    # supposed cached response alive indefinitely.
                    self._seen.move_to_end(request_fp)
                else:
                    # First sighting, or the window has closed and this opens a
                    # new group — an expired repeat is not an avoidable call.
                    self._seen[request_fp] = now
                    self._seen.move_to_end(request_fp)
                while len(self._seen) > self._dup_capacity:
                    self._seen.popitem(last=False)
            entry = self._prefixes.setdefault(
                (prefix_fp, feature),
                {"provider": provider, "model": model, "feature_id": feature,
                 "count": 0, "cached": 0, "writes": 0, "windows": 0,
                 "tin": 0, "tout": 0, "estimated": 0,
                 "measured_sum": 0, "measured_n": 0},
            )
            entry["count"] += 1
            entry["tin"] += int(usage.get("tokens_in") or 0)
            entry["tout"] += int(usage.get("tokens_out") or 0)
            # A no-op fold, kept explicit: the fingerprint IS the static block,
            # so every call in this group has the same character estimate.
            entry["estimated"] = max(entry["estimated"], estimated)
            if measured:
                # Summed and counted, not maximised. What the provider actually
                # caches varies between calls sharing a static block — the
                # breakpoint moves, and the conversation in front of it grows —
                # so the largest figure of the month is the group's most
                # expensive member, not its typical one. A mean also survives
                # being folded again server-side; a max of means does not.
                entry["measured_sum"] += measured
                entry["measured_n"] += 1
            if cache_read:
                entry["cached"] += 1
            if measured:
                # The provider says it WROTE this prefix, so caching is already
                # on here and this call is the unavoidable cost of keeping it
                # warm — not an opportunity to enable something.
                entry["writes"] += 1
            # How many times a cache would have had to be written if one were
            # in use: once per gap longer than the provider's TTL. Counted per
            # process, and a provider's cache is account-wide, so replicas each
            # count a window the account only paid for once — an OVERCOUNT of
            # writes, which understates the saving rather than inflating it.
            last = self._prefix_last.get(prefix_fp)
            # >=, not >: at exactly the TTL the entry is on the boundary, and
            # assuming it survived would be assuming a saving. Assume the write.
            if last is None or (now - last) >= self._cache_window:
                entry["windows"] += 1
            self._prefix_last[prefix_fp] = now
            self._prefix_last.move_to_end(prefix_fp)
            while len(self._prefix_last) > self._prefix_capacity:
                self._prefix_last.popitem(last=False)
        if duplicate:
            return {
                "kind": "duplicate",
                "fingerprint": request_fp,
                "count": 1,
                "fingerprint_version": CANON_VERSION,
                # Whether anybody said these two calls belong to the same user,
                # tenant or cache. The server will not present an unscoped
                # repeat as a safe reuse.
                "scope_kind": "explicit" if scope.explicit else "unscoped",
            }
        return None

    def due_summaries(self, force: bool = False) -> list:
        """Prefix counters as spans, when the timer elapses or the map fills up.

        Each summary is a completed span of its own. The server does not cost it
        — the calls it summarises were each metered when they happened — it only
        folds the counts into the tenant's signal store.

        `force` empties the map whatever the timer says, and `Meter.flush()`
        passes it. Without that, a process that does not outlive the interval —
        a script, a batch job, one serverless invocation — takes every prefix
        counter it gathered to the grave, which is most of the traffic this
        feature exists to measure.
        """
        now = time.monotonic()
        with self._lock:
            if (not force
                    and now - self._last_flush < self._flush_interval
                    and len(self._prefixes) < self._prefix_capacity):
                return []
            self._last_flush = now
            items, self._prefixes = self._prefixes, {}
        events = []
        for (fingerprint, _feature), entry in items.items():
            # The estimate and the measurements travel in SEPARATE fields. They
            # used to share one, folded server-side with GREATEST, which cannot
            # tell them apart: a process that never saw a cache creation sent
            # its character count, and if that was the larger number it won and
            # was then labelled as the provider's own.
            measured = entry["measured_n"] > 0
            events.append(
                self._m._event(
                    "span.completed", _new_id(), span_id=_new_id(), span_kind="llm",
                    operation_name="optimize.prefix", provider=entry["provider"],
                    model=entry["model"] or None, feature_id=entry["feature_id"],
                    signal={
                        "kind": "prefix",
                        "fingerprint": fingerprint,
                        "count": entry["count"],
                        "cached_count": entry["cached"],
                        "tokens_in": entry["tin"],
                        "tokens_out": entry["tout"],
                        # Always the estimate now, whatever else was seen.
                        "prefix_tokens": entry["estimated"],
                        "prefix_measured": measured,
                        # The provider's own counts, summed and counted so the
                        # server can hold a mean across every process reporting
                        # this prefix.
                        "prefix_tokens_sum": entry["measured_sum"],
                        "prefix_tokens_n": entry["measured_n"],
                        # What caching costs, not just what it saves: calls the
                        # provider already wrote to cache, and the number of
                        # writes enabling it would need.
                        "write_calls": entry["writes"],
                        "cache_windows": entry["windows"],
                    },
                )
            )
        return events


def _spawn(work) -> Optional[threading.Thread]:
    """A daemon worker, or None if threads are unavailable.

    Daemon so metering can never hold a process open; atexit below is what
    gives queued events a last chance to leave.
    """
    try:
        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        return thread
    except Exception:
        return None


def _flush_all_meters() -> None:
    for meter in list(_METERS):
        try:
            meter.flush()
        except Exception:
            pass


atexit.register(_flush_all_meters)

__all__ = ["Meter", "AgentRun", "wrap", "__version__"]
