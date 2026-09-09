"""End-to-end API: register a self-hosted pool -> meter usage -> allocate cost."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from fastapi.testclient import TestClient
from meter.api import create_app

PASSWORD = "correct horse battery"


@pytest.fixture
def client(admin_conn, admin_conninfo, app_conninfo, monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "unit-test-secret-key")
    monkeypatch.setenv("DATABASE_URL", admin_conninfo)
    monkeypatch.setenv("DATABASE_APP_URL", app_conninfo)
    c = TestClient(create_app())
    c.post("/api/auth/signup", json={"email": "cto@acme.com", "password": PASSWORD})
    return c


# The SDK reports live, so an event's timestamp is clamped to a believable
# window — a wrong clock must not move real spend into a period nobody looks at.
# This test therefore meters into the CURRENT month rather than a fixed one.
NOW = dt.datetime.now(dt.timezone.utc)
PERIOD = NOW.strftime("%Y-%m")


def test_register_pool_meter_and_allocate(client):
    # Register a self-hosted GPU pool with a monthly infra bill.
    pool = client.post(
        "/api/compute/pools",
        json={"name": "Llama GPU pool", "provider_label": "self_hosted", "monthly_cost": 1000},
    ).json()
    assert pool["monthly_cost"] == 1000.0

    listed = client.get("/api/compute/pools").json()
    assert [p["name"] for p in listed] == ["Llama GPU pool"]

    # A feature to attribute to.
    triage = client.post("/api/features", json={"name": "AI threat triage"}).json()

    # Meter usage via the hook ingest token (server-to-server, like the SDK).
    token = client.post("/api/hook/token").json()["token"]
    client.post(
        "/api/hook/events",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "events": [
                {
                    "event_type": "span.completed",
                    "trace_id": f"t-{uuid.uuid4().hex[:8]}",
                    "span_id": "s1",
                    "span_kind": "llm",
                    "operation_name": "summarize",
                    "application": "support-agent",
                    "provider": "self_hosted",
                    "model": "llama-3.1-70b",
                    "tokens_in": 1_000_000,
                    "tokens_out": 0,
                    "feature_id": triage["id"],
                    "occurred_at": NOW.isoformat(),
                }
            ]
        },
    )

    # Allocate the pool's bill across features by usage share.
    result = client.post("/api/compute/allocate", json={"period": PERIOD}).json()
    assert result[0]["allocated"] == 1000.0

    # The feature now shows the self-hosted inference cost on the dashboard.
    dash = client.get("/api/dashboard", params={"period": PERIOD}).json()
    triage_row = next(f for f in dash["features"] if f["name"] == "AI threat triage")
    assert triage_row["inference_cost"] == 1000.0
    assert triage_row["confidence"] == "med"  # allocation, not a metered price


def test_pool_requires_auth(client):
    client.post("/api/auth/logout")
    assert client.get("/api/compute/pools").status_code == 401
