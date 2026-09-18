"""Per-feature usage, through the routes the screens actually call.

Cost per user and the "Worth it?" indicator are computed from `feature_usage`,
and no connector supplies it until product-analytics connectors land. The table,
the service and the route all existed for months while nothing in the product
called any of them, so these tests go through HTTP rather than the service.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from meter import features
from meter.api import create_app

PASSWORD = "correct horse battery"
PERIOD = dt.date.today().replace(day=1)
MONTH = PERIOD.strftime("%Y-%m")


@pytest.fixture
def client(admin_conn, admin_conninfo, app_conninfo, monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "unit-test-secret-key")
    monkeypatch.setenv("DATABASE_URL", admin_conninfo)
    monkeypatch.setenv("DATABASE_APP_URL", app_conninfo)
    c = TestClient(create_app())
    c.post("/api/auth/signup", json={"email": "cto@acme.com", "password": PASSWORD})
    return c


def tenant_of(client) -> str:
    return client.get("/api/auth/me").json()["tenant_id"]


def test_a_csv_of_active_users_reaches_the_dashboard(client):
    """The whole point: a blank column fills."""
    tenant = tenant_of(client)
    features.add_feature(tenant, "AI threat triage")

    resp = client.post(
        "/api/features/usage/import",
        json={"csv": "feature,active_users\nAI threat triage,540\n", "period": MONTH},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["imported"] == 1

    row = next(
        f
        for f in client.get("/api/dashboard").json()["features"]
        if f["name"] == "AI threat triage"
    )
    assert row["active_users"] == 540


def test_setting_one_feature_by_hand_reaches_the_dashboard_too(client):
    tenant = tenant_of(client)
    feature = features.add_feature(tenant, "Report generator")

    resp = client.put(
        f"/api/features/{feature['id']}/usage", json={"active_users": 120, "period": MONTH}
    )
    assert resp.status_code == 200, resp.text

    row = next(
        f
        for f in client.get("/api/dashboard").json()["features"]
        if f["name"] == "Report generator"
    )
    assert row["active_users"] == 120


def test_a_bad_row_is_refused_with_the_row_number(client):
    tenant = tenant_of(client)
    features.add_feature(tenant, "AI threat triage")

    resp = client.post(
        "/api/features/usage/import",
        json={"csv": "feature,active_users\nAI threat triage,540\nGhost feature,7\n"},
    )
    assert resp.status_code == 400
    assert "Row 3" in resp.json()["detail"]

    # And nothing was written: a refused import leaves the month as it was.
    row = next(
        f
        for f in client.get("/api/dashboard").json()["features"]
        if f["name"] == "AI threat triage"
    )
    assert row["active_users"] is None


def test_usage_cannot_be_imported_for_another_tenant_s_feature(
    client, app_conninfo, admin_conninfo
):
    """The feature name is the addressing key, so names must not cross tenants."""
    other = TestClient(create_app())
    other.post("/api/auth/signup", json={"email": "cfo@other.example", "password": PASSWORD})
    features.add_feature(tenant_of(other), "Their feature")

    resp = client.post(
        "/api/features/usage/import",
        json={"csv": "feature,active_users\nTheir feature,999\n"},
    )
    assert resp.status_code == 400
    assert "no feature called" in resp.json()["detail"]


def test_an_import_needs_a_session(client):
    client.post("/api/auth/logout")
    resp = client.post("/api/features/usage/import", json={"csv": "feature,active_users\nA,1\n"})
    assert resp.status_code in (401, 403)
