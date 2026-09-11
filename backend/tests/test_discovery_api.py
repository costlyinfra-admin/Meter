"""End-to-end API: connect GitHub -> discover -> edit -> confirm & go live."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from meter import discovery
from meter.api import create_app
from meter.github import PullRequest

PASSWORD = "correct horse battery"


def _pr(number, repo, title, branch):
    return PullRequest(number, repo, title, "", branch, "dev", "2026-05-01T00:00:00Z", "")


FIXTURE_PRS = [
    _pr(1, "acme/core", "Threat triage automation", "feature/threat-triage"),
    _pr(2, "acme/core", "Threat scoring model", "feature/threat-scoring"),
    _pr(3, "acme/core", "Report generator", "feature/report-gen"),
]


class _FakeGitHub:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def list_repos(self, owner):
        return sorted({p.repo for p in FIXTURE_PRS})

    def fetch_merged_prs(self, owner, since, *, repos=None, with_stats=True):
        if repos is not None:
            return [p for p in FIXTURE_PRS if p.repo in set(repos)]
        return FIXTURE_PRS


@pytest.fixture
def client(admin_conn, admin_conninfo, app_conninfo, monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "unit-test-secret-key")
    monkeypatch.setenv("DATABASE_URL", admin_conninfo)
    monkeypatch.setenv("DATABASE_APP_URL", app_conninfo)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)  # heuristic clustering
    monkeypatch.setattr(discovery, "_make_github_client", lambda token: _FakeGitHub())
    c = TestClient(create_app())
    c.post("/api/auth/signup", json={"email": "cto@acme.com", "password": PASSWORD})
    return c


def test_discovery_runs_without_github_credential(client):
    # No token connected -> discovery still runs (public, unauthenticated GitHub).
    resp = client.post("/api/discovery/run", json={"owner": "acme"})
    assert resp.status_code == 200
    assert resp.json()["prs"] == 3


def test_full_discovery_edit_confirm_flow(client):
    # Connect GitHub, then run discovery.
    client.post("/api/connectors/github/credential", json={"secret": "ghp_token"})
    summary = client.post("/api/discovery/run", json={"owner": "acme"}).json()
    assert summary["prs"] == 3
    assert summary["proposals"] >= 2

    proposed = client.get("/api/features", params={"status": "proposed"}).json()
    names = {f["name"] for f in proposed}
    assert "Threat" in names and "Reports" in names
    threat = next(f for f in proposed if f["name"] == "Threat")
    assert any(s["signal_type"] == "pr" for s in threat["signals"])
    assert threat["discovery_confidence"] == "high"

    # Rename one proposal.
    renamed = client.patch(f"/api/features/{threat['id']}", json={"name": "AI threat triage"})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "AI threat triage"

    # Add a manual feature.
    manual = client.post("/api/features", json={"name": "Manual feature"})
    assert manual.status_code == 201

    # Merge two proposals.
    report = next(f for f in proposed if f["name"] == "Reports")
    merged = client.post(
        "/api/features/merge",
        json={"feature_ids": [renamed.json()["id"], report["id"]], "name": "Merged feature"},
    )
    assert merged.status_code == 200

    # Confirm & go live.
    confirmed = client.post("/api/onboarding/confirm", json={}).json()
    assert confirmed and all(f["status"] == "confirmed" for f in confirmed)
    assert client.get("/api/features", params={"status": "proposed"}).json() == []


def test_split_via_api(client):
    client.post("/api/connectors/github/credential", json={"secret": "ghp_token"})
    client.post("/api/discovery/run", json={"owner": "acme"})
    proposed = client.get("/api/features", params={"status": "proposed"}).json()
    threat = next(f for f in proposed if f["name"] == "Threat")
    pr_sigs = [s for s in threat["signals"] if s["signal_type"] == "pr"]

    resp = client.post(
        f"/api/features/{threat['id']}/split",
        json={
            "groups": [
                {"name": "Triage", "signal_ids": [pr_sigs[0]["id"]]},
                {"name": "Scoring", "signal_ids": [pr_sigs[1]["id"]]},
            ]
        },
    )
    assert resp.status_code == 200
    assert {f["name"] for f in resp.json()} == {"Triage", "Scoring"}


def test_repos_endpoint_lists_org_repositories(client):
    # The selector fetches the org's repos before running discovery.
    resp = client.get("/api/discovery/repos", params={"owner": "acme"})
    assert resp.status_code == 200
    assert resp.json() == {"owner": "acme", "repos": ["acme/core"]}


def test_scope_is_empty_then_remembers_the_owner(client):
    # Nothing chosen yet -> empty scope prefill.
    assert client.get("/api/discovery/scope").json() == {"owner": None, "repos": []}

    # The fixture org has exactly one repository, so selecting it is selecting
    # everything. That is stored as "the whole owner" rather than as a frozen
    # list of one name — otherwise the next repository the org creates would be
    # excluded from every run from then on.
    client.post("/api/discovery/run", json={"owner": "acme", "repos": ["acme/core"]})
    assert client.get("/api/discovery/scope").json() == {"owner": "acme", "repos": []}


def test_feature_category_endpoint_records_a_user_tag(client):
    feature = client.post("/api/features", json={"name": "Alert digest"}).json()
    assert feature["category"] is None  # nobody has tagged it yet

    tagged = client.put(f"/api/features/{feature['id']}/category", json={"category": "reporting"})
    assert tagged.status_code == 200
    assert tagged.json()["category"] == "reporting"
    assert tagged.json()["category_source"] == "user"

    # Clearing hands it back to whatever discovery guesses next.
    cleared = client.put(f"/api/features/{feature['id']}/category", json={"category": None})
    assert cleared.json()["category"] is None

    # Only the shipped vocabulary is accepted.
    bad = client.put(f"/api/features/{feature['id']}/category", json={"category": "misc"})
    assert bad.status_code == 422
    missing = client.put(
        "/api/features/00000000-0000-0000-0000-000000000000/category",
        json={"category": "ui"},
    )
    assert missing.status_code == 404


def test_feature_categories_endpoint_publishes_the_vocabulary(client):
    # The UI renders the picker from this, so the list is never hardcoded twice.
    body = client.get("/api/features/categories").json()
    values = [c["value"] for c in body["categories"]]
    assert {"chat", "api", "ui", "docs", "auth"} <= set(values)
    assert {"value": "data", "label": "Data/ETL"} in body["categories"]


# --- BYOK over the API: the key must not come back out ---------------------


def test_discovery_llm_roundtrip_never_exposes_the_key(client):
    assert client.get("/api/settings/discovery-llm").json() == {
        "configured": False,
        "enabled": False,
        "has_key": False,
    }

    saved = client.put(
        "/api/settings/discovery-llm",
        json={
            "provider": "groq",
            "base_url": "https://api.groq.com/openai/v1",
            "model": "llama-3.3-70b-versatile",
            "api_key": "gsk_super_secret_key_value",
        },
    )
    assert saved.status_code == 200
    body = saved.json()
    assert body["configured"] is True and body["has_key"] is True
    assert body["provider"] == "groq" and body["model"] == "llama-3.3-70b-versatile"

    # The secret appears in no response, on save or on read.
    assert "gsk_super_secret_key_value" not in saved.text
    assert "gsk_super_secret_key_value" not in client.get("/api/settings/discovery-llm").text
    assert "api_key" not in client.get("/api/settings/discovery-llm").json()


def test_discovery_llm_can_be_disabled_and_removed(client):
    client.put(
        "/api/settings/discovery-llm",
        json={"provider": "groq", "model": "llama-3.3-70b-versatile", "api_key": "gsk_x_secret"},
    )
    off = client.patch("/api/settings/discovery-llm", json={"enabled": False}).json()
    assert off["configured"] is True and off["enabled"] is False

    gone = client.delete("/api/settings/discovery-llm").json()
    assert gone["configured"] is False and gone["has_key"] is False


def test_discovery_llm_rejects_a_bad_configuration(client):
    bad = client.put(
        "/api/settings/discovery-llm",
        json={"provider": "nope", "model": "m", "api_key": "k"},
    )
    assert bad.status_code == 400
    assert "provider" in bad.json()["detail"].lower()


def test_discovery_llm_providers_are_published_for_the_picker(client):
    body = client.get("/api/settings/discovery-llm/providers").json()
    values = [p["value"] for p in body["providers"]]
    assert "groq" in values and "custom" in values
    assert body["default_model"]  # the UI prefills this
    groq = next(p for p in body["providers"] if p["value"] == "groq")
    assert groq["base_url"].startswith("https://")


def test_discovery_llm_requires_a_session(client):
    client.post("/api/auth/logout")
    for call in (
        lambda: client.get("/api/settings/discovery-llm"),
        lambda: client.put("/api/settings/discovery-llm", json={"provider": "groq", "model": "m"}),
        lambda: client.delete("/api/settings/discovery-llm"),
    ):
        assert call().status_code == 401


def test_the_run_endpoint_accepts_a_window_and_records_it(client):
    resp = client.post(
        "/api/discovery/run",
        json={"owner": "acme", "since": "2026-03-01", "repos": ["acme/core"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["covered_from"] == "2026-03-01"

    runs = client.get("/api/discovery/runs").json()
    assert runs["coverage"]["covered_from"] == "2026-03-01"
    assert runs["coverage"]["runs"] == 1
    assert runs["runs"][0]["trigger"] == "manual"
    # Recorded against whoever asked for it.
    assert runs["runs"][0]["started_by"] == "cto@acme.com"


def test_the_schedule_endpoints_refuse_before_discovery_has_a_scope(client):
    assert client.get("/api/discovery/schedule").json() == {
        "configurable": False,
        "enabled": False,
        "lookback_days": 14,
        "next_run_at": None,
        "owner": None,
        "repos": [],
    }
    resp = client.put("/api/discovery/schedule", json={"enabled": True})
    assert resp.status_code == 400
    assert "Run discovery once" in resp.json()["detail"]


def test_the_schedule_can_be_turned_on_and_off(client):
    client.post("/api/discovery/run", json={"owner": "acme"})

    # Off until asked for, even after a run.
    assert client.get("/api/discovery/schedule").json()["enabled"] is False

    on = client.put("/api/discovery/schedule", json={"enabled": True, "lookback_days": 21}).json()
    assert on["enabled"] is True and on["lookback_days"] == 21
    assert on["next_run_at"] is not None

    off = client.put("/api/discovery/schedule", json={"enabled": False}).json()
    assert off["enabled"] is False and off["next_run_at"] is None
    # The lookback is remembered, so turning it back on does not reset the choice.
    assert off["lookback_days"] == 21


def test_discovery_run_and_schedule_endpoints_require_a_session(client):
    client.post("/api/auth/logout")
    for call in (
        lambda: client.get("/api/discovery/runs"),
        lambda: client.get("/api/discovery/schedule"),
        lambda: client.put("/api/discovery/schedule", json={"enabled": True}),
    ):
        assert call().status_code == 401
