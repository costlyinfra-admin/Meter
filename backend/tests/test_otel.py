"""OpenTelemetry as a source: what is read from a span, and what never is."""

from __future__ import annotations

import base64
import datetime as dt
import json

import pytest
from meter import otel, traces

NOW = dt.datetime.now(dt.timezone.utc)
TRACE = "5b8efff798038103d269b633813fc60c"
ROOT = "eee19b7ec3c1b174"
CHILD = "eee19b7ec3c1b173"


def nanos(when: dt.datetime) -> str:
    # OTLP/JSON renders 64-bit integers as strings.
    return str(int(when.timestamp() * 1_000_000_000))


def kv(key, value):
    if isinstance(value, bool):
        wrapped = {"boolValue": value}
    elif isinstance(value, int):
        wrapped = {"intValue": str(value)}
    elif isinstance(value, float):
        wrapped = {"doubleValue": value}
    else:
        wrapped = {"stringValue": value}
    return {"key": key, "value": wrapped}


def span(span_id=CHILD, *, parent=ROOT, name="chat claude-sonnet-4-6", attrs=None, **over):
    body = {
        "traceId": over.pop("trace_id", TRACE),
        "spanId": span_id,
        "name": name,
        "kind": over.pop("kind", 3),
        "startTimeUnixNano": nanos(over.pop("start", NOW - dt.timedelta(seconds=2))),
        "endTimeUnixNano": nanos(over.pop("end", NOW)),
        "attributes": attrs if attrs is not None else [],
    }
    if parent:
        body["parentSpanId"] = parent
    body.update(over)
    return body


def llm_attrs(**over):
    base = {
        "gen_ai.system": "anthropic",
        "gen_ai.request.model": "claude-sonnet-4-6",
        "gen_ai.usage.input_tokens": 1000,
        "gen_ai.usage.output_tokens": 500,
    }
    base.update(over)
    return [kv(k, v) for k, v in base.items()]


def export(*spans, service="support-agent", resource_extra=None):
    resource = [kv("service.name", service), kv("deployment.environment.name", "staging")]
    resource.extend(resource_extra or [])
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": resource},
                "scopeSpans": [
                    {
                        "scope": {"name": "opentelemetry.instrumentation.anthropic"},
                        "spans": list(spans),
                    }
                ],
            }
        ]
    }


def events_of(payload):
    return otel.translate(payload)["events"]


# ---------------------------------------------------------------------------
# Shape: what an OTel span becomes
# ---------------------------------------------------------------------------
def test_a_root_span_opens_and_closes_its_trace():
    # Only the root knows the run's real name and its true start and end.
    out = events_of(export(span(ROOT, parent=None, name="resolve-ticket", kind=2)))
    assert [e["event_type"] for e in out] == ["trace.started", "span.completed", "trace.completed"]
    assert out[0]["operation_name"] == "resolve-ticket"
    assert out[0]["occurred_at"] < out[2]["occurred_at"]


def test_a_child_span_is_one_finished_step_under_its_parent():
    # An OTel span is complete by the time it is exported — no start/finish pair.
    (step,) = events_of(export(span(attrs=llm_attrs())))
    assert step["event_type"] == "span.completed"
    assert step["span_id"] == CHILD
    assert step["parent_span_id"] == ROOT
    assert step["span_kind"] == "llm"
    assert (step["provider"], step["model"]) == ("anthropic", "claude-sonnet-4-6")
    assert (step["tokens_in"], step["tokens_out"]) == (1000, 500)
    assert step["latency_ms"] == 2000


def test_resource_attributes_name_the_application_and_environment():
    (step,) = events_of(export(span(attrs=llm_attrs()), service="Checkout.API"))
    # Service names carry dots the slug grammar does not allow.
    assert step["application"] == "checkout-api"
    assert step["environment"] == "staging"


def test_meter_attributes_override_the_service_name_and_attach_a_feature():
    attrs = llm_attrs() + [kv("meter.application", "billing-bot"), kv("meter.feature_id", "f-1")]
    (step,) = events_of(export(span(attrs=attrs)))
    assert step["application"] == "billing-bot"
    assert step["feature_id"] == "f-1"


def test_an_error_status_fails_the_step_and_the_run():
    failed = span(ROOT, parent=None, status={"code": "STATUS_CODE_ERROR"})
    kinds = [e["event_type"] for e in events_of(export(failed))]
    assert kinds == ["trace.started", "span.failed", "trace.failed"]


def test_an_all_zero_parent_id_means_no_parent():
    # OTel encodes "absent" as zeroes; treating that as a real parent would
    # leave the run without a root and never close it.
    out = events_of(export(span(ROOT, parent="0000000000000000")))
    assert out[0]["event_type"] == "trace.started"


def test_the_model_that_answered_is_priced_not_the_one_requested():
    attrs = llm_attrs(**{"gen_ai.response.model": "claude-sonnet-4-6-20260101"})
    (step,) = events_of(export(span(attrs=attrs)))
    assert step["model"] == "claude-sonnet-4-6-20260101"


@pytest.mark.parametrize(
    "system, expected",
    [
        ("aws.bedrock", "bedrock"),
        ("az.ai.openai", "openai"),
        ("gcp.vertex_ai", "google"),
        ("mistral_ai", "mistral"),
        ("OpenAI", "openai"),
        ("some-new-host", "some-new-host"),  # recorded, just not priced
    ],
)
def test_gen_ai_system_values_map_onto_priced_providers(system, expected):
    (step,) = events_of(export(span(attrs=llm_attrs(**{"gen_ai.system": system}))))
    assert step["provider"] == expected


def test_legacy_and_openinference_token_names_are_both_read():
    legacy = [
        kv("gen_ai.system", "openai"),
        kv("gen_ai.usage.prompt_tokens", 7),
        kv("gen_ai.usage.completion_tokens", 3),
    ]
    openinference = [
        kv("llm.provider", "openai"),
        kv("llm.token_count.prompt", 11),
        kv("llm.token_count.completion", 4),
        kv("openinference.span.kind", "LLM"),
    ]
    (a,) = events_of(export(span(attrs=legacy)))
    (b,) = events_of(export(span(attrs=openinference)))
    assert (a["tokens_in"], a["tokens_out"]) == (7, 3)
    assert (b["tokens_in"], b["tokens_out"], b["span_kind"]) == (11, 4, "llm")


@pytest.mark.parametrize(
    "attr, value, kind",
    [
        ("openinference.span.kind", "RETRIEVER", "retrieval"),
        ("openinference.span.kind", "TOOL", "tool"),
        ("gen_ai.operation.name", "embeddings", "embedding"),
        ("gen_ai.operation.name", "execute_tool", "tool"),
        ("traceloop.span.kind", "workflow", "workflow"),
    ],
)
def test_step_kinds_follow_the_instrumentation(attr, value, kind):
    (step,) = events_of(export(span(attrs=[kv(attr, value)])))
    assert step["span_kind"] == kind


def test_a_span_with_no_model_call_is_not_called_an_llm_step():
    # Putting a free step in the paid bucket would misstate what a run spent on.
    (step,) = events_of(export(span(name="lookup-account", attrs=[])))
    assert step["span_kind"] != "llm"


def test_protobuf_ids_arrive_base64_and_are_read_as_hex():
    # MessageToDict base64-encodes bytes fields; OTLP/JSON specifies hex.
    b64 = base64.b64encode(bytes.fromhex(CHILD)).decode()
    (step,) = events_of(export(span(b64, attrs=llm_attrs())))
    assert step["span_id"] == CHILD


def test_a_span_that_cannot_be_placed_is_skipped_not_fatal():
    result = otel.translate(export(span(attrs=llm_attrs()), {"name": "no ids at all"}))
    assert result["spans"] == 2
    assert result["skipped"] == 1
    assert len(result["events"]) == 1


def test_an_oversized_export_is_refused():
    many = [span(f"{i:016x}") for i in range(otel.MAX_SPANS_PER_REQUEST + 1)]
    with pytest.raises(otel.OtelError, match="at most"):
        otel.translate(export(*many))


def test_every_translated_event_passes_the_sdk_contract():
    # The translator produces SDK events, so it is held to the SDK's allowlist.
    out = events_of(export(span(ROOT, parent=None), span(attrs=llm_attrs())))
    for event in out:
        traces.validate(event, NOW)


# ---------------------------------------------------------------------------
# Privacy: content never leaves the translator
# ---------------------------------------------------------------------------
SECRET_PROMPT = "Summarise the incident for ACME-SECRET-PROMPT"
SECRET_REPLY = "The breach was ACME-SECRET-REPLY"


def test_content_attributes_are_never_copied():
    attrs = llm_attrs() + [
        kv("gen_ai.prompt.0.content", SECRET_PROMPT),
        kv("gen_ai.completion.0.content", SECRET_REPLY),
        kv("input.value", SECRET_PROMPT),
        kv("output.value", SECRET_REPLY),
        kv("llm.input_messages.0.message.content", SECRET_PROMPT),
        kv("traceloop.entity.input", SECRET_PROMPT),
        kv("exception.stacktrace", "Traceback ACME-SECRET-STACK"),
    ]
    result = otel.translate(export(span(attrs=attrs)))
    blob = json.dumps(result)
    for secret in ("ACME-SECRET-PROMPT", "ACME-SECRET-REPLY", "ACME-SECRET-STACK"):
        assert secret not in blob
    # And the sender is told, so it can stop sending it.
    assert result["content_dropped"] is True


def test_span_events_are_not_read_at_all():
    # GenAI conventions put prompts in span EVENTS too. Not reading them is the
    # guarantee; checking their contents would already be reading them.
    body = span(attrs=llm_attrs())
    body["events"] = [
        {"name": "gen_ai.content.prompt", "attributes": [kv("gen_ai.prompt", SECRET_PROMPT)]},
        {"name": "exception", "attributes": [kv("exception.message", "ACME-SECRET-STACK")]},
    ]
    blob = json.dumps(otel.translate(export(body)))
    assert "ACME-SECRET-PROMPT" not in blob
    assert "ACME-SECRET-STACK" not in blob


def test_an_attribute_nobody_named_is_not_copied():
    # A convention invented tomorrow is excluded today, because nothing looks
    # up a key that is not on the list.
    attrs = llm_attrs() + [kv("enduser.id", "alice@acme.com"), kv("http.url", "https://x/?k=v")]
    resource_extra = [kv("host.name", "prod-box-7"), kv("tenant_id", "someone-elses-tenant")]
    blob = json.dumps(otel.translate(export(span(attrs=attrs), resource_extra=resource_extra)))
    for leaked in ("alice@acme.com", "https://x/?k=v", "prod-box-7", "someone-elses-tenant"):
        assert leaked not in blob


def test_a_clean_export_reports_no_content():
    assert otel.translate(export(span(attrs=llm_attrs())))["content_dropped"] is False


def test_nested_attribute_values_are_not_walked():
    # Arrays and maps are never descended into: nothing stored is one, and a
    # deeply nested value must cost nothing to skip.
    nested = {
        "key": "gen_ai.request.model",
        "value": {"kvlistValue": {"values": [kv("x", SECRET_PROMPT)]}},
    }
    (step,) = events_of(export(span(attrs=[kv("gen_ai.system", "openai"), nested])))
    assert step["model"] is None
    assert "ACME-SECRET-PROMPT" not in json.dumps(step)


# ---------------------------------------------------------------------------
# Ingest: the same pipeline as the SDK
# ---------------------------------------------------------------------------
def _cost(app_env, tenant_id):
    return app_env.execute(
        "SELECT coalesce(sum(total_cost), 0), max(source) FROM ai_trace WHERE tenant_id = %s",
        (tenant_id,),
    ).fetchone()


def test_an_otel_span_is_priced_by_the_same_path_as_the_sdk(tenant_id, app_env):
    payload = export(
        span(ROOT, parent=None, name="resolve-ticket"),
        span(
            attrs=llm_attrs(
                **{"gen_ai.usage.input_tokens": 1_000_000, "gen_ai.usage.output_tokens": 0}
            )
        ),
    )
    result = otel.ingest(tenant_id, payload)
    assert result["accepted"] == 4  # started, root step, llm step, completed

    cost, source = _cost(app_env, tenant_id)
    assert float(cost) == pytest.approx(3.0)  # $3 per million input tokens
    assert source == "otel"


def test_a_replayed_export_adds_nothing(tenant_id, app_env):
    # Exporters retry. Idempotency comes from claiming each span's cost, so a
    # resent request — however it was chunked — cannot double the spend.
    payload = export(span(attrs=llm_attrs(**{"gen_ai.usage.input_tokens": 1_000_000})))
    otel.ingest(tenant_id, payload)
    otel.ingest(tenant_id, payload)
    cost, _ = _cost(app_env, tenant_id)
    assert float(cost) == pytest.approx(3.0 + 500 * 15 / 1_000_000)


def test_a_child_exported_before_its_root_still_gets_the_run_name(tenant_id, app_env):
    # Children flush first in any real exporter; the root lands in a later batch.
    otel.ingest(tenant_id, export(span(attrs=llm_attrs())))
    otel.ingest(tenant_id, export(span(ROOT, parent=None, name="resolve-ticket")))
    name, status = app_env.execute(
        "SELECT operation_name, status FROM ai_trace WHERE tenant_id = %s", (tenant_id,)
    ).fetchone()
    assert (name, status) == ("resolve-ticket", "success")


def test_recent_trace_does_not_credit_otel_for_sdk_traffic(tenant_id):
    # An organization already on the SDK must not see a green light on the
    # OpenTelemetry guide for work it has not done.
    traces.ingest(
        tenant_id,
        [{"event_type": "trace.started", "trace_id": "sdk-run", "application": "support-agent"}],
    )
    assert traces.recent_trace(tenant_id, "sdk") is not None
    assert traces.recent_trace(tenant_id, "otel") is None

    otel.ingest(tenant_id, export(span(ROOT, parent=None, name="resolve-ticket")))
    found = traces.recent_trace(tenant_id, "otel")
    assert found["application"] == "support-agent"
    assert found["operation_name"] == "resolve-ticket"
    assert set(found) == {"application", "operation_name", "span_count", "received_at"}


# ---------------------------------------------------------------------------
# Attribution: getting a feature onto the spans that carry cost
# ---------------------------------------------------------------------------
def test_a_service_that_is_one_feature_can_say_so_once():
    resource_extra = [kv("meter.feature_id", "f-svc")]
    (step,) = events_of(export(span(attrs=llm_attrs()), resource_extra=resource_extra))
    assert step["feature_id"] == "f-svc"


def test_a_feature_on_the_run_reaches_the_model_calls_exported_with_it():
    # The model-call span is made by an instrumentation library; nobody can set
    # meter.feature_id on it. Children end first, so they come first in a batch.
    root = span(
        ROOT,
        parent=None,
        name="resolve-ticket",
        attrs=[kv("meter.feature_id", "f-run"), kv("meter.customer_id", "c-9")],
    )
    out = events_of(export(span(attrs=llm_attrs()), root))
    step = next(e for e in out if e.get("span_id") == CHILD)
    assert (step["feature_id"], step["customer_id"]) == ("f-run", "c-9")


def test_a_step_that_names_its_own_feature_keeps_it():
    root = span(ROOT, parent=None, attrs=[kv("meter.feature_id", "f-run")])
    child = span(attrs=llm_attrs() + [kv("meter.feature_id", "f-step")])
    out = events_of(export(root, child))
    step = next(e for e in out if e.get("span_id") == CHILD)
    assert step["feature_id"] == "f-step"


def test_a_feature_is_never_lent_across_runs():
    other_run = span(
        "aaaaaaaaaaaaaaaa", parent=None, trace_id="1" * 32, attrs=[kv("meter.feature_id", "f-x")]
    )
    out = events_of(export(span(attrs=llm_attrs()), other_run))
    step = next(e for e in out if e.get("span_id") == CHILD)
    assert step["feature_id"] is None


def test_a_run_s_feature_lands_its_cost_on_that_feature(tenant_id, app_env):
    # End to end: the lent feature is the one the money is attributed to, not
    # just a field on an event.
    feature = app_env.execute(
        "INSERT INTO feature (tenant_id, name) VALUES (%s, 'Ticket resolution') RETURNING id",
        (tenant_id,),
    ).fetchone()[0]
    app_env.commit()
    root = span(ROOT, parent=None, attrs=[kv("meter.feature_id", str(feature))])
    child = span(
        attrs=llm_attrs(**{"gen_ai.usage.input_tokens": 1_000_000, "gen_ai.usage.output_tokens": 0})
    )
    otel.ingest(tenant_id, export(child, root))

    (attributed,) = app_env.execute(
        "SELECT coalesce(sum(amount), 0) FROM inference_cost"
        " WHERE tenant_id = %s AND feature_id = %s",
        (tenant_id, feature),
    ).fetchone()
    assert float(attributed) == pytest.approx(3.0)
