"""The OTLP/HTTP receiver: auth, wire formats, compression, and what it says back."""

from __future__ import annotations

import datetime as dt
import gzip
import json

import pytest
from fastapi.testclient import TestClient
from meter.api import create_app
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span

PASSWORD = "correct horse battery"
URL = "/api/otel/v1/traces"
NOW = dt.datetime.now(dt.timezone.utc)


@pytest.fixture
def client(admin_conn, admin_conninfo, app_conninfo, monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "unit-test-secret-key")
    monkeypatch.setenv("DATABASE_URL", admin_conninfo)
    monkeypatch.setenv("DATABASE_APP_URL", app_conninfo)
    c = TestClient(create_app())
    c.post("/api/auth/signup", json={"email": "cto@acme.com", "password": PASSWORD})
    return c


@pytest.fixture
def token(client):
    return client.post("/api/hook/token").json()["token"]


def auth(token, **extra):
    return {"Authorization": f"Bearer {token}", **extra}


def nanos(when):
    return int(when.timestamp() * 1_000_000_000)


def json_export(extra_attrs=()):
    attrs = [
        {"key": "gen_ai.system", "value": {"stringValue": "anthropic"}},
        {"key": "gen_ai.request.model", "value": {"stringValue": "claude-sonnet-4-6"}},
        {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "1000000"}},
        {"key": "gen_ai.usage.output_tokens", "value": {"intValue": "0"}},
        *extra_attrs,
    ]
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": "triage"}}]
                },
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "0af7651916cd43dd8448eb211c80319c",
                                "spanId": "b7ad6b7169203331",
                                "name": "chat",
                                "startTimeUnixNano": str(nanos(NOW - dt.timedelta(seconds=1))),
                                "endTimeUnixNano": str(nanos(NOW)),
                                "attributes": attrs,
                            }
                        ]
                    }
                ],
            }
        ]
    }


def proto_export() -> bytes:
    def kv(key, **value):
        return KeyValue(key=key, value=AnyValue(**value))

    root = Span(
        trace_id=bytes.fromhex("0af7651916cd43dd8448eb211c80319c"),
        span_id=bytes.fromhex("00f067aa0ba902b7"),
        name="resolve-ticket",
        start_time_unix_nano=nanos(NOW - dt.timedelta(seconds=3)),
        end_time_unix_nano=nanos(NOW),
    )
    child = Span(
        trace_id=bytes.fromhex("0af7651916cd43dd8448eb211c80319c"),
        span_id=bytes.fromhex("b7ad6b7169203331"),
        parent_span_id=bytes.fromhex("00f067aa0ba902b7"),
        name="chat",
        start_time_unix_nano=nanos(NOW - dt.timedelta(seconds=2)),
        end_time_unix_nano=nanos(NOW),
        attributes=[
            kv("gen_ai.system", string_value="anthropic"),
            kv("gen_ai.request.model", string_value="claude-sonnet-4-6"),
            kv("gen_ai.usage.input_tokens", int_value=1_000_000),
        ],
    )
    request = ExportTraceServiceRequest(
        resource_spans=[
            ResourceSpans(
                resource=Resource(attributes=[kv("service.name", string_value="triage")]),
                scope_spans=[ScopeSpans(spans=[root, child])],
            )
        ]
    )
    return request.SerializeToString()


def test_an_export_without_the_ingest_token_is_refused(client):
    resp = client.post(URL, json=json_export())
    assert resp.status_code == 401


def test_a_json_export_lands_as_a_priced_trace(client, token):
    resp = client.post(URL, json=json_export(), headers=auth(token))
    assert resp.status_code == 200
    # A clean export gets the empty ExportTraceServiceResponse.
    assert resp.json() == {}

    listing = client.get("/api/ai/traces").json()
    assert listing["traces"][0]["total_cost"] == pytest.approx(3.0)


def test_a_protobuf_export_is_answered_in_protobuf(client, token):
    # http/protobuf is what exporters send unless told otherwise.
    resp = client.post(
        URL,
        content=proto_export(),
        headers=auth(token, **{"Content-Type": "application/x-protobuf"}),
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-protobuf")
    ExportTraceServiceResponse().ParseFromString(resp.content)  # parses, or raises

    recent = client.get("/api/otel/recent").json()["trace"]
    assert recent["operation_name"] == "resolve-ticket"
    assert recent["span_count"] == 2


def test_a_gzipped_export_is_accepted(client, token):
    # The Collector's otlphttp exporter compresses by default.
    body = gzip.compress(json.dumps(json_export()).encode())
    resp = client.post(
        URL,
        content=body,
        headers=auth(token, **{"Content-Type": "application/json", "Content-Encoding": "gzip"}),
    )
    assert resp.status_code == 200


def test_a_decompression_bomb_is_stopped_at_the_output_bound(client, token):
    # A size limit on the compressed body alone would let a few kilobytes
    # inflate into gigabytes.
    bomb = gzip.compress(b"\0" * (40 * 1024 * 1024), compresslevel=9)
    assert len(bomb) < 200_000
    resp = client.post(
        URL,
        content=bomb,
        headers=auth(token, **{"Content-Type": "application/json", "Content-Encoding": "gzip"}),
    )
    assert resp.status_code == 413


def test_content_is_dropped_and_the_exporter_is_told(client, token):
    secret = [{"key": "gen_ai.prompt.0.content", "value": {"stringValue": "ACME-SECRET"}}]
    resp = client.post(URL, json=json_export(secret), headers=auth(token))
    assert resp.status_code == 200
    warning = resp.json()["partialSuccess"]["errorMessage"]
    assert "discarded" in warning
    # The warning names what happened, never what arrived.
    assert "ACME-SECRET" not in resp.text

    detail = client.get("/api/ai/traces").json()
    assert "ACME-SECRET" not in json.dumps(detail)


def test_an_unsupported_media_type_is_refused(client, token):
    resp = client.post(URL, content=b"hello", headers=auth(token, **{"Content-Type": "text/plain"}))
    assert resp.status_code == 415


def test_malformed_json_is_a_400_that_exporters_will_not_retry(client, token):
    resp = client.post(
        URL, content=b"{not json", headers=auth(token, **{"Content-Type": "application/json"})
    )
    assert resp.status_code == 400


def test_the_tenant_comes_from_the_token_never_the_payload(client, token):
    # A resource attribute claiming another tenant is simply not read.
    hostile = json_export()
    hostile["resourceSpans"][0]["resource"]["attributes"].append(
        {"key": "tenant_id", "value": {"stringValue": "00000000-0000-0000-0000-000000000000"}}
    )
    assert client.post(URL, json=hostile, headers=auth(token)).status_code == 200
    assert client.get("/api/ai/traces").json()["traces"]


def test_recent_needs_a_session_not_an_ingest_token(client, token):
    anonymous = TestClient(client.app)
    assert anonymous.get("/api/otel/recent", headers=auth(token)).status_code == 401
    assert client.get("/api/otel/recent").json() == {"trace": None}


def test_a_corrupt_gzip_body_is_a_400_not_too_large(client, token):
    # "Too large" would send someone to lower a batch size that was never the
    # problem.
    resp = client.post(
        URL,
        content=b"definitely not gzip",
        headers=auth(token, **{"Content-Type": "application/json", "Content-Encoding": "gzip"}),
    )
    assert resp.status_code == 400
    assert "gzip" in resp.json()["message"]


def test_an_unsupported_compression_is_a_415(client, token):
    resp = client.post(
        URL,
        content=b"{}",
        headers=auth(token, **{"Content-Type": "application/json", "Content-Encoding": "br"}),
    )
    assert resp.status_code == 415
