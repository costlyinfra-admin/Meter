"""An optimization signal, through the route a customer's SDK actually posts to.

Every other test of the measured levers calls `hook.ingest_events` directly — a
function no HTTP request can reach, because `POST /api/hook/events` routes to
`traces.ingest`. So the detector maths was well covered while the question of
whether a signal can ARRIVE was not covered at all, which is how the SDK came to
stop sending them without a single test noticing.

These go through the real route.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from meter import features, optimize_measured
from meter.api import create_app
from meter.db import app_dsn, connect, tenant_tx

PASSWORD = "correct horse battery"
PERIOD = dt.date.today().replace(day=1)
NOW = dt.datetime.now(dt.timezone.utc)


@pytest.fixture
def client(admin_conn, admin_conninfo, app_conninfo, monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "unit-test-secret-key")
    monkeypatch.setenv("DATABASE_URL", admin_conninfo)
    monkeypatch.setenv("DATABASE_APP_URL", app_conninfo)
    c = TestClient(create_app())
    c.post("/api/auth/signup", json={"email": "cto@acme.com", "password": PASSWORD})
    return c


def _post(client, token, events):
    resp = client.post(
        "/api/hook/events",
        headers={"Authorization": f"Bearer {token}"},
        json={"events": events},
    )
    assert resp.status_code == 200, resp.text
    return resp


def _span(trace, span, signal, **over):
    base = {
        "event_type": "span.completed",
        "trace_id": trace,
        "span_id": span,
        "span_kind": "llm",
        "application": "support-agent",
        "operation_name": "classify",
        "environment": "production",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "tokens_in": 1000,
        "tokens_out": 100,
        "occurred_at": NOW.isoformat(),
        "signal": signal,
    }
    base.update(over)
    return base


def _signals(tenant_id):
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        return conn.execute(
            "SELECT signal_kind, fingerprint, call_count, cached_count, prefix_tokens "
            "FROM usage_signal ORDER BY signal_kind"
        ).fetchall()


def test_a_duplicate_signal_survives_the_real_route(client):
    tenant = client.get("/api/auth/me").json()["tenant_id"]
    token = client.post("/api/hook/token").json()["token"]

    _post(
        client,
        token,
        [
            {
                "event_type": "trace.started",
                "trace_id": "t1",
                "application": "support-agent",
                "operation_name": "resolve",
                "occurred_at": NOW.isoformat(),
            },
            _span("t1", "s1", {"kind": "duplicate", "fingerprint": "fp-dup", "count": 2}),
        ],
    )

    rows = _signals(tenant)
    assert rows, "a signal posted to the live route never reached usage_signal"
    kind, fingerprint, call_count, _cached, _prefix = rows[0]
    assert (kind, fingerprint) == ("duplicate", "fp-dup")
    assert call_count == 2


def test_a_prefix_signal_carries_its_cached_calls(client):
    """The field that decides the dollars.

    `cacheable = call_count - cached_count`, so a cached count that never
    arrives makes every already-cached call look like an available saving. The
    validator and the accumulator used different names for this field, which is
    exactly the kind of gap a route-level test catches and a unit test does not.
    """
    tenant = client.get("/api/auth/me").json()["tenant_id"]
    token = client.post("/api/hook/token").json()["token"]

    _post(
        client,
        token,
        [
            {
                "event_type": "trace.started",
                "trace_id": "t2",
                "application": "support-agent",
                "operation_name": "resolve",
                "occurred_at": NOW.isoformat(),
            },
            _span(
                "t2",
                "s2",
                {
                    "kind": "prefix",
                    "fingerprint": "fp-prefix",
                    "count": 100,
                    "cached_count": 40,
                    "prefix_tokens": 4000,
                    "tokens_in": 100_000,
                    "tokens_out": 1_000,
                },
            ),
        ],
    )

    rows = _signals(tenant)
    kind, fingerprint, call_count, cached, prefix_tokens = rows[0]
    assert (kind, fingerprint) == ("prefix", "fp-prefix")
    assert call_count == 100
    assert cached == 40, "cached calls were dropped between the validator and the store"
    assert prefix_tokens == 4000


def test_the_measured_lever_fires_on_a_signal_that_arrived_this_way(client):
    """Not just stored — actually reaching the screen that sells the product."""
    tenant = client.get("/api/auth/me").json()["tenant_id"]
    token = client.post("/api/hook/token").json()["token"]
    feature = features.add_feature(tenant, "AI threat triage")["id"]

    events = [
        {
            "event_type": "trace.started",
            "trace_id": "t3",
            "application": "support-agent",
            "operation_name": "resolve",
            "feature_id": feature,
            "occurred_at": NOW.isoformat(),
        }
    ]
    for i in range(3):
        events.append(
            _span(
                "t3",
                f"s3-{i}",
                {"kind": "duplicate", "fingerprint": "fp-repeat", "count": 1},
                feature_id=feature,
                tokens_in=1_000_000,
            )
        )
    _post(client, token, events)

    result = optimize_measured.opportunities(tenant, feature, PERIOD)
    levers = {o["lever"]: o for o in result["opportunities"]}
    assert "duplicate_calls" in levers, "a real signal produced no duplicate-call finding"
    assert levers["duplicate_calls"]["savings_type"] == "measured"
    assert levers["duplicate_calls"]["projected_monthly_savings"] > 0
