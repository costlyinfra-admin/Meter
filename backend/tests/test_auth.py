"""M2 acceptance: signup creates a tenant + session, logout/login works, and
connector credentials are stored encrypted at rest.
"""

from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient
from meter import auth, crypto
from meter.api import create_app

GOOD_PASSWORD = "correct horse battery"


@pytest.mark.parametrize(
    "email, expected",
    [
        ("alessio@transilienceai.com", "Transilience AI"),  # trailing "ai" -> " AI"
        ("cto@acme.com", "Acme"),
        ("dev@acme-corp.io", "Acme Corp"),  # hyphens become words
        ("x@openai.com", "Open AI"),
        ("someone@gmail.com", None),  # personal mailbox -> no inference
        ("u@outlook.com", None),
        ("u@yahoo.co.uk", None),
        ("noatsign", None),  # unparseable
    ],
)
def test_infer_org_name(email, expected):
    assert auth.infer_org_name(email) == expected


def test_default_tenant_name_falls_back_safely_for_personal_email():
    # No confident company name -> a safe, non-strange fallback (not machine noise).
    assert auth._default_tenant_name("jane@gmail.com") == "jane's workspace"


@pytest.fixture
def client(admin_conn, admin_conninfo, app_conninfo, monkeypatch):
    # admin_conn applies migrations; wire the API at the ephemeral DB.
    monkeypatch.setenv("APP_SECRET_KEY", "unit-test-secret-key")
    monkeypatch.setenv("DATABASE_URL", admin_conninfo)  # admin role: auth
    monkeypatch.setenv("DATABASE_APP_URL", app_conninfo)  # app role: tenant data
    return TestClient(create_app())


def test_signup_creates_tenant_and_logs_in(client):
    resp = client.post(
        "/api/auth/signup", json={"email": "CTO@Acme.com", "password": GOOD_PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    user = resp.json()
    assert user["email"] == "cto@acme.com"  # normalized to lower-case
    assert user["tenant_id"] and user["id"]

    # Session is live: /me returns the same user.
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["id"] == user["id"]


def test_logout_then_login(client):
    client.post("/api/auth/signup", json={"email": "a@b.com", "password": GOOD_PASSWORD})

    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/auth/me").status_code == 401  # session cleared

    login = client.post("/api/auth/login", json={"email": "a@b.com", "password": GOOD_PASSWORD})
    assert login.status_code == 200
    assert client.get("/api/auth/me").status_code == 200


def test_duplicate_email_is_rejected(client):
    first = client.post("/api/auth/signup", json={"email": "dup@x.com", "password": GOOD_PASSWORD})
    assert first.status_code == 200
    second = client.post("/api/auth/signup", json={"email": "dup@x.com", "password": GOOD_PASSWORD})
    assert second.status_code == 409


def test_login_wrong_password(client):
    client.post("/api/auth/signup", json={"email": "u@x.com", "password": GOOD_PASSWORD})
    bad = client.post("/api/auth/login", json={"email": "u@x.com", "password": "wrong wrong wrong"})
    assert bad.status_code == 401


def test_short_password_rejected(client):
    resp = client.post("/api/auth/signup", json={"email": "short@x.com", "password": "abc"})
    assert resp.status_code == 422  # pydantic min_length


def test_me_and_connectors_require_auth(client):
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/connectors").status_code == 401


def test_connector_credential_stored_encrypted(client, admin_conninfo, monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "unit-test-secret-key")
    client.post("/api/auth/signup", json={"email": "sec@x.com", "password": GOOD_PASSWORD})

    secret = "ghp_thisIsAReallySecretToken_0001"
    saved = client.post(
        "/api/connectors/github/credential", json={"secret": secret, "label": "main org"}
    )
    assert saved.status_code == 204

    # The wizard's Connect step now reflects GitHub as connected.
    statuses = {c["type"]: c["connected"] for c in client.get("/api/connectors").json()}
    assert statuses["github"] is True
    assert statuses["anthropic"] is False

    # On disk it is ciphertext, not the plaintext token — but it decrypts back.
    with psycopg.connect(admin_conninfo) as conn:  # admin bypasses RLS
        rows = conn.execute(
            "SELECT ciphertext FROM connector_credential WHERE connector_type = 'github'"
        ).fetchall()
    assert len(rows) == 1
    stored = bytes(rows[0][0])
    assert secret.encode() not in stored  # plaintext is not present
    assert crypto.decrypt(stored) == secret  # round-trips with the key


def test_unknown_connector_type_rejected(client):
    client.post("/api/auth/signup", json={"email": "uc@x.com", "password": GOOD_PASSWORD})
    resp = client.post("/api/connectors/myspace/credential", json={"secret": "x"})
    assert resp.status_code == 400
