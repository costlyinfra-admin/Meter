"""Request-level ingestion: agent runs, their steps, and what each one cost.

The hook (hook.py) turns model calls into monthly totals. It answers "what did
this feature cost". It cannot answer "why did THIS run cost $1.42", because by
the time a row is written the workflow that produced it is gone.

This module keeps the workflow. An SDK sends lifecycle events — a trace starts,
spans start and finish, the trace ends — and each one is folded into `ai_trace`
and `ai_span`. Finalized LLM spans are then priced with the SAME pricing call
the hook uses and folded into the same financial tables, so request-level
evidence and monthly totals are two views of one number rather than two numbers.

Three properties this file exists to guarantee:

**Content never arrives.** Events are read through an allowlist: a field not
named in `_SPAN_FIELDS`/`_TRACE_FIELDS` is dropped before it can reach a query.
Fields that would carry prompt or response content are rejected LOUDLY rather
than silently discarded — a customer who instruments content capture should be
told it is refused, not left believing it worked.

**Money is added exactly once.** `ai_span.costed_at` is claimed with a
conditional UPDATE. Whichever transaction wins the claim does the financial
work; a replayed batch, a duplicated delivery or a retried completion loses the
claim and adds nothing. That is what makes replay safe without a dedupe table.

**Events arrive in any order.** A span completion can precede its start, a child
can precede its parent, a completion can follow a trace being considered stale.
Every write is an upsert over the client-generated id, and terminal states are
sticky: nothing reopens a finished trace.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from decimal import Decimal
from typing import Optional

from . import applications, compute
from .db import app_dsn, connect, tenant_tx
from .pricing import PRICED_PROVIDERS, price

# ---------------------------------------------------------------------------
# Limits. Every one of these is a bound on what an ingest token can make the
# server allocate; none is a stylistic preference.
# ---------------------------------------------------------------------------
MAX_EVENTS_PER_BATCH = 500
MAX_ID = 128
MAX_NAME = 200
MAX_SHORT = 120
MAX_TINY = 60
#: A token count above this is a client bug, not a very large call. Bounded so
#: one bad event cannot poison a monthly total with an absurd number.
MAX_TOKENS = 1_000_000_000
#: How far an event may sit outside now before its timestamp is clamped.
#:
#: Asymmetric on purpose. An event dated in the FUTURE is always a wrong clock,
#: so that window is tight. An event dated in the past is routinely legitimate:
#: a queue backlog, a worker resuming after an outage, an SDK flushing on a
#: process that was suspended. Rejecting those into the current month would
#: silently move real spend between billing periods, which is worse than the
#: skew it guards against.
MAX_CLOCK_SKEW_FUTURE = dt.timedelta(days=1)
MAX_CLOCK_SKEW_PAST = dt.timedelta(days=95)

TRACE_EVENTS = (
    "trace.started",
    "trace.heartbeat",
    "trace.completed",
    "trace.failed",
    "trace.cancelled",
)
SPAN_EVENTS = ("span.started", "span.completed", "span.failed", "span.cancelled")
EVENT_TYPES = TRACE_EVENTS + SPAN_EVENTS

SPAN_KINDS = ("workflow", "llm", "embedding", "retrieval", "tool", "guardrail", "evaluation")
TERMINAL = {
    "trace.completed": "success",
    "trace.failed": "error",
    "trace.cancelled": "cancelled",
    "span.completed": "success",
    "span.failed": "error",
    "span.cancelled": "cancelled",
}

#: Field names that would carry prompt or response content. Their PRESENCE is an
#: error, not something to quietly drop: an SDK sending these is misconfigured
#: and its author needs to know before they ship it.
FORBIDDEN_FIELDS = frozenset(
    {
        "prompt",
        "prompt_text",
        "prompts",
        "messages",
        "message",
        "input",
        "inputs",
        "response",
        "response_text",
        "output",
        "outputs",
        "completion",
        "content",
        "text",
        "tool_args",
        "tool_arguments",
        "arguments",
        "tool_result",
        "tool_results",
        "result",
        "documents",
        "retrieved_documents",
        "context",
        "chunks",
        "embedding_input",
        "error_message",
        "exception",
        "stacktrace",
        "stack_trace",
        "traceback",
        "metadata",
    }
)


class TraceError(ValueError):
    """A malformed or refused event batch (maps to HTTP 400)."""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _text(value, limit: int, field: str, *, required: bool = False) -> Optional[str]:
    if value is None:
        if required:
            raise TraceError(f"{field} is required.")
        return None
    if not isinstance(value, (str, int, float)):
        raise TraceError(f"{field} must be text.")
    text = str(value).strip()
    if not text:
        if required:
            raise TraceError(f"{field} is required.")
        return None
    if len(text) > limit:
        raise TraceError(f"{field} must be {limit} characters or fewer.")
    return text


def _count(value, field: str) -> Optional[int]:
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise TraceError(f"{field} must be a whole number.") from exc
    if number < 0:
        raise TraceError(f"{field} cannot be negative.")
    if number > MAX_TOKENS:
        raise TraceError(f"{field} is implausibly large.")
    return number


def _when(value, now: dt.datetime) -> dt.datetime:
    """An event timestamp, clamped to a believable window around now.

    A client with a wrong clock would otherwise write this month's spend into
    2019 — invisible in every report and impossible to reconcile. Clamping keeps
    the cost in a period someone will actually look at.
    """
    if value is None:
        return now
    parsed = None
    if isinstance(value, str):
        raw = value.strip().replace("Z", "+00:00")
        try:
            parsed = dt.datetime.fromisoformat(raw)
        except ValueError:
            parsed = None
    elif isinstance(value, (int, float)):
        try:
            parsed = dt.datetime.fromtimestamp(float(value), dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            parsed = None
    if parsed is None:
        return now
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    if parsed > now + MAX_CLOCK_SKEW_FUTURE:
        return now
    if parsed < now - MAX_CLOCK_SKEW_PAST:
        return now - MAX_CLOCK_SKEW_PAST
    return parsed


def validate(event: dict, now: dt.datetime) -> dict:
    """One event, reduced to the fields this system is allowed to store.

    Everything not named here is dropped. That is the allowlist that makes the
    privacy guarantee structural rather than a promise: an unknown key cannot
    reach a column because nothing ever reads it.
    """
    if not isinstance(event, dict):
        raise TraceError("Each event must be an object.")
    present = FORBIDDEN_FIELDS.intersection(event.keys())
    if present:
        raise TraceError(
            "Meter does not accept prompt or response content. Remove "
            + ", ".join(sorted(present))
            + " from the event. Only tokens, cost, timing and prompt identity are stored."
        )
    kind = event.get("event_type")
    if kind not in EVENT_TYPES:
        raise TraceError(f"event_type must be one of: {', '.join(EVENT_TYPES)}.")

    out: dict = {
        "event_type": kind,
        "event_id": _text(event.get("event_id"), MAX_ID, "event_id"),
        "trace_id": _text(event.get("trace_id"), MAX_ID, "trace_id", required=True),
        "application": _text(event.get("application"), applications.MAX_SLUG, "application"),
        "operation_name": _text(event.get("operation_name"), MAX_NAME, "operation_name"),
        "feature_id": _text(event.get("feature_id"), MAX_ID, "feature_id"),
        "environment": _text(event.get("environment"), MAX_TINY, "environment") or "production",
        "release_version": _text(event.get("release_version"), MAX_SHORT, "release_version"),
        "customer_id": _text(event.get("customer_id"), MAX_NAME, "customer_id"),
        "occurred_at": _when(event.get("occurred_at"), now),
    }

    if kind in SPAN_EVENTS:
        out["span_id"] = _text(event.get("span_id"), MAX_ID, "span_id", required=True)
        out["parent_span_id"] = _text(event.get("parent_span_id"), MAX_ID, "parent_span_id")
        span_kind = _text(event.get("span_kind"), MAX_TINY, "span_kind") or "llm"
        if span_kind not in SPAN_KINDS:
            raise TraceError(f"span_kind must be one of: {', '.join(SPAN_KINDS)}.")
        out["span_kind"] = span_kind
        out["provider"] = _text(event.get("provider"), MAX_TINY, "provider")
        out["model"] = _text(event.get("model"), MAX_SHORT, "model")
        for field in (
            "tokens_in",
            "tokens_out",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
            "latency_ms",
        ):
            out[field] = _count(event.get(field), field)
        out["prompt_id"] = _text(event.get("prompt_id"), MAX_NAME, "prompt_id")
        out["prompt_version"] = _text(event.get("prompt_version"), MAX_TINY, "prompt_version")
        out["prompt_hash"] = _text(event.get("prompt_hash"), 128, "prompt_hash")
        out["signal"] = _signal(event.get("signal"))
    return out


def _signal(raw) -> Optional[dict]:
    """An optimization signal: a salted fingerprint and counts, never text.

    Kept in the contract deliberately. It is the same privacy class as
    `prompt_hash` — a one-way fingerprint the customer computed — and it is what
    the duplicate/uncached-prefix detectors are built on. Dropping it would have
    quietly removed a working feature.

    Validated rather than passed through: a fingerprint reaches an aggregation
    key, and an unbounded one is an unbounded row.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise TraceError("signal must be an object.")
    kind = _text(raw.get("kind"), MAX_TINY, "signal.kind")
    if kind not in ("duplicate", "prefix"):
        raise TraceError("signal.kind must be 'duplicate' or 'prefix'.")
    fingerprint = _text(raw.get("fingerprint"), 128, "signal.fingerprint")
    if not fingerprint:
        return None  # unusable without one; not worth failing the batch over
    out = {"kind": kind, "fingerprint": fingerprint}
    for field in ("count", "tokens_in", "tokens_out", "cached_tokens", "prefix_tokens"):
        value = _count(raw.get(field), f"signal.{field}")
        if value is not None:
            out[field] = value
    return out


def salted_prompt_hash(raw: Optional[str], salt: str) -> Optional[str]:
    """A prompt identity that cannot be walked back to a prompt.

    A client-side hash of a short, guessable prompt is not private: anyone with
    the database can hash likely prompts until one matches. Re-hashing with the
    tenant's own salt makes that attack need a secret the database does not
    contain, and still compares equal for equal prompts within the tenant, which
    is the only comparison this feature needs.
    """
    if not raw:
        return None
    return hashlib.sha256(f"{salt}:{raw}".encode()).hexdigest()[:64]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def _period_of(when: dt.datetime) -> dt.date:
    return when.date().replace(day=1)


def _upsert_trace(conn, tenant_id, app_id, feature_id, ev, now) -> tuple[str, str]:
    """Create or touch the trace this event belongs to. Returns (id, status).

    Every event touches the trace, which is what keeps a busy agent alive
    without requiring it to heartbeat. A terminal status is sticky: the ON
    CONFLICT clause only ever moves a trace out of `running`.
    """
    # Only a trace.* event names the run. A span event names a STEP, and calling
    # a whole workflow "classify" because that step arrived first would be wrong
    # in the listing, the detail header, and every alert that quotes it. Until
    # the real name arrives the trace stands under its own id.
    trace_name = (ev.get("operation_name") if ev["event_type"] in TRACE_EVENTS else None) or ev[
        "trace_id"
    ]
    row = conn.execute(
        """
        INSERT INTO ai_trace
            (tenant_id, application_id, feature_id, external_trace_id, operation_name,
             customer_ref, environment, release_version, started_at, last_activity_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (tenant_id, external_trace_id) DO UPDATE SET
            -- Never regress to a placeholder: a span arriving before its
            -- trace.started names the trace after its own operation, and the
            -- real name must win when it turns up.
            operation_name  = CASE WHEN ai_trace.operation_name = ai_trace.external_trace_id
                                   THEN EXCLUDED.operation_name ELSE ai_trace.operation_name END,
            feature_id      = COALESCE(ai_trace.feature_id, EXCLUDED.feature_id),
            customer_ref    = COALESCE(ai_trace.customer_ref, EXCLUDED.customer_ref),
            release_version = COALESCE(ai_trace.release_version, EXCLUDED.release_version),
            -- The earliest evidence wins, so an out-of-order start still fixes
            -- the beginning of the run.
            started_at      = LEAST(ai_trace.started_at, EXCLUDED.started_at),
            last_activity_at = GREATEST(ai_trace.last_activity_at, EXCLUDED.last_activity_at),
            updated_at      = now()
        RETURNING id, status
        """,
        (
            tenant_id,
            app_id,
            feature_id,
            ev["trace_id"],
            trace_name,
            ev.get("customer_id"),
            ev["environment"],
            ev.get("release_version"),
            ev["occurred_at"],
            now,
        ),
    ).fetchone()
    return str(row[0]), row[1]


def _finalize_trace(conn, trace_id: str, status: str, when: dt.datetime) -> None:
    """Close a trace. Only ever moves it out of `running`.

    A completion that arrives after the UI had called the trace stale still
    lands here, because stale was never written down — the trace was `running`
    the whole time.
    """
    conn.execute(
        """
        UPDATE ai_trace
        SET status = %s,
            ended_at = COALESCE(ended_at, %s),
            duration_ms = COALESCE(duration_ms,
                GREATEST(0, (EXTRACT(EPOCH FROM (%s - started_at)) * 1000)::bigint)),
            last_activity_at = GREATEST(last_activity_at, %s),
            current_span_id = NULL,
            updated_at = now()
        WHERE id = %s AND status = 'running'
        """,
        (status, when, when, when, trace_id),
    )


def _touch_heartbeat(conn, trace_id: str, when: dt.datetime) -> None:
    conn.execute(
        """
        UPDATE ai_trace
        SET last_heartbeat_at = GREATEST(COALESCE(last_heartbeat_at, %s), %s),
            last_activity_at = GREATEST(last_activity_at, %s),
            updated_at = now()
        WHERE id = %s AND status = 'running'
        """,
        (when, when, when, trace_id),
    )


def _upsert_span(conn, tenant_id, trace_id, ev, status, salt) -> tuple[str, bool]:
    """Create or update a span. Returns (id, created).

    `created` drives span_count, so a duplicated event cannot inflate the step
    count of a workflow.
    """
    terminal = status != "running"
    row = conn.execute(
        """
        INSERT INTO ai_span
            (tenant_id, trace_id, external_span_id, parent_span_id, span_kind,
             operation_name, provider, model, tokens_in, tokens_out, cache_read_tokens,
             cache_write_tokens, reasoning_tokens, latency_ms, status,
             prompt_id, prompt_version, prompt_hash, started_at, ended_at, occurred_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s)
        ON CONFLICT (trace_id, external_span_id) DO UPDATE SET
            parent_span_id = COALESCE(ai_span.parent_span_id, EXCLUDED.parent_span_id),
            span_kind      = CASE WHEN %s THEN EXCLUDED.span_kind ELSE ai_span.span_kind END,
            operation_name = CASE WHEN %s THEN EXCLUDED.operation_name
                                  ELSE ai_span.operation_name END,
            provider       = COALESCE(EXCLUDED.provider, ai_span.provider),
            model          = COALESCE(EXCLUDED.model, ai_span.model),
            -- Token counts only ever arrive with a completion, so a later start
            -- event must not blank them.
            tokens_in          = COALESCE(EXCLUDED.tokens_in, ai_span.tokens_in),
            tokens_out         = COALESCE(EXCLUDED.tokens_out, ai_span.tokens_out),
            cache_read_tokens  = COALESCE(EXCLUDED.cache_read_tokens, ai_span.cache_read_tokens),
            cache_write_tokens = COALESCE(EXCLUDED.cache_write_tokens, ai_span.cache_write_tokens),
            reasoning_tokens   = COALESCE(EXCLUDED.reasoning_tokens, ai_span.reasoning_tokens),
            latency_ms         = COALESCE(EXCLUDED.latency_ms, ai_span.latency_ms),
            prompt_id      = COALESCE(EXCLUDED.prompt_id, ai_span.prompt_id),
            prompt_version = COALESCE(EXCLUDED.prompt_version, ai_span.prompt_version),
            prompt_hash    = COALESCE(EXCLUDED.prompt_hash, ai_span.prompt_hash),
            started_at     = LEAST(ai_span.started_at, EXCLUDED.started_at),
            -- Terminal is sticky, so a start arriving after a completion cannot
            -- put the span back into `running`.
            status         = CASE WHEN ai_span.status = 'running' THEN EXCLUDED.status
                                  ELSE ai_span.status END,
            ended_at       = COALESCE(ai_span.ended_at, EXCLUDED.ended_at),
            updated_at     = now()
        RETURNING id, (xmax = 0) AS created
        """,
        (
            tenant_id,
            trace_id,
            ev["span_id"],
            ev.get("parent_span_id"),
            ev["span_kind"],
            ev.get("operation_name") or ev["span_id"],
            ev.get("provider"),
            ev.get("model"),
            ev.get("tokens_in"),
            ev.get("tokens_out"),
            ev.get("cache_read_tokens"),
            ev.get("cache_write_tokens"),
            ev.get("reasoning_tokens"),
            ev.get("latency_ms"),
            status,
            ev.get("prompt_id"),
            ev.get("prompt_version"),
            salted_prompt_hash(ev.get("prompt_hash"), salt),
            ev["occurred_at"],
            ev["occurred_at"] if terminal else None,
            ev["occurred_at"],
            terminal,
            terminal,
        ),
    ).fetchone()
    return str(row[0]), bool(row[1])


def _claim_cost(conn, span_id: str, amount: Decimal) -> bool:
    """Claim the right to count this span's money, exactly once.

    The conditional UPDATE is the whole mechanism: `costed_at` is NULL for a
    span nobody has priced, and setting it is atomic. Two concurrent deliveries
    of the same completion both try, one updates a row, the other updates none,
    and only the winner touches the financial tables. No dedupe table, no
    advisory lock, no window where a retry double-counts.
    """
    row = conn.execute(
        "UPDATE ai_span SET amount = %s, costed_at = now() "
        "WHERE id = %s AND costed_at IS NULL RETURNING id",
        (amount, span_id),
    ).fetchone()
    return row is not None


def _bump_trace_totals(conn, trace_id: str, amount: Decimal, tokens: int) -> None:
    conn.execute(
        "UPDATE ai_trace SET total_cost = total_cost + %s, total_tokens = total_tokens + %s, "
        "updated_at = now() WHERE id = %s",
        (amount, tokens, trace_id),
    )


def _bump_span_count(conn, trace_id: str, current_span: Optional[str]) -> None:
    conn.execute(
        "UPDATE ai_trace SET span_count = span_count + 1, "
        "current_span_id = COALESCE(%s, current_span_id), updated_at = now() WHERE id = %s",
        (current_span, trace_id),
    )


def ingest(tenant_id: str, events: list, batch_id: Optional[str] = None) -> dict:
    """Apply a batch of lifecycle events. Idempotent, transactional, bounded.

    One transaction for the whole batch, so a batch either lands or does not —
    a partially applied batch would leave a trace whose totals disagree with its
    spans, which is the one inconsistency this product cannot tolerate.

    Financial aggregation reuses hook.py's helpers deliberately. A second
    implementation of "add cost to inference_cost" would drift from the first,
    and §4's reconciliation requirement is precisely that these two agree.
    """
    from . import hook  # imported here: hook imports pricing, this avoids a cycle

    if not isinstance(events, list):
        raise TraceError("events must be a list.")
    if len(events) > MAX_EVENTS_PER_BATCH:
        raise TraceError(f"A batch may contain at most {MAX_EVENTS_PER_BATCH} events.")

    now = dt.datetime.now(dt.timezone.utc)
    parsed = [validate(e, now) for e in events]

    accepted = 0
    costed = Decimal("0")
    traces_touched: set = set()
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        if batch_id:
            prior = hook.replayed_batch(conn, tenant_id, batch_id)
            if prior is not None:
                return prior
        salt = hook.get_or_create_salt(tenant_id)
        valid_features = {str(r[0]) for r in conn.execute("SELECT id FROM feature").fetchall()}
        # Self-hosted pools: a model you run yourself has no per-token price, so
        # its usage is recorded and its cost allocated later from the pool's
        # infrastructure bill. Without this, instrumenting a self-hosted model
        # would silently meter nothing.
        pools = compute.pool_labels(conn)
        # Accumulated and written once at the end, so a batch containing twenty
        # spans of one feature does twenty in-memory adds and one upsert.
        cost_acc: dict = {}
        customer_acc: dict = {}
        signal_acc: dict = {}
        pool_acc: dict = {}

        for ev in parsed:
            # An unknown feature is Unattributed, never a rejected event: the
            # money was still spent and must not vanish because a feature was
            # renamed or deleted between instrumenting and sending.
            feature_id = ev.get("feature_id")
            if feature_id is not None and feature_id not in valid_features:
                feature_id = None
            app_id = applications.resolve(conn, tenant_id, ev.get("application") or "default")
            trace_id, _status = _upsert_trace(conn, tenant_id, app_id, feature_id, ev, now)
            traces_touched.add(trace_id)
            kind = ev["event_type"]

            if kind == "trace.heartbeat":
                _touch_heartbeat(conn, trace_id, ev["occurred_at"])
                accepted += 1
                continue
            if kind in ("trace.completed", "trace.failed", "trace.cancelled"):
                _finalize_trace(conn, trace_id, TERMINAL[kind], ev["occurred_at"])
                accepted += 1
                continue
            if kind == "trace.started":
                accepted += 1
                continue

            status = "running" if kind == "span.started" else TERMINAL[kind]
            span_id, created = _upsert_span(conn, tenant_id, trace_id, ev, status, salt)
            if created:
                _bump_span_count(conn, trace_id, ev["span_id"] if status == "running" else None)
            accepted += 1

            if status == "running":
                continue

            # Only a finalized LLM span with real tokens from a priced provider
            # is money. A tool call, a retrieval, a heartbeat and a start event
            # are all free by construction — there is no path here that invents
            # cost for them.
            provider = ev.get("provider")
            tokens_in = ev.get("tokens_in") or 0
            tokens_out = ev.get("tokens_out") or 0
            if provider in pools and ev["span_kind"] in ("llm", "embedding"):
                # Usage now, dollars later from compute.allocate. Claiming the
                # span keeps this exactly-once alongside the priced path.
                if _claim_cost(conn, span_id, Decimal("0")):
                    key = (
                        pools[provider],
                        feature_id,
                        ev.get("model") or None,
                        _period_of(ev["occurred_at"]),
                    )
                    entry = pool_acc.setdefault(key, {"tin": 0, "tout": 0, "count": 0})
                    entry["tin"] += tokens_in
                    entry["tout"] += tokens_out
                    entry["count"] += 1
                    _bump_trace_totals(conn, trace_id, Decimal("0"), tokens_in + tokens_out)
                continue

            signal = ev.get("signal")
            if signal and provider in PRICED_PROVIDERS:
                hook.accumulate_signal(
                    signal_acc,
                    signal,
                    signal["kind"],
                    feature_id,
                    provider,
                    ev.get("model") or "",
                    _period_of(ev["occurred_at"]),
                    tokens_in,
                    tokens_out,
                )
                if signal["kind"] == "prefix":
                    # A prefix signal summarises calls that were each metered
                    # already. Recording the signal must not re-cost them.
                    continue

            if ev["span_kind"] != "llm" or provider not in PRICED_PROVIDERS:
                continue
            if tokens_in == 0 and tokens_out == 0:
                continue

            amount = price(ev.get("model") or "", tokens_in, tokens_out, provider)
            if not _claim_cost(conn, span_id, amount):
                continue  # a replay: already counted, add nothing

            _bump_trace_totals(conn, trace_id, amount, tokens_in + tokens_out)
            costed += amount
            period = _period_of(ev["occurred_at"])
            key = (feature_id, provider, ev.get("model") or "", period)
            entry = cost_acc.setdefault(
                key, {"amount": Decimal("0"), "tin": 0, "tout": 0, "count": 0, "latency": 0}
            )
            entry["amount"] += amount
            entry["tin"] += tokens_in
            entry["tout"] += tokens_out
            entry["count"] += 1
            entry["latency"] += ev.get("latency_ms") or 0
            if ev.get("customer_id"):
                centry = customer_acc.setdefault(
                    (ev["customer_id"], period), {"amount": Decimal("0"), "count": 0}
                )
                centry["amount"] += amount
                centry["count"] += 1

        for (feature_id, provider, model, period), entry in cost_acc.items():
            hook.upsert_hook_row(conn, tenant_id, feature_id, provider, model, period, entry)
        for (customer_id, period), centry in customer_acc.items():
            hook.upsert_customer_cost(conn, tenant_id, customer_id, period, centry)
        for (pool_id, feature_id, model, period), entry in pool_acc.items():
            compute.record_usage(
                conn,
                tenant_id,
                pool_id,
                feature_id,
                model,
                period,
                entry["tin"],
                entry["tout"],
                entry["count"],
            )
        for skey, sentry in signal_acc.items():
            feature_id, provider, model, period, kind, fingerprint = skey
            hook.upsert_signal(
                conn, tenant_id, feature_id, provider, model, period, kind, fingerprint, sentry
            )

        if batch_id:
            hook.record_batch(conn, tenant_id, batch_id, accepted, costed)

    return {"accepted": accepted, "cost": float(costed), "traces": len(traces_touched)}
