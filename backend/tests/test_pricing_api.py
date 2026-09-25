"""The published price book, served to the pricing screen.

The point of this endpoint is that a customer can check Meter's arithmetic:
every measured saving in the product is one of these rates times a counted
number of tokens. So the tests care about two things — that the screen cannot
show a rate the costing does not use, and that a published-rates endpoint
carries nothing belonging to a tenant.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from meter import pricing
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


def _models(body) -> dict:
    return {m["model"]: m for p in body["providers"] for m in p["models"]}


def test_the_price_book_needs_a_session(admin_conn, admin_conninfo, app_conninfo, monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "unit-test-secret-key")
    monkeypatch.setenv("DATABASE_URL", admin_conninfo)
    monkeypatch.setenv("DATABASE_APP_URL", app_conninfo)
    assert TestClient(create_app()).get("/api/pricing").status_code == 401


def test_every_rate_shown_is_the_rate_used_to_cost_tokens(client):
    # The screen exists so a customer can check the arithmetic. A table that
    # could disagree with the costing would be worse than no table.
    body = client.get("/api/pricing").json()
    assert body["version"] == pricing.PRICING_VERSION
    for p in body["providers"]:
        for row in p["models"]:
            assert pricing.rate_in(row["model"], p["provider"]) == Decimal(
                row["input_per_million"]
            ) / Decimal(1_000_000)
            assert pricing.rate_out(row["model"], p["provider"]) == Decimal(
                row["output_per_million"]
            ) / Decimal(1_000_000)


def test_a_provider_with_no_priced_cache_reports_nothing_rather_than_zero(client):
    body = client.get("/api/pricing").json()
    hosted = next(p for p in body["providers"] if p["provider"] == "together")
    for row in hosted["models"]:
        # Absent, not 0. A zero would read as "caching is free here", which is
        # a claim; the truth is that Meter does not price a cache for this host.
        assert row["cache_read_per_million"] is None
        assert row["cache_write_per_million"] is None
        assert row["cache_is_automatic"] is None


def test_the_cache_columns_follow_the_model_not_only_the_provider(client):
    rows = _models(client.get("/api/pricing").json())
    # gpt-4o reads cache at half its input rate where the current families read
    # at a tenth: $2.50 -> $1.25, not $0.25.
    assert Decimal(rows["gpt-4o"]["cache_read_per_million"]) == Decimal("1.25")
    assert Decimal(rows["gpt-5.6-sol"]["cache_read_per_million"]) == Decimal("0.40")
    # Opus 5.5 goes the other way, at a twentieth of $4.
    assert Decimal(rows["claude-opus-5-5"]["cache_read_per_million"]) == Decimal("0.20")


def test_a_cache_write_is_shown_as_dearer_than_sending_it_uncached(client):
    rows = _models(client.get("/api/pricing").json())
    sonnet = rows["claude-sonnet-4-6"]
    # $3 in, $3.75 to write. The whole reason a caching recommendation can come
    # out negative, so the screen has to show it rather than only the discount.
    assert Decimal(sonnet["cache_write_per_million"]) > Decimal(sonnet["input_per_million"])


def test_a_minimum_nobody_has_checked_is_reported_as_unknown(client):
    rows = _models(client.get("/api/pricing").json())
    assert rows["claude-haiku-4-5"]["min_cacheable_tokens"] == 4096
    # Not 0, and not Meter's own floor dressed up as a provider fact.
    assert rows["mistral-large-latest"]["min_cacheable_tokens"] is None


def test_a_provider_says_whether_its_table_has_been_checked(client):
    body = client.get("/api/pricing").json()
    by_provider = {p["provider"]: p for p in body["providers"]}
    assert by_provider["anthropic"]["checked"] == "2026-09-25"
    assert by_provider["anthropic"]["source_url"]
    # An unchecked table says so rather than borrowing the others' credibility.
    assert by_provider["together"]["checked"] is None


def test_published_rates_carry_nothing_belonging_to_a_tenant(client):
    raw = client.get("/api/pricing").text
    tenant = client.get("/api/auth/me").json()["tenant_id"]
    assert tenant not in raw
    for word in ("tenant", "amount", "spend", "feature", "email"):
        assert word not in raw.lower()
