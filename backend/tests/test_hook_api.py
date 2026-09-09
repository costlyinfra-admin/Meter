"""End-to-end API: mint ingest token -> SDK posts events -> reconcile."""

from __future__ import annotations

import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient
from meter.api import create_app


def span_event(**fields):
    """One finished LLM call, as a lifecycle event.

    The SDK now sends spans rather than cost rows, so every one of these carries
    the trace it belongs to. A helper keeps the tests about what they are
    testing rather than about event plumbing.
    """
    event = {
        "event_type": "span.completed",
        "trace_id": fields.pop("trace_id", None) or f"t-{uuid.uuid4().hex[:8]}",
        "span_id": fields.pop("span_id", None) or uuid.uuid4().hex[:8],
        "span_kind": "llm",
        "operation_name": fields.pop("operation_name", "completion"),
        "application": fields.pop("application", "support-agent"),
    }
    event.update(fields)
    return event


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
                span_event(
                    provider="anthropic",
                    model="claude-sonnet-4-6",
                    tokens_in=100_000_000,
                    tokens_out=0,
                    feature_id=feature["id"],
                )
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
        return span_event(
            provider="anthropic",
            model="claude-sonnet-4-6",
            tokens_in=1_000_000,
            tokens_out=0,
            feature_id=feature["id"],
            latency_ms=latency,
            customer_id=customer,
            occurred_at="2026-06-15T12:00:00Z",
        )

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
    # An optimization signal is a salted fingerprint and counts — the same
    # privacy class as prompt_hash, and never any text. It stays in the event
    # contract because the duplicate/uncached-prefix detectors are built on it,
    # and dropping it would have silently removed a working feature.
    feature = client.post("/api/features", json={"name": "AI threat triage"}).json()
    token = client.post("/api/hook/token").json()["token"]

    resp = client.post(
        "/api/hook/events",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "events": [
                span_event(
                    provider="anthropic",
                    model="claude-sonnet-4-6",
                    tokens_in=1_000_000,
                    tokens_out=0,
                    feature_id=feature["id"],
                    signal={"kind": "duplicate", "fingerprint": "fp-api-1", "count": 1},
                )
            ]
        },
    )
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 1

    with psycopg.connect(admin_conninfo) as db:  # admin bypasses RLS for the assertion
        row = db.execute("SELECT signal_kind, fingerprint, call_count FROM usage_signal").fetchone()
    assert row == ("duplicate", "fp-api-1", 1)


def test_opportunities_endpoint_surfaces_measured_savings(client):
    # End-to-end: ingest a duplicate signal, then read measured opportunities.
    feature = client.post("/api/features", json={"name": "AI threat triage"}).json()
    token = client.post("/api/hook/token").json()["token"]

    def dup():
        return span_event(
            **{
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "tokens_in": 1_000_000,
                "tokens_out": 0,
                "feature_id": feature["id"],
                "occurred_at": "2026-06-15T10:00:00Z",
                "signal": {"kind": "duplicate", "fingerprint": "fp-a", "count": 1},
            }
        )

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
        ev = span_event(
            provider="anthropic",
            model="claude-sonnet-4-6",
            tokens_in=1_000_000,
            tokens_out=0,
            feature_id=fid,
            occurred_at="2026-06-15T10:00:00Z",
            signal={"kind": "duplicate", "fingerprint": "fp-a", "count": 1},
        )
        second = span_event(
            **{
                **{k: v for k, v in ev.items() if k not in ("event_type", "span_id", "trace_id")},
                "occurred_at": "2026-06-16T10:00:00Z",
            }
        )
        client.post(
            "/api/hook/events",
            headers={"Authorization": f"Bearer {token}"},
            json={"events": [ev, second]},
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


# --------------------------------------------------------------------------
# GET /api/hook/recent — the Install SDK page's "is it reporting yet" check
# --------------------------------------------------------------------------
def _post_event(client, token, **over):
    event = span_event(
        provider="anthropic", model="claude-sonnet-4-6", tokens_in=1000, tokens_out=100
    )
    event.update(over)
    return client.post(
        "/api/hook/events",
        headers={"Authorization": f"Bearer {token}"},
        json={"events": [event]},
    )


def test_recent_reports_nothing_before_the_sdk_has_sent_anything(client):
    body = client.get("/api/hook/recent").json()
    # An explicit null, not an empty object or a 404 — "no events yet" is a state
    # the page renders, not an error it has to interpret.
    assert body == {"event": None}


def test_recent_reports_the_newest_event_and_only_what_confirms_the_install(client):
    feature = client.post("/api/features", json={"name": "AI threat triage"}).json()
    token = client.post("/api/hook/token").json()["token"]
    assert _post_event(client, token, feature_id=feature["id"]).status_code == 200

    event = client.get("/api/hook/recent").json()["event"]
    assert event["provider"] == "anthropic"
    assert event["model"] == "claude-sonnet-4-6"
    assert event["feature_id"] == feature["id"]
    assert event["feature_name"] == "AI threat triage"
    assert event["received_at"]
    assert event["requests"] == 1

    # Nothing else comes back. The panel needs to identify the report, and this
    # endpoint is reachable from the browser, so it carries no cost, no tokens,
    # no ingest credential and nothing from the call itself.
    assert set(event) == {
        "feature_id",
        "feature_name",
        "provider",
        "model",
        "received_at",
        "requests",
    }
    assert "token" not in str(event).lower()


def test_recent_follows_the_latest_report_not_the_first(client):
    # Both events land on the same monthly row, because ingest aggregates. The
    # timestamp still has to move, or someone re-installing is shown the moment
    # of their first ever event and told it is their latest.
    token = client.post("/api/hook/token").json()["token"]
    _post_event(client, token)
    first = client.get("/api/hook/recent").json()["event"]["received_at"]

    _post_event(client, token, model="claude-haiku-4-5")
    latest = client.get("/api/hook/recent").json()["event"]
    assert latest["received_at"] >= first
    assert latest["model"] == "claude-haiku-4-5"


def test_recent_reports_an_unattributed_event_rather_than_hiding_it(client):
    # No feature id: the event is real and worth confirming, it simply landed in
    # the Unattributed bucket. Saying "nothing received" would be wrong.
    token = client.post("/api/hook/token").json()["token"]
    _post_event(client, token)

    event = client.get("/api/hook/recent").json()["event"]
    assert event["feature_id"] is None
    assert event["feature_name"] is None
    assert event["provider"] == "anthropic"


def test_recent_requires_a_session(client):
    client.post("/api/auth/logout")
    assert client.get("/api/hook/recent").status_code == 401
    # And an ingest token is not a substitute: this route is session-only.
    client.post("/api/auth/signup", json={"email": "other@acme.com", "password": PASSWORD})
    token = client.post("/api/hook/token").json()["token"]
    client.post("/api/auth/logout")
    assert (
        client.get("/api/hook/recent", headers={"Authorization": f"Bearer {token}"}).status_code
        == 401
    )


def test_recent_never_shows_another_tenants_events(client):
    token = client.post("/api/hook/token").json()["token"]
    _post_event(client, token)
    assert client.get("/api/hook/recent").json()["event"] is not None
    client.post("/api/auth/logout")

    # A second organization sees its own empty state, not the first one's event.
    client.post("/api/auth/signup", json={"email": "cto@other.com", "password": PASSWORD})
    assert client.get("/api/hook/recent").json() == {"event": None}
