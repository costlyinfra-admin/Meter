"""Test this, over HTTP.

The experiment logic is tested below the request model in test_experiments.py.
These cross the wire, because a guard in the request model that is tighter
than what the application accepts — the 16-character cap that rejected every
projected-budget alert — is invisible to every test that does not.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from meter import experiments, features, hook
from meter.api import ExperimentRequest, create_app

PASSWORD = "correct horse battery"
WHEN = dt.datetime.now(dt.timezone.utc).replace(day=15, hour=10).isoformat()


@pytest.fixture
def client(admin_conn, admin_conninfo, app_conninfo, monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "unit-test-secret-key")
    monkeypatch.setenv("DATABASE_URL", admin_conninfo)
    monkeypatch.setenv("DATABASE_APP_URL", app_conninfo)
    c = TestClient(create_app())
    c.post("/api/auth/signup", json={"email": "cto@acme.com", "password": PASSWORD})
    return c


@pytest.fixture
def feature(client):
    tenant = client.get("/api/auth/me").json()["tenant_id"]
    feature_id = features.add_feature(tenant, "AI threat triage")["id"]
    repeat = {
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "tokens_in": 1_000_000,
        "tokens_out": 0,
        "feature_id": feature_id,
        "occurred_at": WHEN,
        "signal": {
            "kind": "duplicate",
            "fingerprint": "fp",
            "count": 1,
            "fingerprint_version": "v2",
            "scope_kind": "explicit",
        },
    }
    hook.ingest_events(tenant, [repeat, repeat])
    return feature_id


def test_a_test_can_be_started_read_and_cancelled_over_http(client, feature):
    resp = client.post(
        f"/api/features/{feature}/experiments",
        json={"lever": "duplicate_calls", "ttl_seconds": 3600, "scoped_only": False},
    )
    assert resp.status_code == 201, resp.text
    exp = resp.json()
    # No simulation counters yet: it waits, and says how many calls it has.
    assert exp["status"] == "waiting_for_data"
    assert exp["setting_label"] == "answers reused for up to 1 hour"
    assert exp["created_by"] == "cto@acme.com"

    got = client.get(f"/api/experiments/{exp['id']}")
    assert got.status_code == 200 and got.json()["feature_name"] == "AI threat triage"

    cancelled = client.post(f"/api/experiments/{exp['id']}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"


def test_the_recommendation_carries_its_test_over_http(client, feature):
    client.post(
        f"/api/features/{feature}/experiments",
        json={"lever": "duplicate_calls", "ttl_seconds": 600, "scoped_only": True},
    )
    opps = client.get(f"/api/features/{feature}/opportunities").json()["opportunities"]
    dup = next(o for o in opps if o["lever"] == "duplicate_calls")
    assert dup["testable"] is True
    assert dup["validation"] == "untested"
    assert dup["experiment"]["status"] == "waiting_for_data"
    assert dup["experiment"]["setting_label"] == (
        "answers reused for up to 10 minutes, within a customer or cache scope only"
    )


def test_a_setting_the_application_refuses_says_why(client, feature):
    resp = client.post(
        f"/api/features/{feature}/experiments",
        json={"lever": "duplicate_calls", "ttl_seconds": 120, "scoped_only": False},
    )
    assert resp.status_code == 400
    assert "how old a cached answer may be" in resp.json()["detail"]


def test_unknown_things_are_not_found(client, feature):
    missing = "00000000-0000-0000-0000-000000000000"
    assert (
        client.post(
            f"/api/features/{missing}/experiments",
            json={"lever": "duplicate_calls", "ttl_seconds": 600, "scoped_only": False},
        ).status_code
        == 404
    )
    assert client.get(f"/api/experiments/{missing}").status_code == 404
    assert client.get("/api/experiments/not-a-uuid").status_code == 404
    assert client.post(f"/api/experiments/{missing}/cancel").status_code == 404


def test_every_choice_the_application_offers_fits_the_request_model():
    """The tripwire: each lever and setting experiments.py accepts must get past
    the request model's guards, or it would be refused before it is validated."""
    for seconds in experiments.TTL_CHOICES:
        for scoped in (True, False):
            ExperimentRequest(lever="duplicate_calls", ttl_seconds=seconds, scoped_only=scoped)
    for ttl in experiments.CACHE_TTLS:
        ExperimentRequest(lever="prompt_caching", cache_ttl=ttl)
    for lever in experiments.SIMULATABLE:
        ExperimentRequest(lever=lever)
