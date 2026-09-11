"""Prompt Optimization's HTTP surface: consent needs a password, capture needs the
ingest token and both consents, content needs an audited request, and Meter staff
viewing a customer's account can do none of it."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from meter import assistant_facts, prompt_capture
from meter.api import create_app

PASSWORD = "correct horse battery"
ADMIN_EMAIL = "admin@costlyinfra.com"
SECRET = "ACME-SECRET-PROMPT"


@pytest.fixture
def client(admin_conn, admin_conninfo, app_conninfo, monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "unit-test-secret-key")
    monkeypatch.setenv("DATABASE_URL", admin_conninfo)
    monkeypatch.setenv("DATABASE_APP_URL", app_conninfo)
    monkeypatch.setenv("METER_ADMIN_EMAILS", ADMIN_EMAIL)
    monkeypatch.setenv("METER_DISCOVERY_BASE_URL", "https://api.groq.com/openai/v1")
    monkeypatch.setenv("METER_DISCOVERY_MODEL", "openai/gpt-oss-120b")
    monkeypatch.setenv("METER_DISCOVERY_API_KEY", "test-key")
    return TestClient(create_app())


def signup(c, email="cto@acme.com"):
    c.post("/api/auth/signup", json={"email": email, "password": PASSWORD})


def grant(c, password=PASSWORD, **over):
    status = c.get("/api/prompt-optimization/consent").json()
    body = {
        "password": password,
        "accepted_version": status["current_version"],
        "accepted_disclosure": status["current_disclosure"],
    }
    body.update(over)
    return c.post("/api/prompt-optimization/consent", json=body)


def sample(feature_id, **over):
    body = {
        "feature_id": feature_id,
        "prompt_id": "triage",
        "prompt_version": "v7",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "template": f"You are {SECRET}.",
        "input": [{"role": "user", "text": f"Ticket mentions {SECRET}"}],
        "output": f"{SECRET}: billing",
        "tokens_in": 1200,
        "tokens_out": 40,
    }
    body.update(over)
    return body


def post_sample(c, token, payload):
    return c.post(
        "/api/prompt-capture/samples", json=payload, headers={"Authorization": f"Bearer {token}"}
    )


def ready(c):
    """A signed-in customer with consent, one enabled feature, and an ingest token."""
    signup(c)
    feature = c.post("/api/features", json={"name": "Ticket triage"}).json()["id"]
    assert grant(c).status_code == 200
    enabled = c.put(f"/api/prompt-optimization/features/{feature}", json={"enabled": True})
    assert enabled.status_code == 200
    token = c.post("/api/hook/token").json()["token"]
    return feature, token


def test_consent_needs_the_right_password_and_a_wrong_one_does_not_sign_you_out(client):
    signup(client)
    wrong = grant(client, password="not my password")
    assert wrong.status_code == 403
    assert wrong.json()["detail"] == "That password is not correct."
    assert client.get("/api/prompt-optimization/consent").json()["consent"] is None
    assert client.get("/api/auth/me").status_code == 200

    right = grant(client)
    assert right.status_code == 200
    assert right.json()["consent"]["granted_by"] == "cto@acme.com"


def test_consent_to_a_stale_screen_is_refused(client):
    signup(client)
    assert grant(client, accepted_version="2020-01-01").status_code == 400
    stale = {"source": "meter", "provider": "Groq", "model": "yesterdays-model"}
    assert grant(client, accepted_disclosure=stale).status_code == 400


def test_capture_needs_the_ingest_token(client):
    signup(client)
    assert client.post("/api/prompt-capture/samples", json={}).status_code == 401


def test_a_sample_is_refused_with_a_reason_until_both_consents_hold(client):
    signup(client)
    feature = client.post("/api/features", json={"name": "Ticket triage"}).json()["id"]
    token = client.post("/api/hook/token").json()["token"]

    refused = post_sample(client, token, sample(feature))
    assert (refused.status_code, refused.json()["reason"]) == (403, "no_consent")

    assert grant(client).status_code == 200
    refused = post_sample(client, token, sample(feature))
    assert (refused.status_code, refused.json()["reason"]) == (403, "feature_not_enabled")

    client.put(f"/api/prompt-optimization/features/{feature}", json={"enabled": True})
    stored = post_sample(client, token, sample(feature))
    assert stored.status_code == 200 and stored.json()["stored"] is True

    opened = client.get(
        "/api/prompt-capture/open",
        params={"feature_id": feature},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert opened.json() == {"open": True, "reason": None}


def test_listing_carries_no_content_and_showing_it_is_audited(client):
    feature, token = ready(client)
    sample_id = post_sample(client, token, sample(feature)).json()["sample_id"]

    listed = client.get("/api/prompt-optimization/samples")
    assert listed.status_code == 200
    assert SECRET not in listed.text
    assert listed.json()["samples"][0]["sample_id"] == sample_id

    shown = client.get(f"/api/prompt-optimization/samples/{sample_id}/content")
    assert shown.status_code == 200
    assert SECRET in shown.json()["output"]

    audit = client.get("/api/prompt-optimization/audit")
    assert "sample_content_viewed" in {e["event"] for e in audit.json()["events"]}
    assert SECRET not in audit.text


def test_an_oversized_body_is_refused_before_it_is_parsed(client):
    feature, token = ready(client)
    resp = client.post(
        "/api/prompt-capture/samples",
        content=b"x" * (4 * prompt_capture.MAX_SAMPLE_BYTES + 1),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    assert resp.status_code == 413
    assert resp.json()["reason"] == "too_large"


def test_meter_staff_viewing_an_account_cannot_consent_capture_withdraw_or_read(client):
    feature, token = ready(client)
    sample_id = post_sample(client, token, sample(feature)).json()["sample_id"]
    customer_tenant = client.get("/api/auth/me").json()["tenant_id"]
    client.post("/api/auth/logout")

    signup(client, ADMIN_EMAIL)
    assert client.post(f"/api/admin/impersonate/{customer_tenant}").status_code == 200

    # Seeing whether consent stands is fine; acting on it or reading content is not.
    assert client.get("/api/prompt-optimization/consent").status_code == 200
    assert grant(client).status_code == 403
    assert (
        client.put(
            f"/api/prompt-optimization/features/{feature}", json={"enabled": False}
        ).status_code
        == 403
    )
    shown = client.get(f"/api/prompt-optimization/samples/{sample_id}/content")
    assert shown.status_code == 403
    assert SECRET not in shown.text
    assert client.delete("/api/prompt-optimization/consent").status_code == 403

    # And nothing the customer holds was touched.
    client.delete("/api/admin/impersonate")
    client.post("/api/auth/logout")
    client.post("/api/auth/login", json={"email": "cto@acme.com", "password": PASSWORD})
    assert len(client.get("/api/prompt-optimization/samples").json()["samples"]) == 1
    assert client.get("/api/prompt-optimization/consent").json()["capturing"] is True


def test_captured_content_never_reaches_traces_or_the_assistant(client):
    feature, token = ready(client)
    post_sample(client, token, sample(feature))
    tenant_id = client.get("/api/auth/me").json()["tenant_id"]

    assert SECRET not in client.get("/api/ai/traces").text
    snapshot = assistant_facts.snapshot(tenant_id, "prompts traces cost agents alerts refresh")
    assert SECRET not in json.dumps(snapshot, default=str)


def test_withdrawing_through_the_api_deletes_samples_and_closes_capture(client):
    feature, token = ready(client)
    post_sample(client, token, sample(feature))

    withdrawn = client.delete("/api/prompt-optimization/consent")
    assert withdrawn.status_code == 200
    assert withdrawn.json()["consent"] is None
    assert client.get("/api/prompt-optimization/samples").json()["samples"] == []
    refused = post_sample(client, token, sample(feature))
    assert (refused.status_code, refused.json()["reason"]) == (403, "no_consent")
