"""End-to-end: connect AWS -> sync -> read the tab's data, all tenant-scoped."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from meter import infrastructure
from meter.api import create_app
from meter.providers import AwsCostItem, ProviderError

PASSWORD = "correct horse battery"
SECRET = "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY"
CREDENTIAL = json.dumps(
    {"access_key_id": "AKIAIOSFODNN7EXAMPLE", "secret_access_key": SECRET, "tag": "feature"}
)


def _item(service, amount, *, tag=None):
    return AwsCostItem(
        period=dt.date.today(),
        amount=Decimal(str(amount)),
        service=service,
        tag_key="feature",
        tag_value=tag,
        dimensions={"SERVICE": service, "TAG:feature": tag},
    )


class _FakeAws:
    metric = "UnblendedCost"
    granularity = "DAILY"

    def __init__(self, items=None, error=None):
        self._items, self._error = items or [], error

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def fetch_items(self, start, end):
        if self._error:
            raise self._error
        return self._items


@pytest.fixture
def client(admin_conn, admin_conninfo, app_conninfo, monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "unit-test-secret-key")
    monkeypatch.setenv("DATABASE_URL", admin_conninfo)
    monkeypatch.setenv("DATABASE_APP_URL", app_conninfo)
    c = TestClient(create_app())
    c.post("/api/auth/signup", json={"email": "cto@acme.com", "password": PASSWORD})
    return c


def _connect(client):
    r = client.post("/api/connectors/aws/credential", json={"secret": CREDENTIAL})
    assert r.status_code == 204


def _fake(monkeypatch, items=None, error=None):
    monkeypatch.setattr(infrastructure, "_make_client", lambda *_: _FakeAws(items, error))


# --- the registry ----------------------------------------------------------
def test_providers_lists_every_cloud_as_connectable(client):
    providers = client.get("/api/infrastructure/providers").json()

    # "azure_cloud", not "azure": that id belongs to the Azure OpenAI inference
    # connector, and the two must not be confused for one another.
    assert [p["type"] for p in providers] == ["aws", "azure_cloud", "gcp"]
    assert all(p["status"] == "available" for p in providers)
    assert all(p["connected"] is False for p in providers)
    assert all(p["last_sync"] is None for p in providers)


def test_the_aws_connector_appears_under_the_infrastructure_category(client):
    connectors = client.get("/api/connectors").json()
    aws = next(c for c in connectors if c["type"] == "aws")
    assert aws["category"] == "infrastructure"
    assert aws["name"] == "Amazon Web Services"


# --- sync ------------------------------------------------------------------
def test_sync_requires_a_connected_credential(client):
    r = client.post("/api/infrastructure/ingest", json={"provider": "aws"})
    assert r.status_code == 400
    assert "Connect aws" in r.json()["detail"]


def test_sync_imports_the_bill_and_reports_what_it_did(client, monkeypatch):
    _connect(client)
    _fake(
        monkeypatch,
        [_item("Amazon Simple Storage Service", "12.50", tag="triage"), _item("Amazon EC2", "40")],
    )

    body = client.post("/api/infrastructure/ingest", json={"provider": "aws"}).json()

    assert body["items"] == 2
    assert body["infrastructure"] == 52.5
    assert body["excluded"] == 0.0


def test_bedrock_spend_is_never_part_of_the_infrastructure_total(client, monkeypatch):
    _connect(client)
    _fake(monkeypatch, [_item("Amazon Bedrock", "500"), _item("Amazon S3", "10")])

    body = client.post("/api/infrastructure/ingest", json={"provider": "aws"}).json()
    assert body["infrastructure"] == 10.0
    assert body["excluded"] == 500.0

    summary = client.get("/api/infrastructure/summary").json()
    assert summary["total"] == 10.0
    assert summary["excluded"] == 500.0
    assert [s["service"] for s in summary["services"]] == ["Amazon S3"]


def test_a_repeated_sync_does_not_double_the_bill(client, monkeypatch):
    _connect(client)
    _fake(monkeypatch, [_item("Amazon S3", "10")])

    client.post("/api/infrastructure/ingest", json={"provider": "aws"})
    client.post("/api/infrastructure/ingest", json={"provider": "aws"})

    summary = client.get("/api/infrastructure/summary").json()
    assert summary["total"] == 10.0
    assert summary["rows"] == 1


@pytest.mark.parametrize(
    "provider, message",
    [
        ("aws", "AWS rejected the credentials. Needs ce:GetCostAndUsage."),
        ("azure_cloud", "Azure rejected the request. Needs Cost Management Reader."),
        ("gcp", "Google rejected the request. Needs BigQuery Data Viewer."),
    ],
)
def test_a_rejected_credential_is_a_400_in_that_cloud_s_own_words(
    client, monkeypatch, provider, message
):
    # Each cloud names its own roles and consoles. A generic sentence — or worse,
    # another provider's — sends someone to the wrong place to fix it.
    r = client.post(f"/api/connectors/{provider}/credential", json={"secret": CREDENTIAL})
    assert r.status_code == 204
    _fake(monkeypatch, error=ProviderError(message, 401))

    r = client.post("/api/infrastructure/ingest", json={"provider": provider})
    assert r.status_code == 400
    assert r.json()["detail"] == message


def test_an_unusable_credential_is_a_400_not_a_bad_gateway(client, monkeypatch):
    # A key we cannot even parse is the customer's to fix. Reporting it as 502
    # says "the provider is broken", which sends them to the wrong place.
    _connect(client)
    _fake(monkeypatch, error=ProviderError("The private key could not be read.", 401))

    r = client.post("/api/infrastructure/ingest", json={"provider": "aws"})
    assert r.status_code == 400


def test_an_unreachable_provider_is_named_correctly(client, monkeypatch):
    import httpx as _httpx

    r = client.post("/api/connectors/gcp/credential", json={"secret": CREDENTIAL})
    assert r.status_code == 204
    _fake(monkeypatch, error=_httpx.ConnectError("no route"))

    r = client.post("/api/infrastructure/ingest", json={"provider": "gcp"})
    assert r.status_code == 502
    assert "Google Cloud Platform" in r.json()["detail"]
    assert "AWS" not in r.json()["detail"]


def test_an_unknown_cloud_is_refused(client):
    r = client.post("/api/infrastructure/ingest", json={"provider": "digitalocean"})
    assert r.status_code == 400
    assert "Unknown infrastructure provider" in r.json()["detail"]


@pytest.mark.parametrize("provider", ["azure_cloud", "gcp"])
def test_every_cloud_syncs_through_the_same_route(client, monkeypatch, provider):
    r = client.post(f"/api/connectors/{provider}/credential", json={"secret": CREDENTIAL})
    assert r.status_code == 204
    _fake(monkeypatch, [_item("Some Service", "12.00"), _item("Vertex AI", "900.00")])

    body = client.post("/api/infrastructure/ingest", json={"provider": provider}).json()
    assert body["provider"] == provider
    assert body["items"] == 2

    summary = client.get(f"/api/infrastructure/summary?provider={provider}").json()
    assert summary["provider"] == provider


def test_every_cloud_appears_under_the_infrastructure_category(client):
    connectors = client.get("/api/connectors").json()
    infra = {c["type"] for c in connectors if c["category"] == "infrastructure"}
    assert infra == {"aws", "azure_cloud", "gcp"}
    # The Azure OpenAI connector stays where it was, on the inference side.
    azure_openai = next(c for c in connectors if c["type"] == "azure")
    assert azure_openai["category"] == "inference"


# --- state shown on the card ----------------------------------------------
def test_the_card_shows_connection_state_last_sync_and_config(client, monkeypatch):
    _connect(client)
    _fake(monkeypatch, [_item("Amazon S3", "10")])
    client.post("/api/infrastructure/ingest", json={"provider": "aws"})

    aws = next(p for p in client.get("/api/infrastructure/providers").json() if p["type"] == "aws")
    assert aws["connected"] is True
    assert aws["last_sync"]["status"] == "success"
    assert aws["last_sync"]["items"] == 1
    assert aws["config"]["tag"] == "feature"
    assert aws["config"]["metric"] == "UnblendedCost"


def test_a_failed_sync_is_visible_on_the_card(client, monkeypatch):
    _connect(client)
    _fake(monkeypatch, error=ProviderError("AWS rejected the credentials.", 403))
    client.post("/api/infrastructure/ingest", json={"provider": "aws"})

    aws = next(p for p in client.get("/api/infrastructure/providers").json() if p["type"] == "aws")
    assert aws["last_sync"]["status"] == "error"
    assert aws["last_sync"]["error_message"] == "AWS rejected the credentials."


def test_no_endpoint_ever_returns_the_stored_keys(client, monkeypatch):
    _connect(client)
    _fake(monkeypatch, [_item("Amazon S3", "10")])
    client.post("/api/infrastructure/ingest", json={"provider": "aws"})

    for path in (
        "/api/infrastructure/providers",
        "/api/infrastructure/summary",
        "/api/connectors",
    ):
        body = client.get(path).text
        assert SECRET not in body
        assert "AKIAIOSFODNN7EXAMPLE" not in body


# --- authentication --------------------------------------------------------
@pytest.mark.parametrize(
    "method, path",
    [
        ("get", "/api/infrastructure/providers"),
        ("get", "/api/infrastructure/summary"),
        ("post", "/api/infrastructure/ingest"),
    ],
)
def test_every_route_requires_a_session(client, method, path):
    client.post("/api/auth/logout")
    r = getattr(client, method)(path, **({"json": {}} if method == "post" else {}))
    assert r.status_code == 401
