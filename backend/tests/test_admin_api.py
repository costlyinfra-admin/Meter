"""Internal admin portal: access gate, cross-tenant read, connectors, impersonation."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from meter.api import create_app

PASSWORD = "correct horse battery"
ADMIN_EMAIL = "admin@costlyinfra.com"


@pytest.fixture
def client(admin_conn, admin_conninfo, app_conninfo, monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "unit-test-secret-key")
    monkeypatch.setenv("DATABASE_URL", admin_conninfo)
    monkeypatch.setenv("DATABASE_APP_URL", app_conninfo)
    monkeypatch.setenv("METER_ADMIN_EMAILS", ADMIN_EMAIL)
    return TestClient(create_app())


def _signup(c, email):
    return c.post("/api/auth/signup", json={"email": email, "password": PASSWORD})


def test_admin_gate_blocks_unauth_and_non_admins(client):
    assert client.get("/api/admin/overview").status_code == 401  # not logged in
    _signup(client, "user@acme.com")  # a normal, non-allowlisted user
    assert client.get("/api/admin/overview").status_code == 403


def test_overview_and_customers_span_tenants(client):
    _signup(client, "user@acme.com")  # customer tenant
    client.post("/api/auth/logout")
    _signup(client, ADMIN_EMAIL)  # admin's own tenant

    ov = client.get("/api/admin/overview")
    assert ov.status_code == 200
    body = ov.json()
    assert body["total_customers"] >= 2
    assert set(body) == {
        "total_customers",
        "connected_customers",
        "pending_connections",
        "total_ai_spend",
        "total_opportunities",
        "total_verified_savings",
    }
    companies = {c["company"] for c in client.get("/api/admin/customers").json()}
    assert len(companies) >= 2


def test_connector_lifecycle_and_error_logging(client):
    _signup(client, "user@acme.com")
    tenant = client.get("/api/auth/me").json()["tenant_id"]
    client.post("/api/auth/logout")
    _signup(client, ADMIN_EMAIL)

    # Save a credential for the customer (encrypted via existing utilities).
    saved = client.post(
        f"/api/admin/customers/{tenant}/connectors",
        json={"connector_type": "github", "secret": "ghp_x", "label": "acme"},
    )
    assert saved.status_code == 200
    detail = client.get(f"/api/admin/customers/{tenant}").json()
    assert any(c["type"] == "github" and c["connected"] for c in detail["connectors"])

    # Test a connector with no stored credential -> fast error, logged (no network).
    res = client.post(f"/api/admin/customers/{tenant}/connectors/anthropic/test").json()
    assert res["status"] == "error"
    errs = client.get("/api/admin/errors").json()
    assert any(e["connector_type"] == "anthropic" and e["status"] == "error" for e in errs)
    assert len(client.get("/api/admin/sync-history").json()) >= 1

    # Disconnect removes the credential.
    assert client.delete(f"/api/admin/customers/{tenant}/connectors/github").status_code == 204
    detail2 = client.get(f"/api/admin/customers/{tenant}").json()
    assert not any(c["type"] == "github" and c["connected"] for c in detail2["connectors"])


def test_impersonation_switches_tenant_context(client):
    # A customer with a feature.
    _signup(client, "user@acme.com")
    client.post("/api/features", json={"name": "Widget triage"})
    customer_tenant = client.get("/api/auth/me").json()["tenant_id"]
    client.post("/api/auth/logout")

    # Admin logs in and impersonates the customer.
    _signup(client, ADMIN_EMAIL)
    assert "Widget triage" not in {f["name"] for f in client.get("/api/features").json()}
    imp = client.post(f"/api/admin/impersonate/{customer_tenant}")
    assert imp.status_code == 200

    # The whole customer UI now operates in that tenant — no duplication.
    feats = {f["name"] for f in client.get("/api/features").json()}
    assert "Widget triage" in feats
    me = client.get("/api/auth/me").json()
    assert me["is_admin"] is True
    assert me["impersonating"]["tenant_id"] == customer_tenant

    # Exit impersonation restores the admin's own context.
    assert client.delete("/api/admin/impersonate").status_code == 204
    assert client.get("/api/auth/me").json()["impersonating"] is None
    assert "Widget triage" not in {f["name"] for f in client.get("/api/features").json()}


def test_non_admin_cannot_impersonate(client):
    _signup(client, "user@acme.com")
    tenant = client.get("/api/auth/me").json()["tenant_id"]
    # A non-admin hitting the impersonate endpoint is refused...
    assert client.post(f"/api/admin/impersonate/{tenant}").status_code == 403
    # ...and /auth/me never reports admin powers for them.
    assert client.get("/api/auth/me").json()["is_admin"] is False
