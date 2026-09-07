"""End-to-end API: connect provider -> map a key -> ingest -> summary."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from meter import inference
from meter.api import create_app
from meter.providers import CostRecord, ProviderError

PASSWORD = "correct horse battery"


class _FakeAnthropic:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def fetch_costs(self, period):
        today = dt.date.today()
        return [
            CostRecord("anthropic", today, Decimal("4200"), api_key_ref="key:triage"),
            CostRecord("anthropic", today, Decimal("760"), api_key_ref="key:shared"),
        ]


@pytest.fixture
def client(admin_conn, admin_conninfo, app_conninfo, monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "unit-test-secret-key")
    monkeypatch.setenv("DATABASE_URL", admin_conninfo)
    monkeypatch.setenv("DATABASE_APP_URL", app_conninfo)
    monkeypatch.setattr(inference, "_make_cost_client", lambda provider, key: _FakeAnthropic())
    c = TestClient(create_app())
    c.post("/api/auth/signup", json={"email": "cto@acme.com", "password": PASSWORD})
    return c


class _FakeAnthropicDetailed:
    """A full Anthropic admin client: cost + usage + workspace/key metadata."""

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def fetch_costs(self, period):
        from meter.providers import month_start

        start = month_start(period)
        return [CostRecord("anthropic", start, Decimal("1000"), project="ws_mcs")]

    def fetch_usage(self, period):
        from meter.providers import UsageRecord

        return [
            UsageRecord("ws_mcs", "k_a", "claude-sonnet-4-6", tokens_in=1_000_000, tokens_out=0),
            UsageRecord("ws_mcs", "k_b", "claude-sonnet-4-6", tokens_in=1_000_000, tokens_out=0),
        ]

    def fetch_workspaces(self):
        return {"ws_mcs": "mcs-dev"}

    def fetch_api_keys(self):
        return {
            "k_a": {"name": "service-a-prod", "workspace_id": "ws_mcs"},
            "k_b": {"name": "experimental", "workspace_id": "ws_mcs"},
        }


def test_cost_source_detail_and_classify_endpoints(client, monkeypatch):
    monkeypatch.setattr(
        inference, "_make_cost_client", lambda provider, key: _FakeAnthropicDetailed()
    )
    client.post("/api/connectors/anthropic/credential", json={"secret": "sk-ant-admin"})
    client.post("/api/inference/ingest", json={"provider": "anthropic"})

    # Detail: one flat classifiable table; nothing auto-classified.
    detail = client.get("/api/cost-sources/anthropic/detail").json()
    assert detail["classifiable"] is True
    assert detail["columns"] == {"group": "Workspace", "name": "API key"}
    by_name = {r["name"]: r for r in detail["rows"]}
    assert by_name["service-a-prod"]["classification"] == "unclassified"  # NOT production
    assert sum(r["cost"] for r in detail["rows"]) == pytest.approx(1000.0, abs=0.01)

    # Classify a key -> persists and shows immediately.
    resp = client.post(
        "/api/cost-sources/anthropic/classify",
        json={"resource_type": "api_key", "resource_id": "k_a", "classification": "production"},
    )
    assert resp.status_code == 200
    again = client.get("/api/cost-sources/anthropic/detail").json()
    assert {r["name"]: r["classification"] for r in again["rows"]}["service-a-prod"] == "production"

    # Invalid classification -> 400.
    bad = client.post(
        "/api/cost-sources/anthropic/classify",
        json={"resource_type": "api_key", "resource_id": "k_a", "classification": "prod"},
    )
    assert bad.status_code == 400


def test_cost_source_detail_for_provider_without_adapter(client):
    detail = client.get("/api/cost-sources/openai/detail").json()
    assert detail["classifiable"] is False
    assert detail["rows"] == []
    assert "message" in detail


class _FakeTogether:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def fetch_costs(self, period):
        today = dt.date.today()
        return [
            CostRecord("together", today, Decimal("900"), api_key_ref="key:phishing"),
            CostRecord("together", today, Decimal("120"), api_key_ref="key:misc"),
        ]


def test_ingest_requires_provider_connected(client):
    assert client.post("/api/inference/ingest", json={"provider": "anthropic"}).status_code == 400


def test_hosted_open_source_connector_ingests(client, monkeypatch):
    # A hosted open-source aggregator is a first-class inference connector.
    monkeypatch.setattr(inference, "_make_cost_client", lambda provider, key: _FakeTogether())
    client.post("/api/connectors/together/credential", json={"secret": "tg-admin"})
    feature = client.post("/api/features", json={"name": "Phishing detection"}).json()
    client.post(
        f"/api/features/{feature['id']}/signals",
        json={"signal_type": "api_key", "external_ref": "key:phishing"},
    )

    summary = client.post("/api/inference/ingest", json={"provider": "together"}).json()
    assert summary["attributed"] == 900.0
    assert summary["unattributed"] == 120.0  # unmapped key -> Unattributed

    view = client.get("/api/inference/summary").json()
    assert view["by_provider"]["together"] == 1020.0


def test_sync_backfills_multiple_months(client, monkeypatch):
    # "Sync now" pulls history: months>1 walks the window, not just this month.
    monkeypatch.setattr(inference, "_make_cost_client", lambda provider, key: _FakeTogether())
    client.post("/api/connectors/together/credential", json={"secret": "tg-admin"})

    summary = client.post(
        "/api/inference/ingest", json={"provider": "together", "months": 3}
    ).json()
    assert summary["months"] == 3
    assert len(summary["by_month"]) == 3
    assert summary["total"] == 3060.0  # 3 * (900 + 120)


class _FakeBedrock:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def fetch_costs(self, period):
        today = dt.date.today()
        return [
            CostRecord("bedrock", today, Decimal("4200"), api_key_ref="triage"),  # tag value
            CostRecord("bedrock", today, Decimal("300"), api_key_ref=None),  # untagged
        ]


def test_bedrock_cloud_cost_connector_ingests_by_tag(client, monkeypatch):
    import json

    monkeypatch.setattr(inference, "_make_cost_client", lambda provider, key: _FakeBedrock())
    # AWS creds are stored as one JSON blob.
    client.post(
        "/api/connectors/bedrock/credential",
        json={
            "secret": json.dumps(
                {"access_key_id": "AKIA", "secret_access_key": "s", "tag": "feature"}
            )
        },
    )
    feature = client.post("/api/features", json={"name": "AI threat triage"}).json()
    client.post(
        f"/api/features/{feature['id']}/signals",
        json={"signal_type": "api_key", "external_ref": "triage"},  # map the tag value
    )

    summary = client.post("/api/inference/ingest", json={"provider": "bedrock"}).json()
    assert summary["attributed"] == 4200.0
    assert summary["unattributed"] == 300.0  # untagged Bedrock spend


def test_connect_map_ingest_summary(client):
    # Connect Anthropic + create a feature mapped to one of the API keys.
    client.post("/api/connectors/anthropic/credential", json={"secret": "sk-ant-admin"})
    feature = client.post("/api/features", json={"name": "AI threat triage"}).json()
    mapped = client.post(
        f"/api/features/{feature['id']}/signals",
        json={"signal_type": "api_key", "external_ref": "key:triage"},
    )
    assert mapped.status_code == 200

    summary = client.post("/api/inference/ingest", json={"provider": "anthropic"}).json()
    assert summary["total"] == 4960.0
    assert summary["attributed"] == 4200.0
    assert summary["unattributed"] == 760.0

    view = client.get("/api/inference/summary").json()
    by_name = {f["name"]: f for f in view["features"]}
    assert by_name["AI threat triage"]["amount"] == 4200.0
    assert by_name["AI threat triage"]["confidence"] == "high"
    assert view["unattributed"] == 760.0  # shared key landed in Unattributed
    assert view["by_provider"]["anthropic"] == 4960.0  # matches the provider total


class _FlakyProvider:
    """Anthropic works; OpenAI's key is rejected — the refresh must report both."""

    def __init__(self, provider):
        self.provider = provider

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def fetch_costs(self, period):
        if self.provider == "openai":
            raise ProviderError("Provider rejected the admin key (401).", 401)
        return [CostRecord("anthropic", dt.date.today(), Decimal("125.00"), api_key_ref="k")]


def test_refresh_syncs_every_connected_inference_provider(client, monkeypatch):
    monkeypatch.setattr(
        inference, "_make_cost_client", lambda provider, key: _FlakyProvider(provider)
    )
    client.post("/api/connectors/anthropic/credential", json={"secret": "sk-ant-admin"})
    client.post("/api/connectors/openai/credential", json={"secret": "sk-openai-admin"})

    out = client.post("/api/inference/refresh").json()

    # Both connected providers were attempted; the good one landed real dollars...
    assert out["providers"] == 2
    assert {s["provider"]: s["total"] for s in out["synced"]} == {"anthropic": 125.0}
    assert out["total"] == 125.0
    # ...and the broken one is reported, not swallowed.
    assert [e["provider"] for e in out["errors"]] == ["openai"]
    assert "401" in out["errors"][0]["error"]


def test_refresh_with_nothing_connected_is_a_no_op(client):
    out = client.post("/api/inference/refresh").json()
    assert out == {"providers": 0, "synced": [], "errors": [], "total": 0.0}
