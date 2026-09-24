"""The hosted MCP endpoint.

Two things are being checked. The transport is MCP's Streamable HTTP, so it must
behave the way the specification says a stateless, non-streaming server behaves
— otherwise a client that follows the spec fails against it. And it is reachable
from the open internet, so what it refuses matters at least as much as what it
answers.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient
from meter.api import create_app
from meter.mcp import audit, limits, tokens
from meter.sampledata import insert_sample_data

PASSWORD = "correct horse battery"
MONTH = "2026-05"
PERIOD = dt.date(2026, 5, 1)

#: The header pair a spec-following client sends on every POST.
SPEC_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2025-06-18",
}


@pytest.fixture(autouse=True)
def _fresh_rate_window():
    limits.reset_rate()


@pytest.fixture
def client(admin_conn, admin_conninfo, app_conninfo, read_conninfo, monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "unit-test-secret-key")
    monkeypatch.setenv("DATABASE_URL", admin_conninfo)
    monkeypatch.setenv("DATABASE_APP_URL", app_conninfo)
    # The endpoint narrows itself to the SELECT-only role for each call, so a
    # deployment that serves MCP must configure one. See test_mcp_read_role.py.
    monkeypatch.setenv("DATABASE_READ_URL", read_conninfo)
    monkeypatch.delenv("METER_APP_ORIGIN", raising=False)
    c = TestClient(create_app())
    c.post("/api/auth/signup", json={"email": "cto@acme.com", "password": PASSWORD})
    return c


@pytest.fixture
def tenant_with_data(client, admin_conn):
    tenant = client.get("/api/auth/me").json()
    tenant_id = None
    row = admin_conn.execute("SELECT tenant_id FROM app_user WHERE email = 'cto@acme.com'")
    tenant_id = str(row.fetchone()[0])
    insert_sample_data(admin_conn, tenant_id, extended=True)
    admin_conn.commit()
    assert tenant
    return tenant_id


@pytest.fixture
def token(client, tenant_with_data):
    made = client.post("/api/mcp/tokens", json={"label": "laptop"})
    assert made.status_code == 201
    return made.json()["token"]


def rpc(client, body, token=None, **headers):
    sent = dict(SPEC_HEADERS)
    if token:
        sent["Authorization"] = f"Bearer {token}"
    sent.update(headers)
    return client.post("/api/mcp", json=body, headers=sent)


def call(client, token, name, arguments=None):
    reply = rpc(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": name, "arguments": arguments or {}}},
        token,
    )
    assert reply.status_code == 200, reply.text
    result = reply.json()["result"]
    return {"payload": json.loads(result["content"][0]["text"]), "is_error": result["isError"]}


# ---------------------------------------------------------------------------
# The transport, as the specification describes it
# ---------------------------------------------------------------------------
def test_a_request_gets_one_json_response(client, token):
    reply = rpc(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18"}},
        token,
    )
    assert reply.status_code == 200
    assert reply.headers["content-type"].startswith("application/json")
    body = reply.json()
    assert body["id"] == 1
    assert body["result"]["serverInfo"]["name"] == "meter"


def test_a_notification_gets_202_and_no_body(client, token):
    reply = rpc(client, {"jsonrpc": "2.0", "method": "notifications/initialized"}, token)
    # The spec is explicit: 202 Accepted, empty body.
    assert reply.status_code == 202
    assert reply.content == b""


def test_get_is_405_because_there_is_no_stream(client):
    # The spec's alternative to opening an SSE stream. A client that tries will
    # fall back to POST-only, which is all this server needs.
    assert client.get("/api/mcp").status_code == 405


def test_delete_is_405_because_there_is_no_session(client):
    assert client.delete("/api/mcp").status_code == 405


def test_an_unsupported_protocol_version_is_400(client, token):
    reply = rpc(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        token,
        **{"MCP-Protocol-Version": "1999-01-01"},
    )
    assert reply.status_code == 400
    assert "1999-01-01" in reply.text


def test_a_missing_protocol_version_is_allowed(client, token):
    # Absent means an older client, not an unsupported one. The spec says to
    # assume 2025-03-26 rather than refuse.
    reply = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    assert reply.status_code == 200
    assert len(reply.json()["result"]["tools"]) == 3


def test_every_supported_version_is_accepted(client, token):
    from meter.mcp import http as mcp_http

    for version in mcp_http.SUPPORTED_PROTOCOLS:
        reply = rpc(
            client,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            token,
            **{"MCP-Protocol-Version": version},
        )
        assert reply.status_code == 200, version


def test_a_batch_is_refused_rather_than_crashing(client, token):
    # Batching was removed from the protocol; a list here is a client bug.
    reply = rpc(client, [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}], token)
    assert reply.status_code == 400


def test_unparseable_json_is_400(client, token):
    reply = client.post(
        "/api/mcp",
        content=b"not json",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    assert reply.status_code == 400


# ---------------------------------------------------------------------------
# What it refuses
# ---------------------------------------------------------------------------
def test_no_credential_is_401(client):
    reply = rpc(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert reply.status_code == 401
    # So a client knows what kind of credential to offer.
    assert reply.headers["www-authenticate"] == "Bearer"


def test_a_made_up_token_is_401(client, tenant_with_data):
    reply = rpc(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, "mtr_mcp_invented")
    assert reply.status_code == 401


def test_a_revoked_token_is_401(client, token):
    listed = client.get("/api/mcp/tokens").json()
    assert client.delete(f"/api/mcp/tokens/{listed[0]['id']}").status_code == 204
    reply = rpc(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token)
    assert reply.status_code == 401


def test_a_browser_session_is_not_a_credential_here(client, tenant_with_data):
    # `client` is signed in — every other endpoint would answer it. This one
    # must not, or any page the user visits could drive it with their cookie.
    assert client.get("/api/auth/me").status_code == 200
    reply = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers=SPEC_HEADERS,
    )
    assert reply.status_code == 401


def test_a_request_from_a_web_page_is_refused(client, token):
    # DNS rebinding, which the spec requires validating Origin against. A real
    # MCP client is not a browser and sends no Origin at all.
    reply = rpc(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token,
                Origin="https://evil.example")
    assert reply.status_code == 403


def test_the_apps_own_origin_is_allowed(client, token, monkeypatch):
    monkeypatch.setenv("METER_APP_ORIGIN", "https://meter.costlyinfra.com")
    reply = rpc(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token,
                Origin="https://meter.costlyinfra.com")
    assert reply.status_code == 200


# ---------------------------------------------------------------------------
# It answers with the caller's own data, and only theirs
# ---------------------------------------------------------------------------
def test_it_returns_this_organizations_numbers(client, token):
    from meter import dashboard

    got = call(client, token, "get_cost_summary", {"start": MONTH})
    assert not got["is_error"]
    tenant_id = tokens.resolve(token).tenant_id
    assert got["payload"]["totals"] == dashboard.dashboard(tenant_id, PERIOD)["totals"]


def test_a_token_cannot_reach_another_organization(client, token, admin_conn):
    other = str(
        admin_conn.execute("INSERT INTO tenant (name) VALUES ('Other') RETURNING id").fetchone()[0]
    )
    insert_sample_data(admin_conn, other)
    admin_conn.commit()
    theirs = tokens.create(other, "their laptop")

    mine = call(client, token, "get_cost_summary", {"start": MONTH})["payload"]
    yours = call(client, theirs["token"], "get_cost_summary", {"start": MONTH})["payload"]
    assert mine["totals"] != yours["totals"]


def test_a_flood_is_slowed_down_over_http(client, token):
    for _ in range(limits.RATE_LIMIT):
        assert not call(client, token, "get_cost_summary", {"start": MONTH})["is_error"]
    stopped = call(client, token, "get_cost_summary", {"start": MONTH})
    assert stopped["is_error"]
    assert "Too many" in stopped["payload"]["error"]


# ---------------------------------------------------------------------------
# The audit trail
# ---------------------------------------------------------------------------
def test_every_call_is_recorded_against_its_token(client, token):
    call(client, token, "get_cost_summary", {"start": MONTH, "group_by": "provider"})
    entries = client.get("/api/mcp/activity").json()

    assert len(entries) == 1
    only = entries[0]
    assert only["tool"] == "get_cost_summary"
    assert only["transport"] == "http"
    assert only["outcome"] == audit.OK
    assert only["token_label"] == "laptop"
    # The question it asked, which is what makes the trail worth keeping.
    assert "provider" in only["arguments"]
    assert only["duration_ms"] >= 0


def test_a_failed_call_is_recorded_as_failed(client, token):
    call(client, token, "get_cost_summary", {"group_by": "galaxy"})
    assert client.get("/api/mcp/activity").json()[0]["outcome"] == audit.ERROR


def test_being_rate_limited_is_recorded_as_such(client, token):
    for _ in range(limits.RATE_LIMIT + 1):
        call(client, token, "get_cost_summary", {"start": MONTH})
    assert client.get("/api/mcp/activity").json()[0]["outcome"] == audit.RATE_LIMITED


def test_the_trail_records_the_question_and_never_the_answer(client, token):
    got = call(client, token, "get_cost_summary", {"start": MONTH})
    figure = str(got["payload"]["totals"]["inference_cost"])
    only = client.get("/api/mcp/activity").json()[0]
    # Results would be a second copy of the customer's cost data with its own
    # retention story. The arguments are the shape of the question.
    assert figure not in json.dumps(only)


def test_revoking_a_token_keeps_what_it_did(client, token):
    call(client, token, "get_cost_summary", {"start": MONTH})
    listed = client.get("/api/mcp/tokens").json()
    client.delete(f"/api/mcp/tokens/{listed[0]['id']}")
    # "It was used until Tuesday" is the answer to a security question.
    assert client.get("/api/mcp/activity").json()[0]["tool"] == "get_cost_summary"


def test_a_tenant_sees_only_its_own_trail(client, token, admin_conn):
    other = str(
        admin_conn.execute("INSERT INTO tenant (name) VALUES ('Other') RETURNING id").fetchone()[0]
    )
    insert_sample_data(admin_conn, other)
    admin_conn.commit()
    theirs = tokens.create(other, "their laptop")
    call(client, theirs["token"], "get_cost_summary", {"start": MONTH})

    assert client.get("/api/mcp/activity").json() == []


# ---------------------------------------------------------------------------
# Managing tokens from the app
# ---------------------------------------------------------------------------
def test_minting_needs_a_signed_in_person(client, tenant_with_data):
    client.post("/api/auth/logout")
    assert client.post("/api/mcp/tokens", json={"label": "x"}).status_code == 401
    assert client.get("/api/mcp/tokens").status_code == 401
    assert client.get("/api/mcp/activity").status_code == 401


def test_an_mcp_token_cannot_mint_another(client, token, tenant_with_data):
    # A credential that can issue credentials is an escalation.
    client.post("/api/auth/logout")
    reply = client.post(
        "/api/mcp/tokens",
        json={"label": "second"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert reply.status_code == 401


def test_the_listing_never_carries_the_token_itself(client, token):
    listed = client.get("/api/mcp/tokens").json()
    assert listed and all("token" not in row for row in listed)
    assert token not in json.dumps(listed)


def test_the_listing_says_how_much_each_token_is_used(client, token):
    call(client, token, "get_cost_summary", {"start": MONTH})
    call(client, token, "find_optimization_opportunities", {"period": MONTH})
    row = client.get("/api/mcp/tokens").json()[0]
    assert row["recent_calls"] == 2
    assert row["last_used_at"]


def test_a_label_is_required(client, tenant_with_data):
    assert client.post("/api/mcp/tokens", json={"label": ""}).status_code == 422


def test_one_organization_cannot_revoke_anothers_token(client, tenant_with_data, admin_conn):
    other = str(
        admin_conn.execute("INSERT INTO tenant (name) VALUES ('Other') RETURNING id").fetchone()[0]
    )
    admin_conn.commit()
    theirs = tokens.create(other, "their laptop")
    assert client.delete(f"/api/mcp/tokens/{theirs['id']}").status_code == 404
    assert tokens.resolve(theirs["token"]) is not None


# ---------------------------------------------------------------------------
# It reads as the read-only role, not as the API's own
# ---------------------------------------------------------------------------
def test_a_call_runs_on_a_connection_that_cannot_write(client, token, monkeypatch):
    # The endpoint lives inside the API process, which CAN write. Without this
    # the hosted path — the one customers use — would be the weaker one.
    import psycopg
    from meter import db

    # Patched at psycopg, not at db.connect: every service imported `connect`
    # by name, so patching the module attribute would catch none of them and
    # this test would pass on an empty list.
    seen = []
    real = psycopg.connect

    def watched(conninfo, **kwargs):
        seen.append(conninfo)
        return real(conninfo, **kwargs)

    monkeypatch.setattr(db.psycopg, "connect", watched)
    call(client, token, "get_cost_summary", {"start": MONTH})

    # The first connection is the API authenticating the token as itself —
    # resolving a credential is not part of the call it authorizes, and happens
    # before the narrowing. Everything the tool then does is read-only.
    authenticating, *reading = seen
    assert "user=meter_app" in authenticating
    assert reading, "the tool opened no connection at all"
    assert all(f"user={db.READ_ROLE}" in dsn for dsn in reading), reading


def test_an_mcp_request_does_not_wedge_the_rest_of_the_api(client, token):
    from meter import db

    call(client, token, "get_cost_summary", {"start": MONTH})
    # The narrowing is per call. Whether the ContextVar were reset or not, the
    # threadpool copies the context — so this cannot distinguish a missing
    # reset, and test_mcp_read_role.py tests the scope directly. What it does
    # prove is the thing a customer would notice: the API still writes.
    assert not db.is_read_only()
    assert client.post("/api/mcp/tokens", json={"label": "second"}).status_code == 201


def test_a_deploy_with_no_read_credential_says_so(client, token, monkeypatch):
    monkeypatch.delenv("DATABASE_READ_URL", raising=False)
    monkeypatch.delenv("METER_READ_DB_PASSWORD", raising=False)
    reply = rpc(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token)
    # One clear answer, not "get_cost_summary failed" on every call.
    assert reply.status_code == 503
    assert "not configured" in reply.text
