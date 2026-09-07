"""End-to-end API: discover features -> import a coding-tool CSV -> build summary."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from meter import discovery
from meter.api import create_app
from meter.github import PullRequest

PASSWORD = "correct horse battery"


def _pr(number, title, branch, author):
    return PullRequest(number, "acme/core", title, "", branch, author, "2026-05-01T00:00:00Z", "")


FIXTURE_PRS = [
    _pr(1, "Threat triage automation", "feature/threat-triage", "alice"),
    _pr(2, "Threat scoring model", "feature/threat-scoring", "alice"),
    _pr(3, "Report generator", "feature/report-gen", "bob"),
]


class _FakeGitHub:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def list_repos(self, owner):
        return sorted({p.repo for p in FIXTURE_PRS})

    def fetch_merged_prs(self, owner, since):
        return FIXTURE_PRS


@pytest.fixture
def client(admin_conn, admin_conninfo, app_conninfo, monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "unit-test-secret-key")
    monkeypatch.setenv("DATABASE_URL", admin_conninfo)
    monkeypatch.setenv("DATABASE_APP_URL", app_conninfo)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(discovery, "_make_github_client", lambda token: _FakeGitHub())
    c = TestClient(create_app())
    c.post("/api/auth/signup", json={"email": "cto@acme.com", "password": PASSWORD})
    c.post("/api/connectors/github/credential", json={"secret": "ghp_token"})
    c.post("/api/discovery/run", json={"owner": "acme"})
    return c


def test_import_csv_and_summary(client):
    csv_text = "developer,tool,amount\nalice,cursor,100\nbob,cursor,60\ndave,cursor,30\n"
    summary = client.post("/api/build/import", json={"csv": csv_text, "period": "2026-05"}).json()

    features = {f["name"]: f for f in summary["features"]}
    assert features["Threat"]["amount"] == 100.0  # alice, all in Threat -> high
    assert features["Threat"]["by_tool"] == {"cursor": 100.0}
    assert features["Reports"]["amount"] == 60.0
    assert summary["unattributed"] == 30.0  # dave has no PRs
    assert summary["total"] == 190.0

    view = client.get("/api/build/summary", params={"period": "2026-05"}).json()
    assert {d["developer_id"] for d in view["developers"]} == {"alice", "bob", "dave"}


def test_bad_csv_returns_400(client):
    resp = client.post("/api/build/import", json={"csv": "not,really,a,spend,file\n"})
    assert resp.status_code == 400


def test_record_fine_tune_training_cost(client):
    feature = client.post("/api/features", json={"name": "Log triage"}).json()
    summary = client.post(
        "/api/build/training",
        json={
            "feature_id": feature["id"],
            "amount": 4200,
            "label": "Llama-3.1-70B tuning",
            "period": "2026-05",
            "run_ref": "run:ft-2026-04",
        },
    ).json()

    feat = next(f for f in summary["features"] if f["name"] == "Log triage")
    assert feat["amount"] == 4200.0
    # A fine-tuning run is BUILD cost (the tool bucket), never inference.
    assert feat["by_tool"] == {"fine_tune": 4200.0}
    assert feat["confidence"] == "high"  # directly attributed
