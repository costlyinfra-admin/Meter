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

**What is never sent.** Prompts, responses, messages, tool arguments, tool
results, retrieved documents, exception messages and stack traces. Not
truncated, not hashed unless you pass a `prompt_hash` yourself, not behind a
setting — the events this SDK can construct have no field for them, and the
server refuses a payload that carries one. What travels is identity, counts,
timing and money: which prompt version, how many tokens, how long, how much.

**It must never break your agent.** Every path here is wrapped: a Meter failure
degrades to sending nothing. Delivery happens on a background worker off your
request path, over a bounded queue that sheds oldest-first rather than growing,
and `meter.dropped` tells you if it ever came to that.
"""

from __future__ import annotations

import atexit
import datetime as dt
import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional

__version__ = "2.0.0"

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

_METERS: "set" = set()

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
        _METERS.add(self)

    @property
    def enabled(self) -> bool:
        return bool(self.ingest_url and self.token)

    # -- the two entry points ---------------------------------------------
    def wrap(self, client: Any, *, feature_id: Optional[str] = None,
             application: Optional[str] = None, provider: Optional[str] = None) -> Any:
        """Instrument a provider client so each call becomes its own trace.

        For the common case — one model call, no workflow around it — explicit
        trace management would be ceremony with no payoff. A wrapped call opens
        a trace, records one LLM span, and closes it.

        `provider` is inferred from the client's type, which works for the
        official SDKs and fails quietly for a wrapper, a subclass or a test
        double — so it can be stated outright. Passing it wrong instruments
        nothing rather than instrumenting the wrong thing.
        """
        return _Wrapped(client, self, provider or _detect_provider(client),
                        feature_id=feature_id or self.feature_id,
                        application=application or self.application)

    @contextmanager
    def agent(self, operation_name: str, *, feature_id: Optional[str] = None,
              customer_id: Optional[str] = None, application: Optional[str] = None,
              trace_id: Optional[str] = None,
              parent_span_id: Optional[str] = None) -> Iterator["AgentRun"]:
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
    def resume(self, context: Any, **overrides: Any) -> Iterator["AgentRun"]:
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
                while not self._queue:
                    self._cv.wait()
                deadline = time.monotonic() + self._flush_interval
                while len(self._queue) < self._batch_size and not self._flush_now:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._cv.wait(remaining)
                batch = [
                    self._queue.popleft() for _ in range(min(len(self._queue), self._batch_size))
                ]
                self._sending = True
            try:
                self._deliver(batch)
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
        try:
            deadline = time.monotonic() + max(0.0, timeout)
            with self._cv:
                if not self._queue and not self._sending:
                    return True
                self._reset_after_fork_locked()
                self._ensure_worker_locked()
                if self._worker is None:
                    return False
                self._flush_now = True
                self._cv.notify_all()
                while self._queue or self._sending:
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


class _Wrapped:
    """A transparent proxy that records the completion call and returns the real
    response unchanged. Anything off the instrumented path is handed straight
    back, so a wrapped client behaves exactly like the original."""

    def __init__(self, target: Any, meter: Meter, provider: str, *, path: tuple = (),
                 feature_id: Optional[str] = None, application: Optional[str] = None):
        object.__setattr__(self, "_t", target)
        object.__setattr__(self, "_m", meter)
        object.__setattr__(self, "_p", provider)
        object.__setattr__(self, "_path", path)
        object.__setattr__(self, "_feature", feature_id)
        object.__setattr__(self, "_app", application)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._t, name)
        new_path = self._path + (name,)
        paths = _COMPLETION_PATHS.get(self._p, set())
        if any(p[: len(new_path)] == new_path for p in paths):
            return _Wrapped(attr, self._m, self._p, path=new_path,
                            feature_id=self._feature, application=self._app)
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
                                 feature_id=self._feature, application=self._app),
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
            meter._send([
                meter._event("span.completed", trace_id, span_id=span_id, span_kind="llm",
                             operation_name=operation, latency_ms=latency,
                             feature_id=self._feature, application=self._app, **usage),
                meter._event("trace.completed", trace_id, operation_name=operation,
                             application=self._app),
            ])
        except Exception:
            pass  # metering must never raise into the caller
        return resp


def wrap(client: Any, meter: Meter, *, feature_id: Optional[str] = None) -> Any:
    """Module-level convenience for `meter.wrap(client)`."""
    return meter.wrap(client, feature_id=feature_id)


# ---------------------------------------------------------------------------
# Process lifetime
# ---------------------------------------------------------------------------
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
