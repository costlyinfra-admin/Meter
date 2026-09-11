"""OpenTelemetry as a source: OTLP spans in, Meter traces out.

Most teams instrumenting an LLM application are already emitting OpenTelemetry
— through OpenLLMetry, OpenInference, Traceloop, or the GenAI semantic
conventions in the OTel SDKs themselves. This module lets them point an
existing exporter at Meter and get request-level economics without adopting
Meter's SDK or touching a call site.

It is a TRANSLATOR, not a second ingest path. An OTLP payload becomes the same
lifecycle events `traces.ingest` already accepts, so pricing, attribution,
exactly-once costing, RLS and the Unattributed rule are shared with the SDK
rather than reimplemented. Every property traces.py guarantees is inherited
here because the same code does the work.

WHY THE ALLOWLIST IS SHAPED THE WAY IT IS. GenAI instrumentation captures
prompts and completions by default, and puts them in span attributes
(`gen_ai.prompt.0.content`), in span events (`gen_ai.content.prompt`), and in
whole-payload blobs (`input.value`, `traceloop.entity.input`). Meter must never
store any of it. So this module never copies an attribute it cannot name:
`_named()` builds a dict containing ONLY keys from a fixed set, and span events
are not read at all. There is no path here that moves an unrecognized value
anywhere — which is a stronger guarantee than checking arrivals against a list
of things to refuse, because a convention invented tomorrow is already excluded.

What the sender should still do is turn content capture OFF at the source: a
prompt dropped on arrival was a prompt that crossed the network. Meter says so
in the export response (`partialSuccess.errorMessage`), which collectors log as
a warning, and in the setup guide. Dropping is a backstop, not the design.

WHY DROPPED AND NOT REFUSED. The SDK path rejects a batch carrying content
LOUDLY, because whoever sent it wrote the call site and can fix it. Here the
attribute was very likely added by a library the sender did not write and may
not control. Failing their export would cost them every span, including the
ones carrying the numbers, to punish something they did not choose.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import re
from typing import Any, Optional

from . import applications, traces

#: OTLP senders batch a few hundred spans. This is far above any default and
#: bounds what one request can make the server allocate.
MAX_SPANS_PER_REQUEST = 2000
#: Lifecycle events per call into traces.ingest. A root span expands to three
#: events, so a request at the span cap stays comfortably inside this chunking.
EVENTS_PER_CHUNK = traces.MAX_EVENTS_PER_BATCH

#: OTel status codes. 0 UNSET, 1 OK, 2 ERROR.
_STATUS_ERROR = {2, "2", "STATUS_CODE_ERROR"}


class OtelError(ValueError):
    """A malformed or oversized OTLP payload (maps to HTTP 400)."""


# ---------------------------------------------------------------------------
# Attribute names we are willing to read
#
# Grouped by the convention that defines them. A key absent from these tuples is
# never looked up, so its value is never copied out of the payload.
# ---------------------------------------------------------------------------

#: Resource attributes: which application, which environment, which release.
_RES_SERVICE = ("service.name",)
_RES_ENV = ("deployment.environment.name", "deployment.environment")
_RES_VERSION = ("service.version",)

#: gen_ai.system / llm.provider — who served the call.
_PROVIDER = ("gen_ai.system", "gen_ai.provider.name", "llm.provider", "llm.system")

#: The model that actually served it, preferred over the one requested: a
#: request for "gpt-4o" answered by "gpt-4o-2024-11-20" is priced as the latter.
_MODEL = ("gen_ai.response.model", "gen_ai.request.model", "llm.model_name", "llm.model")

_TOKENS_IN = (
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.prompt_tokens",  # pre-1.27 semconv
    "llm.token_count.prompt",  # OpenInference
)
_TOKENS_OUT = (
    "gen_ai.usage.output_tokens",
    "gen_ai.usage.completion_tokens",
    "llm.token_count.completion",
)
_CACHE_READ = (
    "gen_ai.usage.cache_read_input_tokens",
    "gen_ai.usage.cached_input_tokens",
    "llm.token_count.prompt_details.cache_read",
)
_CACHE_WRITE = (
    "gen_ai.usage.cache_creation_input_tokens",
    "llm.token_count.prompt_details.cache_write",
)
_REASONING = (
    "gen_ai.usage.reasoning_tokens",
    "llm.token_count.completion_details.reasoning",
)

#: How the instrumentation described the step.
_KIND = (
    "openinference.span.kind",
    "gen_ai.operation.name",
    "traceloop.span.kind",
)

#: Meter's own attributes. The only way to attach a feature, and the only place
#: a customer reference or a prompt identity is accepted — deliberately explicit
#: so neither is ever inferred from a convention that might carry something else.
_FEATURE = ("meter.feature_id", "meter.feature")
_APPLICATION = ("meter.application",)
_CUSTOMER = ("meter.customer_id",)
_PROMPT_ID = ("meter.prompt_id",)
_PROMPT_VERSION = ("meter.prompt_version",)
_PROMPT_HASH = ("meter.prompt_hash",)

#: Every key above, as one set. `_named()` reads nothing outside it.
_READABLE = frozenset(
    _RES_SERVICE
    + _RES_ENV
    + _RES_VERSION
    + _PROVIDER
    + _MODEL
    + _TOKENS_IN
    + _TOKENS_OUT
    + _CACHE_READ
    + _CACHE_WRITE
    + _REASONING
    + _KIND
    + _FEATURE
    + _APPLICATION
    + _CUSTOMER
    + _PROMPT_ID
    + _PROMPT_VERSION
    + _PROMPT_HASH
)

#: Keys whose PRESENCE means the sender is still capturing content. Only the key
#: name is ever examined; the value is not read, counted or copied. Used solely
#: to warn the sender in the export response.
_CONTENT_PREFIXES = (
    "gen_ai.prompt",
    "gen_ai.completion",
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "gen_ai.content",
    "gen_ai.system_instructions",
    "gen_ai.tool.call.arguments",
    "gen_ai.tool.call.result",
    "llm.input_messages",
    "llm.output_messages",
    "llm.prompts",
    "llm.prompt_template",
    "input.value",
    "output.value",
    "retrieval.documents",
    "tool.parameters",
    "traceloop.entity.input",
    "traceloop.entity.output",
    "exception.message",
    "exception.stacktrace",
)

#: gen_ai.system values, mapped onto the provider names Meter prices. Anything
#: unlisted passes through as-is: it is recorded as usage and simply not priced,
#: which is the honest outcome for a provider Meter has no rate card for.
_PROVIDER_ALIASES = {
    "az.ai.openai": "openai",
    "azure.ai.openai": "openai",
    "azure_openai": "openai",
    "aws.bedrock": "bedrock",
    "aws_bedrock": "bedrock",
    "amazon.bedrock": "bedrock",
    "gcp.gemini": "google",
    "gcp.vertex_ai": "google",
    "vertex_ai": "google",
    "vertexai": "google",
    "google_genai": "google",
    "gemini": "google",
    "mistral_ai": "mistral",
    "together_ai": "together",
    "perplexity_ai": "perplexity",
    "x_ai": "xai",
}

#: Step kinds, mapped onto Meter's seven. Keys are lowercased attribute values.
_KIND_ALIASES = {
    # OpenInference
    "llm": "llm",
    "embedding": "embedding",
    "retriever": "retrieval",
    "reranker": "retrieval",
    "tool": "tool",
    "chain": "workflow",
    "agent": "workflow",
    "guardrail": "guardrail",
    "evaluator": "evaluation",
    # OTel GenAI operation names
    "chat": "llm",
    "text_completion": "llm",
    "generate_content": "llm",
    "embeddings": "embedding",
    "execute_tool": "tool",
    "invoke_agent": "workflow",
    "create_agent": "workflow",
    # Traceloop
    "workflow": "workflow",
    "task": "workflow",
}


# ---------------------------------------------------------------------------
# OTLP decoding
#
# One shape is handled: the JSON mapping, camelCase or snake_case. Protobuf
# requests are converted to it by `from_protobuf` before they reach here, so
# there is a single translator rather than one per wire format.
# ---------------------------------------------------------------------------
def _get(obj: dict, *names: str) -> Any:
    for name in names:
        if name in obj:
            return obj[name]
    return None


def _scalar(value: Any) -> Optional[Any]:
    """One OTLP AnyValue, if it is a scalar. Arrays and maps are not read.

    Nothing Meter stores is a list or a nested object, so refusing to descend is
    free — and it means a deeply nested attribute cannot cost anything to walk.
    """
    if not isinstance(value, dict):
        return None
    for field in ("stringValue", "string_value"):
        if field in value:
            return value[field]
    for field in ("intValue", "int_value"):
        if field in value:
            return value[field]
    for field in ("doubleValue", "double_value"):
        if field in value:
            return value[field]
    for field in ("boolValue", "bool_value"):
        if field in value:
            return value[field]
    return None


def _named(attributes: Any) -> dict:
    """Values for the keys in `_READABLE`, and nothing else.

    This is the privacy boundary. An attribute whose key is not named above is
    not copied, not counted and not inspected — the loop simply moves on.
    """
    out: dict = {}
    if not isinstance(attributes, list):
        return out
    for entry in attributes:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if isinstance(key, str) and key in _READABLE and key not in out:
            value = _scalar(entry.get("value"))
            if value is not None:
                out[key] = value
    return out


def _carries_content(attributes: Any) -> bool:
    """Whether content-bearing attributes are present. Reads key names only."""
    if not isinstance(attributes, list):
        return False
    for entry in attributes:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if isinstance(key, str) and key.startswith(_CONTENT_PREFIXES):
            return True
    return False


def _first(named: dict, keys: tuple) -> Optional[Any]:
    for key in keys:
        if key in named:
            return named[key]
    return None


def _hex_id(raw: Any) -> Optional[str]:
    """A trace or span id as hex, from either OTLP encoding.

    OTLP/JSON specifies hex. Protobuf carries raw bytes, and the conversion to a
    dict base64-encodes them. Both arrive here, so both are accepted — an id
    that is already hex is left alone, anything else is decoded and re-encoded.
    """
    if isinstance(raw, bytes):
        return raw.hex() or None
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if re.fullmatch(r"[0-9a-fA-F]+", text) and len(text) % 2 == 0:
        lowered = text.lower()
        # All-zero ids mean "absent" in OTel and must not become a real parent.
        return None if not lowered.strip("0") else lowered
    try:
        decoded = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return None
    return decoded.hex() if decoded.strip(b"\x00") else None


def _nanos(raw: Any) -> Optional[dt.datetime]:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    try:
        return dt.datetime.fromtimestamp(value / 1_000_000_000, dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _int(raw: Any) -> Optional[int]:
    """A token count. OTLP/JSON renders 64-bit integers as strings."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if 0 <= value <= traces.MAX_TOKENS else None


def _slug(raw: Any) -> Optional[str]:
    """A service name as an application slug, or None if it cannot be one.

    Service names carry dots ("checkout.api") that the slug grammar does not
    allow, so they become dashes. A name that survives as nothing is not an
    error worth failing an export over — the events land under `default`.
    """
    if not isinstance(raw, str):
        return None
    candidate = re.sub(r"[^a-z0-9-]+", "-", raw.strip().lower()).strip("-")
    if not candidate:
        return None
    try:
        return applications.normalize_slug(candidate[: applications.MAX_SLUG])
    except applications.ApplicationError:
        return None


def _provider(named: dict) -> Optional[str]:
    raw = _first(named, _PROVIDER)
    if not isinstance(raw, str):
        return None
    value = raw.strip().lower()
    if not value:
        return None
    return _PROVIDER_ALIASES.get(value, value)[: traces.MAX_TINY]


def _span_kind(named: dict, otel_kind: Any) -> str:
    """Meter's step kind for this span.

    Falls back to `llm` only when the span reports token usage — a span with no
    model call in it is not an LLM step, and calling it one would put a free
    step in the same bucket as the paid ones.
    """
    for key in _KIND:
        raw = named.get(key)
        if isinstance(raw, str):
            mapped = _KIND_ALIASES.get(raw.strip().lower())
            if mapped:
                return mapped
    if _first(named, _MODEL) or _first(named, _TOKENS_IN) or _first(named, _TOKENS_OUT):
        return "llm"
    # SPAN_KIND_SERVER / CONSUMER roots are the shape of a workflow entry point.
    if otel_kind in (2, "2", "SPAN_KIND_SERVER", 5, "5", "SPAN_KIND_CONSUMER"):
        return "workflow"
    return "tool"


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------
def translate(payload: dict) -> dict:
    """An OTLP ExportTraceServiceRequest as Meter lifecycle events.

    Returns the events plus what the sender should be told: how many spans were
    seen, how many could not be used, and whether content arrived. Nothing
    returned here contains an attribute value the payload did not name.
    """
    if not isinstance(payload, dict):
        raise OtelError("Body must be an OTLP ExportTraceServiceRequest object.")
    resource_spans = _get(payload, "resourceSpans", "resource_spans")
    if resource_spans is None:
        resource_spans = []
    if not isinstance(resource_spans, list):
        raise OtelError("resourceSpans must be a list.")

    events: list = []
    seen = 0
    skipped = 0
    content_seen = False

    for resource_span in resource_spans:
        if not isinstance(resource_span, dict):
            skipped += 1
            continue
        resource = resource_span.get("resource")
        res_named = _named(resource.get("attributes")) if isinstance(resource, dict) else {}
        defaults = {
            "application": _slug(_first(res_named, _RES_SERVICE)),
            "environment": _first(res_named, _RES_ENV),
            "release_version": _first(res_named, _RES_VERSION),
            # A service that IS one feature can say so once, for every span.
            "feature_id": _first(res_named, _FEATURE),
        }

        scope_spans = _get(resource_span, "scopeSpans", "scope_spans") or []
        if not isinstance(scope_spans, list):
            skipped += 1
            continue
        for scope_span in scope_spans:
            if not isinstance(scope_span, dict):
                skipped += 1
                continue
            spans = scope_span.get("spans") or []
            if not isinstance(spans, list):
                skipped += 1
                continue
            for span in spans:
                seen += 1
                if seen > MAX_SPANS_PER_REQUEST:
                    raise OtelError(
                        f"A request may carry at most {MAX_SPANS_PER_REQUEST} spans. "
                        "Lower the exporter's batch size."
                    )
                if not isinstance(span, dict):
                    skipped += 1
                    continue
                if _carries_content(span.get("attributes")):
                    content_seen = True
                produced = _span_events(span, defaults)
                if produced:
                    events.extend(produced)
                else:
                    skipped += 1

    _lend_within_trace(events)
    return {
        "events": events,
        "spans": seen,
        "skipped": skipped,
        "content_dropped": content_seen,
    }


#: What a step of a run inherits from the run when it does not say for itself.
_INHERITED = ("feature_id", "customer_id")


def _lend_within_trace(events: list) -> None:
    """Carry a run's feature and customer to the steps exported with it.

    OpenTelemetry does not copy a span's attributes onto its children, and the
    spans that carry cost are created by an instrumentation library nobody can
    set an attribute on. So a `meter.feature_id` set on the run's own span would
    leave every model call under it Unattributed. Within one export, the first
    event of a trace that names a feature lends it to the ones that do not.

    A backstop, not the mechanism. Across exports it cannot help — a root span
    ends last and so is exported last, after its children were already priced —
    which is why the setup guide recommends baggage, and why nothing here reaches
    back to re-attribute cost that has already landed.
    """
    lent: dict = {}
    for event in events:
        for field in _INHERITED:
            if event.get(field):
                lent.setdefault((event["trace_id"], field), event[field])
    for event in events:
        for field in _INHERITED:
            if not event.get(field):
                inherited = lent.get((event["trace_id"], field))
                if inherited:
                    event[field] = inherited


def _span_events(span: dict, defaults: dict) -> list:
    """One OTel span as the Meter events it implies.

    An OTel span is already finished when it is exported, so it maps to a single
    completion rather than a start/finish pair. A ROOT span additionally opens
    and closes the trace — it is the only span that knows the run's real name
    and its true beginning and end.
    """
    trace_id = _hex_id(_get(span, "traceId", "trace_id"))
    span_id = _hex_id(_get(span, "spanId", "span_id"))
    if not trace_id or not span_id:
        return []  # unusable: nothing to attach the step to

    started = _nanos(_get(span, "startTimeUnixNano", "start_time_unix_nano"))
    ended = _nanos(_get(span, "endTimeUnixNano", "end_time_unix_nano"))
    if started is None:
        started = ended
    if ended is None:
        ended = started
    if started is None:
        return []  # a step with no time at all cannot be placed in a period

    named = _named(span.get("attributes"))
    status = span.get("status")
    code = status.get("code") if isinstance(status, dict) else None
    failed = code in _STATUS_ERROR

    name = span.get("name")
    operation = name.strip()[: traces.MAX_NAME] if isinstance(name, str) and name.strip() else None

    application = _slug(_first(named, _APPLICATION)) or defaults["application"] or "default"
    environment = _first(named, _RES_ENV) or defaults["environment"]
    common = {
        "trace_id": trace_id,
        "application": application,
        "environment": str(environment)[: traces.MAX_TINY] if environment else None,
        "release_version": _text(defaults["release_version"], traces.MAX_SHORT),
        "feature_id": _text(_first(named, _FEATURE) or defaults["feature_id"], traces.MAX_ID),
        "customer_id": _text(_first(named, _CUSTOMER), traces.MAX_NAME),
    }

    latency = max(0, int((ended - started).total_seconds() * 1000)) if ended else None
    step = {
        **common,
        "event_type": "span.failed" if failed else "span.completed",
        "span_id": span_id,
        "parent_span_id": _hex_id(_get(span, "parentSpanId", "parent_span_id")),
        "span_kind": _span_kind(named, _get(span, "kind")),
        "operation_name": operation,
        "provider": _provider(named),
        "model": _text(_first(named, _MODEL), traces.MAX_SHORT),
        "tokens_in": _int(_first(named, _TOKENS_IN)),
        "tokens_out": _int(_first(named, _TOKENS_OUT)),
        "cache_read_tokens": _int(_first(named, _CACHE_READ)),
        "cache_write_tokens": _int(_first(named, _CACHE_WRITE)),
        "reasoning_tokens": _int(_first(named, _REASONING)),
        "latency_ms": latency,
        "prompt_id": _text(_first(named, _PROMPT_ID), traces.MAX_NAME),
        "prompt_version": _text(_first(named, _PROMPT_VERSION), traces.MAX_TINY),
        "prompt_hash": _text(_first(named, _PROMPT_HASH), 128),
        "occurred_at": ended.isoformat(),
    }

    if step["parent_span_id"]:
        return [step]

    # A root span: the run starts when it starts and is over when it ends.
    return [
        {
            **common,
            "event_type": "trace.started",
            "operation_name": operation,
            "occurred_at": started.isoformat(),
        },
        step,
        {
            **common,
            "event_type": "trace.failed" if failed else "trace.completed",
            "operation_name": operation,
            "occurred_at": ended.isoformat(),
        },
    ]


def _text(raw: Any, limit: int) -> Optional[str]:
    if raw is None or isinstance(raw, bool):
        return None
    text = str(raw).strip()
    return text[:limit] if text else None


# ---------------------------------------------------------------------------
# Protobuf
# ---------------------------------------------------------------------------
def protobuf_available() -> bool:
    """Whether this deployment can decode `application/x-protobuf` exports."""
    try:  # noqa: SIM105 - the import IS the check
        import opentelemetry.proto.collector.trace.v1.trace_service_pb2  # noqa: F401
    except Exception:
        return False
    return True


def from_protobuf(body: bytes) -> dict:
    """A binary OTLP request as the JSON shape `translate` reads.

    Senders default to `http/protobuf`, so this is the common path rather than
    the exotic one. Decoding goes through the generated OTLP classes: a
    hand-rolled wire parser would be one more thing to get right against
    hostile input, for no benefit.
    """
    try:
        from google.protobuf.json_format import MessageToDict
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )
    except Exception as exc:  # pragma: no cover - exercised by deployment, not tests
        raise OtelError(
            "This deployment cannot decode protobuf exports. Set "
            "OTEL_EXPORTER_OTLP_PROTOCOL=http/json on the exporter."
        ) from exc

    request = ExportTraceServiceRequest()
    try:
        request.ParseFromString(body)
    except Exception as exc:
        raise OtelError("Body is not a valid OTLP ExportTraceServiceRequest.") from exc
    return MessageToDict(request)


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------
def ingest(tenant_id: str, payload: dict) -> dict:
    """Translate an OTLP payload and apply it. Returns what to tell the sender.

    Chunked rather than sent as one batch: an OTLP request can carry more spans
    than `traces.ingest` accepts at once. Chunking is safe because idempotency
    in traces.py comes from claiming each span's cost, not from the batch — a
    replayed request adds nothing however it was split.
    """
    result = translate(payload)
    events = result["events"]
    accepted = 0
    for start in range(0, len(events), EVENTS_PER_CHUNK):
        chunk = events[start : start + EVENTS_PER_CHUNK]
        outcome = traces.ingest(tenant_id, chunk, source="otel")
        accepted += outcome.get("accepted", 0)
    return {
        "spans": result["spans"],
        "skipped": result["skipped"],
        "accepted": accepted,
        "content_dropped": result["content_dropped"],
    }
