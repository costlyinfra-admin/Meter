"""Organization settings: tenant-level org profile + privacy, save/load + validation."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from meter.api import create_app

GOOD_PASSWORD = "correct horse battery"


@pytest.fixture
def client(admin_conn, admin_conninfo, app_conninfo, monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "unit-test-secret-key")
    monkeypatch.setenv("DATABASE_URL", admin_conninfo)
    monkeypatch.setenv("DATABASE_APP_URL", app_conninfo)
    c = TestClient(create_app())
    return c


def _signup(client, email="cto@acme.com"):
    resp = client.post("/api/auth/signup", json={"email": email, "password": GOOD_PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_defaults_for_existing_tenant_are_safe(client):
    # A tenant that never touched Settings still loads with sensible defaults.
    _signup(client)
    s = client.get("/api/settings").json()
    assert s["timezone"] == "UTC"
    assert s["currency"] == "USD"
    assert s["customer_id_storage"] == "hashed"  # most private default
    assert s["store_prompts"] is False  # privacy-conscious default
    assert s["data_retention"] == "indefinite"  # never auto-delete by default


def test_signup_initializes_org_name_from_work_domain(client):
    _signup(client, email="alessio@transilienceai.com")
    assert client.get("/api/settings").json()["org_name"] == "Transilience AI"


def test_signup_personal_email_does_not_generate_bad_org_name(client):
    _signup(client, email="jane@gmail.com")
    # Safe fallback, never a strange inferred company name.
    assert client.get("/api/settings").json()["org_name"] == "jane's workspace"


def test_org_name_save_and_load(client):
    _signup(client)
    updated = client.patch("/api/settings", json={"org_name": "  Transilience AI  "}).json()
    assert updated["org_name"] == "Transilience AI"  # trimmed
    assert client.get("/api/settings").json()["org_name"] == "Transilience AI"


def test_timezone_and_currency_save_and_load(client):
    _signup(client)
    updated = client.patch(
        "/api/settings", json={"timezone": "America/Los_Angeles", "currency": "usd"}
    ).json()
    assert updated["timezone"] == "America/Los_Angeles"
    assert updated["currency"] == "USD"  # normalized upper-case


def test_privacy_settings_save_and_load(client):
    _signup(client)
    updated = client.patch(
        "/api/settings",
        json={
            "customer_id_storage": "aliases",
            "store_prompts": True,
            "data_retention": "90d",
        },
    ).json()
    assert updated["customer_id_storage"] == "aliases"
    assert updated["store_prompts"] is True
    assert updated["data_retention"] == "90d"
    reloaded = client.get("/api/settings").json()
    assert reloaded["customer_id_storage"] == "aliases"
    assert reloaded["data_retention"] == "90d"


def test_partial_patch_leaves_other_fields_untouched(client):
    _signup(client)
    client.patch("/api/settings", json={"timezone": "Asia/Kolkata"})
    client.patch("/api/settings", json={"org_name": "Acme Security"})
    s = client.get("/api/settings").json()
    assert s["timezone"] == "Asia/Kolkata"  # not reset by the second patch
    assert s["org_name"] == "Acme Security"


@pytest.mark.parametrize(
    "payload",
    [
        {"org_name": "   "},  # empty after trim
        {"org_name": "x" * 201},  # too long
        {"timezone": "Mars/Phobos"},  # not a supported IANA zone
        {"currency": "EUR"},  # unsupported currency
        {"customer_id_storage": "plaintext"},  # not an allowed mode
        {"data_retention": "forever"},  # not an allowed window
    ],
)
def test_invalid_values_are_rejected(client, payload):
    _signup(client)
    before = client.get("/api/settings").json()
    resp = client.patch("/api/settings", json=payload)
    assert resp.status_code == 400
    # Nothing was written on rejection.
    assert client.get("/api/settings").json() == before


def test_settings_require_auth(client):
    assert client.get("/api/settings").status_code == 401
    assert client.patch("/api/settings", json={"org_name": "X"}).status_code == 401


def test_settings_are_tenant_scoped(client):
    # Two tenants; each sees only its own organization name.
    _signup(client, email="a@acme.com")
    client.patch("/api/settings", json={"org_name": "Acme"})
    client.post("/api/auth/logout")
    _signup(client, email="b@globex.com")
    assert client.get("/api/settings").json()["org_name"] == "Globex"  # inferred, not "Acme"
