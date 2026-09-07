"""Versioned per-model pricing for hook-metered tokens.

The hook reports tokens; cost is computed here from internal price tables. These
MUST be kept current as model prices change — drift surfaces immediately as a
bill-reconciliation delta (design §12). Prices are USD per 1M tokens (input, output).
Unknown models cost 0, which shows up as a reconciliation gap rather than a wrong
per-feature number.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

PRICING_VERSION = "2026-06-01"

# Providers that bill (or whose tokens we price) PER TOKEN — closed-source plus
# hosted open-source aggregators. These flow through the priced/hook pipeline.
# Self-hosted GPU pools are NOT here: they have no per-token price and are costed
# by allocating an infra-cost pool (see compute.py), not by this table.
PRICED_PROVIDERS = {
    "anthropic",
    "openai",
    "google",
    "together",
    "fireworks",
    "groq",
    "openrouter",
    "deepinfra",
    "bedrock",
    "mistral",
    "xai",
    "perplexity",
    "cohere",
}

# Provider-agnostic model prices (single-vendor models).
# model -> (input_per_million, output_per_million) in USD.
_PRICES: dict[str, tuple[str, str]] = {
    # Anthropic
    "claude-opus-4-8": ("15", "75"),
    "claude-sonnet-4-6": ("3", "15"),
    "claude-haiku-4-5": ("0.80", "4"),
    # OpenAI
    "gpt-4o": ("2.5", "10"),
    "gpt-4o-mini": ("0.15", "0.60"),
    # Google Gemini (standard-context list prices)
    "gemini-2.5-pro": ("1.25", "10"),
    "gemini-2.5-flash": ("0.30", "2.50"),
    "gemini-2.5-flash-lite": ("0.10", "0.40"),
    "gemini-2.0-flash": ("0.10", "0.40"),
}

# Hosted open-source: the SAME open weights cost different amounts depending on
# who serves them, so these are keyed by (provider, model). Rates are public
# list prices per 1M tokens and approximate — drift surfaces as a reconciliation
# delta, never as a silently-wrong per-feature number.
_OSS_PRICES: dict[tuple[str, str], tuple[str, str]] = {
    # Together AI
    ("together", "meta-llama-3.1-70b-instruct"): ("0.88", "0.88"),
    ("together", "meta-llama-3.1-8b-instruct"): ("0.18", "0.18"),
    ("together", "mixtral-8x7b-instruct"): ("0.60", "0.60"),
    ("together", "qwen2.5-72b-instruct"): ("1.20", "1.20"),
    # Fireworks AI
    ("fireworks", "llama-v3p1-70b-instruct"): ("0.90", "0.90"),
    ("fireworks", "llama-v3p1-8b-instruct"): ("0.20", "0.20"),
    ("fireworks", "mixtral-8x7b-instruct"): ("0.50", "0.50"),
    # Groq
    ("groq", "llama-3.1-70b-versatile"): ("0.59", "0.79"),
    ("groq", "llama-3.1-8b-instant"): ("0.05", "0.08"),
    # AWS Bedrock (Llama)
    ("bedrock", "meta.llama3-1-70b-instruct-v1:0"): ("0.72", "0.72"),
    ("bedrock", "meta.llama3-1-8b-instruct-v1:0"): ("0.22", "0.22"),
    # DeepInfra
    ("deepinfra", "meta-llama-3.1-70b-instruct"): ("0.35", "0.40"),
    # OpenRouter (representative)
    ("openrouter", "meta-llama-3.1-70b-instruct"): ("0.59", "0.79"),
    # Single-vendor frontier models on their own APIs (representative list prices,
    # per 1M tokens; approximate — drift surfaces as a reconciliation delta).
    ("mistral", "mistral-large-latest"): ("2", "6"),
    ("mistral", "mistral-small-latest"): ("0.20", "0.60"),
    ("xai", "grok-4"): ("3", "15"),
    ("xai", "grok-3-mini"): ("0.30", "0.50"),
    ("perplexity", "sonar"): ("1", "1"),
    ("perplexity", "sonar-pro"): ("3", "15"),
    ("cohere", "command-r-plus"): ("2.50", "10"),
    ("cohere", "command-r"): ("0.15", "0.60"),
}

_MILLION = Decimal("1000000")

# The SAME open weights are served by multiple hosts under DIFFERENT model ids.
# This maps each host's specific (provider, model) to a canonical family so we can
# compare the price of identical weights across providers — cross-provider
# arbitrage (opt spec §16, M-opt-8). Frontier models (one vendor) aren't here.
_MODEL_FAMILY: dict[tuple[str, str], str] = {
    ("together", "meta-llama-3.1-70b-instruct"): "llama-3.1-70b-instruct",
    ("deepinfra", "meta-llama-3.1-70b-instruct"): "llama-3.1-70b-instruct",
    ("openrouter", "meta-llama-3.1-70b-instruct"): "llama-3.1-70b-instruct",
    ("fireworks", "llama-v3p1-70b-instruct"): "llama-3.1-70b-instruct",
    ("groq", "llama-3.1-70b-versatile"): "llama-3.1-70b-instruct",
    ("bedrock", "meta.llama3-1-70b-instruct-v1:0"): "llama-3.1-70b-instruct",
    ("together", "meta-llama-3.1-8b-instruct"): "llama-3.1-8b-instruct",
    ("fireworks", "llama-v3p1-8b-instruct"): "llama-3.1-8b-instruct",
    ("groq", "llama-3.1-8b-instant"): "llama-3.1-8b-instruct",
    ("bedrock", "meta.llama3-1-8b-instruct-v1:0"): "llama-3.1-8b-instruct",
    ("together", "mixtral-8x7b-instruct"): "mixtral-8x7b-instruct",
    ("fireworks", "mixtral-8x7b-instruct"): "mixtral-8x7b-instruct",
}

# Human-friendly family labels for the UI.
_FAMILY_LABEL: dict[str, str] = {
    "llama-3.1-70b-instruct": "Llama 3.1 70B Instruct",
    "llama-3.1-8b-instruct": "Llama 3.1 8B Instruct",
    "mixtral-8x7b-instruct": "Mixtral 8x7B Instruct",
}

# One-step model right-sizing: a cheaper same-vendor tier that often preserves
# quality for simpler tasks (opt spec §16, M-opt-7). Quality-gated — a CEILING,
# not a guaranteed saving. One step down keeps the recommendation credible.
_DOWNGRADE_TARGET: dict[str, str] = {
    "claude-opus-4-8": "claude-sonnet-4-6",
    "claude-sonnet-4-6": "claude-haiku-4-5",
    "gpt-4o": "gpt-4o-mini",
    "gemini-2.5-pro": "gemini-2.5-flash",
    "gemini-2.5-flash": "gemini-2.5-flash-lite",
}

# Cache/batch discount model (opt spec §7.1) — versioned with the price tables.
# CACHE_READ_MULT is the fraction of the input rate charged when input tokens are
# served from the provider's prompt cache. Only providers listed here support
# prompt caching we can price; others are left out so the measured optimizer never
# claims a caching saving we can't stand behind (drift shows up as a recon gap).
CACHE_READ_MULT: dict[str, Decimal] = {
    "anthropic": Decimal("0.10"),  # cache reads ~10% of input
    "openai": Decimal("0.50"),  # cached input ~50% of input
    "google": Decimal("0.25"),  # context cache ~25% of input
}
# Reserved for the later batch-eligibility detector (opt spec §12): async/
# non-latency-sensitive calls run ~50% cheaper on batch APIs.
BATCH_MULT = Decimal("0.50")


# CACHE_WRITE_MULT is the multiple of the input rate charged to WRITE tokens into
# the cache (cache creation). Anthropic prices writes by how long the entry lives:
# a 5-minute TTL costs 1.25x input, a 1-hour TTL 2x. Providers without a separate
# write charge bill it as ordinary input (1.0).
CACHE_WRITE_MULT: dict[str, Decimal] = {
    "anthropic": Decimal("1.25"),  # 5-minute TTL (the default)
}
CACHE_WRITE_1H_MULT: dict[str, Decimal] = {
    "anthropic": Decimal("2.0"),  # 1-hour TTL
}


def cache_read_mult(provider: Optional[str]) -> Optional[Decimal]:
    """Cache-read discount multiplier for a provider, or None if it isn't priced."""
    if provider is None:
        return None
    return CACHE_READ_MULT.get(provider)


def cache_write_mult(provider: Optional[str], ttl: str = "5m") -> Decimal:
    """Cache-WRITE multiplier on the input rate, by cache TTL ("5m" or "1h").

    1.0 means the provider bills writes as ordinary input.
    """
    table = CACHE_WRITE_1H_MULT if ttl == "1h" else CACHE_WRITE_MULT
    return table.get(provider or "", Decimal("1"))


def rate_in(model: str, provider: Optional[str] = None) -> Decimal:
    """USD per single input token for (model, provider), or 0 if unpriced."""
    rates = _rates(model, provider)
    if rates is None:
        return Decimal("0")
    return Decimal(rates[0]) / _MILLION


def rate_out(model: str, provider: Optional[str] = None) -> Decimal:
    """USD per single output token for (model, provider), or 0 if unpriced."""
    rates = _rates(model, provider)
    if rates is None:
        return Decimal("0")
    return Decimal(rates[1]) / _MILLION


def _rates(model: str, provider: Optional[str]) -> Optional[tuple[str, str]]:
    """Provider-specific OSS price first, then the provider-agnostic table."""
    if provider is not None:
        oss = _OSS_PRICES.get((provider, model))
        if oss is not None:
            return oss
    return _PRICES.get(model)


def is_priced(model: str, provider: Optional[str] = None) -> bool:
    return _rates(model, provider) is not None


def price(model: str, tokens_in: int, tokens_out: int, provider: Optional[str] = None) -> Decimal:
    """Cost of a call in USD, or 0 for an unknown (provider, model)."""
    rates = _rates(model, provider)
    if rates is None:
        return Decimal("0")
    rate_in, rate_out = rates
    cost = (Decimal(tokens_in) / _MILLION) * Decimal(rate_in) + (
        Decimal(tokens_out) / _MILLION
    ) * Decimal(rate_out)
    return cost.quantize(Decimal("0.0001"))


def cheapest_equivalent(
    provider: str, model: str, tokens_in: int, tokens_out: int
) -> Optional[dict]:
    """The cheapest same-weights host for (provider, model) at this token mix.

    Returns None when the model isn't a known multi-host open model, or when the
    current host is already the cheapest. Both costs are list-price, price-book
    numbers priced at the feature's ACTUAL token mix, so the saving is the exact
    rate advantage of identical weights — no quality change, no invented percentage.
    """
    family = _MODEL_FAMILY.get((provider, model))
    if family is None:
        return None
    current_cost = price(model, tokens_in, tokens_out, provider)
    if current_cost <= 0:
        return None
    best = None
    for (alt_provider, alt_model), fam in _MODEL_FAMILY.items():
        if fam != family or (alt_provider, alt_model) == (provider, model):
            continue
        alt_cost = price(alt_model, tokens_in, tokens_out, alt_provider)
        if best is None or alt_cost < best["cost"]:
            best = {"provider": alt_provider, "model": alt_model, "cost": alt_cost}
    if best is None or best["cost"] >= current_cost:
        return None
    return {
        "family": family,
        "family_label": _FAMILY_LABEL.get(family, family),
        "from_provider": provider,
        "to_provider": best["provider"],
        "to_model": best["model"],
        "current_cost": current_cost,
        "alt_cost": best["cost"],
        "savings": current_cost - best["cost"],
    }


def downgrade_ceiling(model: str, tokens_in: int, tokens_out: int) -> Optional[dict]:
    """Cheaper same-vendor tier for `model`, plus the FRACTION of cost it would save
    at this token mix (opt spec §16, M-opt-7). None if there's no cheaper tier or no
    tokens to price the ratio. Quality-gated — the caller applies the fraction to the
    feature's real spend to get a ceiling, not a guaranteed saving. Rates are
    provider-agnostic here (these are single-vendor frontier models)."""
    target = _DOWNGRADE_TARGET.get(model)
    if target is None:
        return None
    current = price(model, tokens_in, tokens_out)
    cheaper = price(target, tokens_in, tokens_out)
    if current <= 0 or cheaper >= current:
        return None
    return {"target": target, "save_fraction": float((current - cheaper) / current)}
