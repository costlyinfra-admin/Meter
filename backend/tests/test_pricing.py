"""Pricing table sanity."""

from __future__ import annotations

from decimal import Decimal

import pytest
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
    assert pricing.cache_read_mult("openai") == Decimal("0.10")
    # Providers with no priced cache discount return None (never claim a saving).
    assert pricing.cache_read_mult("together") is None
    assert pricing.cache_read_mult(None) is None


def test_a_model_that_caches_at_a_different_rate_overrides_its_provider():
    # It is not a provider-wide fact. gpt-4o reads cache at HALF the input rate
    # where the current families read at a tenth — five times the difference,
    # in the direction that overstates a caching saving. Opus 5.5 goes the
    # other way, at a twentieth.
    assert pricing.cache_read_mult("openai", "gpt-4o") == Decimal("0.50")
    assert pricing.cache_read_mult("openai", "gpt-5.6-sol") == Decimal("0.10")
    assert pricing.cache_read_mult("anthropic", "claude-opus-5-5") == Decimal("0.05")
    assert pricing.cache_read_mult("anthropic", "claude-fable-5-1") == Decimal("0.025")
    # A model with no override falls back to its provider's rate...
    assert pricing.cache_read_mult("anthropic", "claude-sonnet-4-6") == Decimal("0.10")
    # ...and a provider with no cache discount stays None whatever the model.
    assert pricing.cache_read_mult("together", "gpt-4o") is None


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
    # sonnet ($3/$15) -> haiku ($1/$5) at 1M in / 1M out:
    # sonnet = $18, haiku = $6 -> saves (18-6)/18 = 0.6667...
    dc = pricing.downgrade_ceiling("claude-sonnet-4-6", 1_000_000, 1_000_000)
    assert dc["target"] == "claude-haiku-4-5"
    assert round(dc["save_fraction"], 4) == 0.6667
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


# ---------------------------------------------------------------------------
# The cache model, checked against published rates rather than recalled
# ---------------------------------------------------------------------------
#: (provider, model, input $/M, cached-read $/M) as the providers publish them.
#: The multiplier is a RATIO, so it belongs next to the two prices it comes
#: from — "google is about a quarter" was wrong for two years and nothing said so.
_PUBLISHED_CACHE_RATES = [
    ("anthropic", "claude-sonnet-4-6", "3", "0.30"),
    ("google", "gemini-2.5-flash", "0.30", "0.03"),
    ("google", "gemini-2.5-pro", "1.25", "0.125"),
]


@pytest.mark.parametrize("provider, model, rate_in, cached", _PUBLISHED_CACHE_RATES)
def test_the_cache_read_multiplier_is_the_published_ratio(provider, model, rate_in, cached):
    assert pricing.cache_read_mult(provider) == Decimal(cached) / Decimal(rate_in)


def test_a_small_model_needs_a_bigger_prefix_before_it_is_cached_at_all():
    # The intuition runs the wrong way, which is why this is pinned: Haiku's
    # minimum is four times Sonnet's, and four times Meter's own floor.
    assert pricing.min_cacheable_tokens("claude-haiku-4-5") == 4096
    assert pricing.min_cacheable_tokens("claude-sonnet-4-6") == 1024
    assert pricing.min_cacheable_tokens("gemini-2.5-pro") == 2048
    # An unchecked model reports no minimum rather than a guessed one.
    assert pricing.min_cacheable_tokens("mistral-large-latest") is None


def test_which_providers_cache_without_being_asked():
    # Anthropic caches what you mark; the other two cache by themselves. The
    # fix a finding recommends depends entirely on which of those it is.
    assert pricing.cache_is_automatic("openai")
    assert pricing.cache_is_automatic("google")
    assert not pricing.cache_is_automatic("anthropic")


def test_a_cache_write_costs_more_than_not_caching():
    # 5-minute and 1-hour entries price differently, and a provider with no
    # separate write charge bills it as ordinary input.
    assert pricing.cache_write_mult("anthropic") == Decimal("1.25")
    assert pricing.cache_write_mult("anthropic", "1h") == Decimal("2.0")
    assert pricing.cache_write_mult("openai") == Decimal("1")
