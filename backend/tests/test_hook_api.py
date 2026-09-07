"""End-to-end API: mint ingest token -> SDK posts events -> reconcile."""

from __future__ import annotations

import psycopg
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


def test_hook_token_and_event_ingest(client):
    feature = client.post("/api/features", json={"name": "AI threat triage"}).json()
    token = client.post("/api/hook/token").json()["token"]

    # The SDK posts events with the ingest token (no session cookie).
    bad = client.post("/api/hook/events", json={"events": [{"provider": "anthropic"}]})
    assert bad.status_code == 401  # no/!bad token

    resp = client.post(
        "/api/hook/events",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "events": [
                {
                    "provider": "anthropic",
                    "model": "claude-sonnet-4-6",
                    "tokens_in": 100_000_000,
                    "tokens_out": 0,
                    "feature_id": feature["id"],
                }
            ]
        },
    )
    assert resp.status_code == 200
    assert resp.json()["cost"] == 300.0

    detail = client.get(f"/api/features/{feature['id']}/detail").json()
    assert detail["inference_sources"] == ["hook"]

    recon = client.post("/api/inference/reconcile", json={}).json()
    assert any(r["provider"] == "anthropic" and r["attributed"] == 300.0 for r in recon)


def test_hook_ingest_captures_latency_and_customer(client):
    # Regression: the API event model must NOT strip SDK v0.2 fields
    # (latency_ms, metadata.customer_id) before they reach the hook.
    feature = client.post("/api/features", json={"name": "AI threat triage"}).json()
    token = client.post("/api/hook/token").json()["token"]

    def ev(latency, customer):
        return {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "tokens_in": 1_000_000,
            "tokens_out": 0,
            "feature_id": feature["id"],
            "occurred_at": "2026-06-15T10:00:00Z",
            "latency_ms": latency,
            "metadata": {"customer_id": customer},
        }

    resp = client.post(
        "/api/hook/events",
        headers={"Authorization": f"Bearer {token}"},
        json={"events": [ev(800, "Acme"), ev(1200, "Acme"), ev(400, "Globex")]},
    )
    assert resp.status_code == 200

    # Avg latency across the 3 calls = (800 + 1200 + 400) / 3 = 800 ms.
    detail = client.get(f"/api/features/{feature['id']}/detail?period=2026-06").json()
    assert detail["headline"]["avg_latency_ms"] == 800

    # Per-customer metered spend (anthropic $3/M input): Acme 2M -> $6, Globex 1M -> $3.
    prov = client.get("/api/dashboard/providers?start=2026-06&end=2026-06").json()
    by_customer = {c["customer_id"]: c["amount"] for c in prov["by_customer"]}
    assert by_customer == {"Acme": 6.0, "Globex": 3.0}

    # ...and the Overview's By Customer tab reads the same metered rows, with the
    # per-call unit cost the SDK makes possible (Acme: $6 over 2 calls).
    cust = client.get("/api/dashboard/customers?start=2026-06&end=2026-06").json()
    acme = next(c for c in cust["customers"] if c["customer_id"] == "Acme")
    assert acme["requests"] == 2
    assert acme["cost_per_request"] == 3.0
    assert cust["coverage_pct"] == 100.0  # every metered call here is tagged


def test_hook_ingest_persists_optimization_signal(client, admin_conninfo):
    # Regression: the API event model must NOT strip the optional `signal` block
    # before it reaches the hook (same failure mode as latency_ms/metadata once had).
    feature = client.post("/api/features", json={"name": "AI threat triage"}).json()
    token = client.post("/api/hook/token").json()["token"]

    resp = client.post(
        "/api/hook/events",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "events": [
                {
                    "provider": "anthropic",
                    "model": "claude-sonnet-4-6",
                    "tokens_in": 1_000_000,
                    "tokens_out": 0,
                    "feature_id": feature["id"],
                    "signal": {"kind": "duplicate", "fingerprint": "fp-api-1", "count": 1},
                }
            ]
        },
    )
    assert resp.status_code == 200

    with psycopg.connect(admin_conninfo) as db:  # admin bypasses RLS for the assertion
        row = db.execute("SELECT signal_kind, fingerprint, call_count FROM usage_signal").fetchone()
    assert row == ("duplicate", "fp-api-1", 1)


def test_opportunities_endpoint_surfaces_measured_savings(client):
    # End-to-end: ingest a duplicate signal, then read measured opportunities.
    feature = client.post("/api/features", json={"name": "AI threat triage"}).json()
    token = client.post("/api/hook/token").json()["token"]

    def dup():
        return {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "tokens_in": 1_000_000,
            "tokens_out": 0,
            "feature_id": feature["id"],
            "occurred_at": "2026-06-15T10:00:00Z",
            "signal": {"kind": "duplicate", "fingerprint": "fp-a", "count": 1},
        }

    client.post(
        "/api/hook/events",
        headers={"Authorization": f"Bearer {token}"},
        json={"events": [dup(), dup()]},
    )

    resp = client.get(f"/api/features/{feature['id']}/opportunities?period=2026-06")
    assert resp.status_code == 200
    body = resp.json()
    dup_opp = next(o for o in body["opportunities"] if o["lever"] == "duplicate_calls")
    assert dup_opp["projected_monthly_savings"] == 6.0  # 2M input @ $3/M
    assert dup_opp["savings_type"] == "measured"
    assert body["totals"]["measured"] == 6.0

    missing = client.get("/api/features/00000000-0000-0000-0000-000000000000/opportunities")
    assert missing.status_code == 404


def test_copilot_overview_aggregates_across_features(client):
    # Two features, each with a duplicate signal -> tenant-wide rollup.
    token = client.post("/api/hook/token").json()["token"]
    ids = []
    for name in ("AI threat triage", "Report generator"):
        fid = client.post("/api/features", json={"name": name}).json()["id"]
        ids.append(fid)
        ev = {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "tokens_in": 1_000_000,
            "tokens_out": 0,
            "feature_id": fid,
            "occurred_at": "2026-06-15T10:00:00Z",
            "signal": {"kind": "duplicate", "fingerprint": "fp-a", "count": 1},
        }
        client.post(
            "/api/hook/events",
            headers={"Authorization": f"Bearer {token}"},
            json={"events": [ev, {**ev, "occurred_at": "2026-06-16T10:00:00Z"}]},
        )

    body = client.get("/api/copilot/overview?period=2026-06").json()
    # Three savings figures kept separate. Each feature's duplicates = $6, so $12.
    assert body["totals"]["measured"] == 12.0
    assert "modeled_ceiling" in body["totals"] and "directional" in body["totals"]
    # Top recommendations are ranked and tagged with their feature.
    assert body["top_recommendations"][0]["feature_name"] in {
        "AI threat triage",
        "Report generator",
    }  # noqa: E501
    # by-lever rollup: duplicate_calls across the two features.
    dup = next(x for x in body["by_lever"] if x["lever"] == "duplicate_calls")
    assert dup["count"] == 2 and dup["monthly"] == 12.0
    assert len(body["by_feature"]) >= 2


def test_apply_and_unapply_opportunity(client):
    feature = client.post("/api/features", json={"name": "AI threat triage"}).json()

    # Mark the duplicate-calls opportunity applied for this period.
    applied = client.post(
        f"/api/features/{feature['id']}/opportunities/apply",
        json={"lever": "duplicate_calls", "projected_monthly": 369.0},
    )
    assert applied.status_code == 200
    assert applied.json()["lever"] == "duplicate_calls"

    body = client.get(f"/api/features/{feature['id']}/opportunities").json()
    action = next(a for a in body["actions"] if a["lever"] == "duplicate_calls")
    assert action["projected_monthly"] == 369.0
    assert action["status"] == "pending"  # applied this period

    # Undo it.
    undo = client.delete(f"/api/features/{feature['id']}/opportunities/apply?lever=duplicate_calls")
    assert undo.status_code == 204
    body = client.get(f"/api/features/{feature['id']}/opportunities").json()
    assert body["actions"] == []


def test_hook_salt_endpoint_is_stable_and_token_gated(client):
    # The SDK's optimize mode fetches a per-tenant fingerprint salt with its token.
    token = client.post("/api/hook/token").json()["token"]

    unauth = client.get("/api/hook/salt")
    assert unauth.status_code == 401

    first = client.get("/api/hook/salt", headers={"Authorization": f"Bearer {token}"})
    assert first.status_code == 200
    salt = first.json()["salt"]
    assert salt  # a non-empty secret

    # Stable across calls (generated once, then reused).
    again = client.get("/api/hook/salt", headers={"Authorization": f"Bearer {token}"})
    assert again.json()["salt"] == salt
