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
        assert row["cache_write_5m_per_million"] is None
        assert row["cache_write_1h_per_million"] is None
        assert row["cache_is_automatic"] is None
        # Same for a batch tier nobody has established this host offers.
        assert row["input_batch_per_million"] is None
        assert row["output_batch_per_million"] is None


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
    # $3 in, $3.75 to write for five minutes, $6 to write for an hour. The
    # whole reason a caching recommendation can come out negative, so the
    # screen shows both rather than only the discount.
    assert Decimal(sonnet["cache_write_5m_per_million"]) == Decimal("3.75")
    assert Decimal(sonnet["cache_write_1h_per_million"]) == Decimal("6")
    assert Decimal(sonnet["cache_write_5m_per_million"]) > Decimal(sonnet["input_per_million"])


def test_only_a_provider_that_offers_a_longer_cache_has_a_one_hour_price(client):
    rows = _models(client.get("/api/pricing").json())
    # Anthropic lets a caller pay more for an entry that lives an hour. OpenAI
    # and Gemini do not offer the choice, so repeating their 5-minute number
    # under a 1-hour heading would invent a product.
    assert rows["claude-opus-4-8"]["cache_write_1h_per_million"] is not None
    assert rows["gpt-5.6-sol"]["cache_write_1h_per_million"] is None
    assert rows["gemini-2.5-pro"]["cache_write_1h_per_million"] is None


def test_batch_rates_are_half_where_a_batch_api_exists(client):
    rows = _models(client.get("/api/pricing").json())
    sonnet = rows["claude-sonnet-4-6"]
    assert Decimal(sonnet["input_batch_per_million"]) == Decimal("1.5")
    assert Decimal(sonnet["output_batch_per_million"]) == Decimal("7.5")
    assert Decimal(rows["gemini-2.5-pro"]["input_batch_per_million"]) == Decimal("0.625")


def test_the_providers_a_reader_came_for_are_listed_first(client):
    body = client.get("/api/pricing").json()
    order = [p["provider"] for p in body["providers"]]
    assert order[:3] == ["anthropic", "openai", "google"]
    # ...and the rest alphabetically by label, not by internal key.
    rest = [p["label"] for p in body["providers"][3:]]
    assert rest == sorted(rest, key=str.lower)


def test_bedrock_is_grouped_by_the_vendor_whose_model_it_is(client):
    body = client.get("/api/pricing").json()
    bedrock = next(p for p in body["providers"] if p["provider"] == "bedrock")
    vendors = {v["vendor"]: v for v in bedrock["vendors"]}
    # AWS resells a dozen vendors' models and its own pricing page is a tab per
    # vendor. Anything less than that is a subset pretending to be a catalogue.
    assert {"Amazon", "Anthropic", "Meta", "Mistral", "OpenAI"} <= set(vendors)
    assert sum(len(v["models"]) for v in bedrock["vendors"]) > 60
    # Amazon's own models have no vendor in AWS's price list; they are not left
    # under a blank heading.
    assert "" not in vendors and None not in vendors
    # Every other provider sells only its own models and has no vendor split.
    assert next(p for p in body["providers"] if p["provider"] == "anthropic")["vendors"] is None


def test_a_bedrock_rate_aws_does_not_publish_is_left_blank(client):
    body = client.get("/api/pricing").json()
    bedrock = next(p for p in body["providers"] if p["provider"] == "bedrock")
    anthropic = next(v for v in bedrock["vendors"] if v["vendor"] == "Anthropic")
    # AWS's price list carries input prices for the older Claude models and no
    # output prices at all. Inventing one would be worse than the gap.
    assert any(m["output_per_million"] is None for m in anthropic["models"])
    assert all(m["input_per_million"] is not None for m in anthropic["models"])


def test_llama_is_listed_as_a_family_across_its_hosts(client):
    body = client.get("/api/pricing").json()
    families = {f["label"]: f for f in body["families"]}
    llama = families["Llama 3.1 70B Instruct"]
    # Meta sells no inference of its own, so it is a vendor of weights rather
    # than a provider with rates. The finding is the spread between its hosts.
    assert llama["vendor"] == "Meta"
    hosts = [h["label"] for h in llama["hosts"]]
    assert len(hosts) >= 4
    rates = [Decimal(h["input_per_million"]) for h in llama["hosts"]]
    assert rates == sorted(rates), "cheapest host first — that is the point"
    assert max(rates) / min(rates) > 2  # the same weights, at twice the price


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
