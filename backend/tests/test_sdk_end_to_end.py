"""The real Python SDK against the real ingest endpoint.

Every other SDK test uses a fake transport, which proves the SDK builds the
right events but not that the server accepts them. This posts what the SDK
actually constructs through the actual route, so a drift between the two
contracts fails here rather than on a customer's first install.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from meter.api import create_app
from meter.db import app_dsn, connect, tenant_tx

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdk" / "python"))

PASSWORD = "correct horse battery"


@pytest.fixture
def client(admin_conn, admin_conninfo, app_conninfo, monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "unit-test-secret-key")
    monkeypatch.setenv("DATABASE_URL", admin_conninfo)
    monkeypatch.setenv("DATABASE_APP_URL", app_conninfo)
    c = TestClient(create_app())
    c.post("/api/auth/signup", json={"email": "cto@acme.com", "password": PASSWORD})
    return c


@pytest.fixture
def sdk(client):
    """A real Meter whose transport posts through the real TestClient."""
    from costlyinfra_meter import Meter

    token = client.post("/api/hook/token").json()["token"]
    posted = []

    def transport(url, headers, body):
        payload = json.loads(body.decode())
        posted.append(payload)
        resp = client.post(
            "/api/hook/events",
            headers={"Authorization": headers["Authorization"]},
            json=payload,
        )
        # A non-2xx here means the SDK built something the server refuses.
        assert resp.status_code == 200, resp.text

    meter = Meter(
        application="support-agent",
        environment="production",
        ingest_url="http://testserver/api/hook/events",
        token=token,
        transport=transport,
        flush_interval=0.01,
    )
    return meter, posted


class Response:
    model = "claude-sonnet-4-6"
    usage = type(
        "U",
        (),
        {
            "input_tokens": 1000,
            "output_tokens": 200,
            "cache_read_input_tokens": 600,
            "cache_creation_input_tokens": 0,
        },
    )()


def rows(tenant_id, sql):
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        return conn.execute(sql).fetchall()


def tenant_of(client) -> str:
    return client.get("/api/auth/me").json()["tenant_id"]


def test_a_wrapped_call_becomes_a_trace_the_server_stored(client, sdk):
    meter, _ = sdk

    class Anthropic:
        class messages:
            @staticmethod
            def create(**kw):
                return Response()

    meter.wrap(Anthropic(), feature_id=None).messages.create(
        model="claude-sonnet-4-6", messages=[{"role": "user", "content": "SECRET"}]
    )
    assert meter.flush(timeout=5.0)

    body = client.get("/api/ai/traces").json()
    assert body["total"] == 1
    (trace,) = body["traces"]
    assert trace["live_status"] == "success"
    assert trace["total_tokens"] == 1200
    assert trace["total_cost"] > 0
    assert trace["application"]["slug"] == "support-agent"


def test_a_multi_step_agent_arrives_as_a_waterfall(client, sdk):
    meter, _ = sdk
    with meter.agent("resolve-ticket", customer_id="acme-1") as run:
        run.llm("classify", lambda: Response())
        run.tool("retrieve-documents", lambda: ["doc"])
        run.llm("generate-answer", lambda: Response())
    assert meter.flush(timeout=5.0)

    (listed,) = client.get("/api/ai/traces").json()["traces"]
    detail = client.get(f"/api/ai/traces/{listed['trace_id']}").json()

    assert detail["operation_name"] == "resolve-ticket"
    assert detail["live_status"] == "success"
    assert [s["operation_name"] for s in detail["spans"]] == [
        "classify",
        "retrieve-documents",
        "generate-answer",
    ]
    # Only the model calls cost anything.
    assert [s["amount"] > 0 for s in detail["spans"]] == [True, False, True]
    assert detail["llm_calls"] == 2


def test_the_sdk_and_the_hook_agree_on_the_money(client, sdk):
    meter, _ = sdk
    with meter.agent("resolve") as run:
        run.llm("answer", lambda: Response())
    assert meter.flush(timeout=5.0)

    tenant = tenant_of(client)
    (span_cost,) = rows(tenant, "SELECT SUM(amount) FROM ai_span")[0]
    (hook_cost,) = rows(tenant, "SELECT SUM(amount) FROM inference_cost WHERE source='hook'")[0]
    # Request-level evidence and the monthly total are two views of one number.
    assert span_cost == hook_cost > 0


def test_a_resumed_run_continues_one_trace_across_processes(client, sdk):
    meter, _ = sdk
    with meter.agent("resolve-ticket") as run:
        context = run.export_context()
        run.tool("enqueue", lambda: None)
    with meter.resume(context) as worker:
        worker.tool("process-document", lambda: None)
    assert meter.flush(timeout=5.0)

    body = client.get("/api/ai/traces").json()
    # One run, not two, even though two "processes" reported it.
    assert body["total"] == 1
    detail = client.get(f"/api/ai/traces/{body['traces'][0]['trace_id']}").json()
    assert {s["operation_name"] for s in detail["spans"]} == {"enqueue", "process-document"}


def test_nothing_the_sdk_sends_carries_content(client, sdk):
    meter, posted = sdk

    class Anthropic:
        class messages:
            @staticmethod
            def create(**kw):
                return Response()

    meter.wrap(Anthropic()).messages.create(
        messages=[{"role": "user", "content": "MY-SECRET-PROMPT"}], system="MY-SECRET-SYSTEM"
    )
    with meter.agent("resolve") as run:
        run.tool("search", lambda: ["MY-SECRET-DOCUMENT"])
    assert meter.flush(timeout=5.0)

    wire = json.dumps(posted)
    for secret in ("MY-SECRET-PROMPT", "MY-SECRET-SYSTEM", "MY-SECRET-DOCUMENT"):
        assert secret not in wire

    tenant = tenant_of(client)
    stored = json.dumps(rows(tenant, "SELECT * FROM ai_span"), default=str)
    for secret in ("MY-SECRET-PROMPT", "MY-SECRET-SYSTEM", "MY-SECRET-DOCUMENT"):
        assert secret not in stored


def test_a_replayed_batch_does_not_double_the_bill(client, sdk):
    meter, posted = sdk
    with meter.agent("resolve") as run:
        run.llm("answer", lambda: Response())
    assert meter.flush(timeout=5.0)

    tenant = tenant_of(client)
    before = rows(tenant, "SELECT SUM(amount) FROM inference_cost WHERE source='hook'")[0][0]

    # Exactly what a retry after an ambiguous timeout looks like.
    for payload in posted:
        resp = client.post(
            "/api/hook/events",
            headers={"Authorization": f"Bearer {meter.token}"},
            json=payload,
        )
        assert resp.status_code == 200

    after = rows(tenant, "SELECT SUM(amount) FROM inference_cost WHERE source='hook'")[0][0]
    assert after == before


def test_a_failing_agent_reports_error_without_the_exception(client, sdk):
    meter, posted = sdk
    with pytest.raises(RuntimeError):
        with meter.agent("resolve") as run:
            run.tool("explode", lambda: (_ for _ in ()).throw(RuntimeError("SECRET-TRACEBACK")))
    assert meter.flush(timeout=5.0)

    (trace,) = client.get("/api/ai/traces").json()["traces"]
    assert trace["live_status"] == "error"
    assert "SECRET-TRACEBACK" not in json.dumps(posted)
