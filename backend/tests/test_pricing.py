"""Pricing table sanity."""

from __future__ import annotations

from decimal import Decimal

from meter import pricing


def test_known_model_costs_tokens():
    # sonnet: $3 / 1M input, $15 / 1M output
    assert pricing.price("claude-sonnet-4-6", 1_000_000, 0) == Decimal("3.0000")
    assert pricing.price("claude-sonnet-4-6", 0, 1_000_000) == Decimal("15.0000")
    assert pricing.price("claude-sonnet-4-6", 100_000_000, 0) == Decimal("300.0000")


def test_unknown_model_is_zero():
    assert pricing.price("mystery-model", 1_000_000, 1_000_000) == Decimal("0")
    assert not pricing.is_priced("mystery-model")


def test_input_rate_per_token():
    # $3 / 1M input tokens -> $0.000003 per token.
    assert pricing.rate_in("claude-sonnet-4-6") == Decimal("3") / Decimal("1000000")
    assert pricing.rate_in("mystery-model") == Decimal("0")


def test_cache_read_multiplier_is_provider_specific():
    assert pricing.cache_read_mult("anthropic") == Decimal("0.10")
    assert pricing.cache_read_mult("openai") == Decimal("0.50")
    # Providers with no priced cache discount return None (never claim a saving).
    assert pricing.cache_read_mult("together") is None
    assert pricing.cache_read_mult(None) is None


def test_cheapest_equivalent_finds_a_cheaper_host():
    # Same Llama-3.1-70B weights: Together $0.88/$0.88 vs DeepInfra $0.35/$0.40.
    alt = pricing.cheapest_equivalent("together", "meta-llama-3.1-70b-instruct", 1_000_000, 0)
    assert alt is not None
    assert alt["to_provider"] == "deepinfra"
    assert alt["current_cost"] == Decimal("0.8800")  # 1M in @ $0.88
    assert alt["alt_cost"] == Decimal("0.3500")  # 1M in @ $0.35
    assert alt["savings"] == Decimal("0.5300")
    assert alt["family_label"] == "Llama 3.1 70B Instruct"


def test_cheapest_equivalent_none_when_already_cheapest_or_unknown():
    # DeepInfra is already the cheapest host for this model.
    assert (
        pricing.cheapest_equivalent("deepinfra", "meta-llama-3.1-70b-instruct", 1_000_000, 0)
        is None
    )
    # Frontier models aren't multi-host in the table -> no arbitrage.
    assert pricing.cheapest_equivalent("anthropic", "claude-sonnet-4-6", 1_000_000, 0) is None


def test_downgrade_ceiling_fraction():
    # sonnet ($3/$15) -> haiku ($0.80/$4) at 1M in / 1M out:
    # sonnet = $18, haiku = $4.80 -> saves (18-4.8)/18 = 0.7333...
    dc = pricing.downgrade_ceiling("claude-sonnet-4-6", 1_000_000, 1_000_000)
    assert dc["target"] == "claude-haiku-4-5"
    assert round(dc["save_fraction"], 4) == 0.7333
    # gpt-4o -> gpt-4o-mini exists; the cheapest tier has no target.
    assert pricing.downgrade_ceiling("gpt-4o", 1_000_000, 0)["target"] == "gpt-4o-mini"
    assert pricing.downgrade_ceiling("gpt-4o-mini", 1_000_000, 0) is None
    assert pricing.downgrade_ceiling("claude-haiku-4-5", 1_000_000, 0) is None


def test_hosted_open_source_is_priced_per_provider():
    # Same open weights, different host -> different price; keyed by (provider, model).
    assert pricing.price(
        "meta-llama-3.1-70b-instruct", 1_000_000, 0, provider="together"
    ) == Decimal("0.8800")
    assert pricing.price("llama-3.1-70b-versatile", 0, 1_000_000, provider="groq") == Decimal(
        "0.7900"
    )
    assert pricing.is_priced("meta-llama-3.1-70b-instruct", provider="together")

    # Hosted-OSS providers are recognized as priced.
    assert {"together", "fireworks", "groq", "bedrock"} <= pricing.PRICED_PROVIDERS


def test_gemini_is_priced():
    # gemini-2.5-flash: $0.30/M input, $2.50/M output
    assert pricing.price("gemini-2.5-flash", 1_000_000, 0) == Decimal("0.3000")
    assert pricing.price("gemini-2.5-flash", 0, 1_000_000) == Decimal("2.5000")
    assert pricing.is_priced("gemini-2.5-pro")
    assert "google" in pricing.PRICED_PROVIDERS


def test_open_source_model_without_provider_is_unknown():
    # The bare model name (no host) has no canonical price -> 0, not a guess.
    assert pricing.price("meta-llama-3.1-70b-instruct", 1_000_000, 0) == Decimal("0")
    assert not pricing.is_priced("meta-llama-3.1-70b-instruct")
