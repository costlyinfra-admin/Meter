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

PRICING_VERSION = "2026-09-25"

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
#
# Two of these were wrong for months in the direction that matters most. Claude
# Opus 4.8 was carried at $15/$75 — the Opus 4.1 rate — when it is $5/$25, so
# every hook-metered Opus call was costed at three times what it billed. Haiku
# 4.5 was carried at the retired 3.5 rate. Neither showed up as an obviously
# broken number; both showed up as a reconciliation delta nobody had traced.
#
# Prices are list, standard context, global routing, non-batch. Where a provider
# tiers by context length the SHORT-context rate is used, because that is what
# almost all traffic is and overstating it would inflate every saving computed
# from it. PROVIDER_SOURCES records where each table came from and when.
_PRICES: dict[str, tuple[str, str]] = {
    # --- Anthropic ---------------------------------------------------------
    "claude-fable-5-1": ("10", "50"),
    "claude-mythos-5-1": ("10", "50"),
    "claude-fable-5": ("10", "50"),
    "claude-mythos-5": ("10", "50"),
    "claude-opus-5-5": ("4", "20"),
    "claude-opus-5": ("5", "25"),
    "claude-opus-4-8": ("5", "25"),
    "claude-opus-4-7": ("5", "25"),
    "claude-opus-4-6": ("5", "25"),
    "claude-opus-4-5": ("5", "25"),
    "claude-opus-4-1": ("15", "75"),
    "claude-opus-4": ("15", "75"),
    "claude-sonnet-5": ("2", "10"),
    "claude-sonnet-4-6": ("3", "15"),
    "claude-sonnet-4-5": ("3", "15"),
    "claude-sonnet-4": ("3", "15"),
    "claude-haiku-4-5": ("1", "5"),
    "claude-haiku-3-5": ("0.80", "4"),
    # --- OpenAI ------------------------------------------------------------
    "gpt-6-astra": ("10", "50"),
    "gpt-6-sol": ("2", "10"),
    "gpt-6-luna": ("0.10", "0.50"),
    "gpt-5.6-cyber": ("12.50", "75"),
    "gpt-5.6-sol": ("4", "20"),
    "gpt-5.6-terra": ("2", "12"),
    "gpt-5.6-luna": ("0.20", "1.20"),
    "gpt-5.5": ("5", "30"),
    "gpt-5.5-pro": ("30", "180"),
    "gpt-5.4": ("2.50", "15"),
    "gpt-5.4-mini": ("0.75", "4.50"),
    "gpt-5.4-nano": ("0.20", "1.25"),
    "gpt-5.2": ("1.75", "14"),
    "gpt-5.1": ("1.25", "10"),
    "gpt-5": ("1.25", "10"),
    "gpt-5-mini": ("0.25", "2"),
    "gpt-5-nano": ("0.05", "0.40"),
    "gpt-4.1": ("2", "8"),
    "gpt-4.1-mini": ("0.40", "1.60"),
    "gpt-4.1-nano": ("0.10", "0.40"),
    "gpt-4o": ("2.5", "10"),
    "gpt-4o-mini": ("0.15", "0.60"),
    "o3": ("2", "8"),
    "o3-mini": ("1.10", "4.40"),
    "o4-mini": ("1.10", "4.40"),
    # --- Google Gemini (short-context list prices) -------------------------
    "gemini-3.8-flash": ("0.75", "3.75"),
    "gemini-3.7-flash": ("0.75", "3.75"),
    "gemini-3.6-flash": ("0.75", "3.75"),
    "gemini-3.5-flash": ("1.50", "9"),
    "gemini-3.5-flash-lite": ("0.30", "2.50"),
    "gemini-3.1-flash-lite": ("0.25", "1.50"),
    "gemini-3.1-pro-preview": ("2", "12"),
    "gemini-2.5-pro": ("1.25", "10"),
    "gemini-2.5-flash": ("0.30", "2.50"),
    "gemini-2.5-flash-lite": ("0.10", "0.40"),
    "gemini-2.0-flash": ("0.10", "0.40"),
}

#: Where each provider publishes its rates, and when this table was last
#: reconciled against it. A price book nobody can date is one nobody should
#: trust, and the pricing screen shows both.
PROVIDER_SOURCES: dict[str, dict] = {
    "anthropic": {
        "label": "Anthropic",
        "url": "https://claude.com/pricing",
        "checked": "2026-09-25",
    },
    "openai": {
        "label": "OpenAI",
        "url": "https://developers.openai.com/api/docs/pricing",
        "checked": "2026-09-25",
    },
    "google": {
        "label": "Google Gemini",
        "url": "https://ai.google.dev/gemini-api/docs/pricing",
        "checked": "2026-09-25",
    },
    "together": {"label": "Together AI", "url": "https://www.together.ai/pricing"},
    "fireworks": {"label": "Fireworks AI", "url": "https://fireworks.ai/pricing"},
    "groq": {"label": "Groq", "url": "https://groq.com/pricing"},
    "deepinfra": {"label": "DeepInfra", "url": "https://deepinfra.com/pricing"},
    "openrouter": {"label": "OpenRouter", "url": "https://openrouter.ai/models"},
    "bedrock": {"label": "AWS Bedrock", "url": "https://aws.amazon.com/bedrock/pricing/"},
    "mistral": {"label": "Mistral", "url": "https://mistral.ai/pricing"},
    "xai": {"label": "xAI", "url": "https://x.ai/api"},
    "perplexity": {"label": "Perplexity", "url": "https://docs.perplexity.ai/guides/pricing"},
    "cohere": {"label": "Cohere", "url": "https://cohere.com/pricing"},
}

#: Which provider a single-vendor model belongs to, for grouping on the pricing
#: screen. The OSS table is already keyed by provider; this covers the rest.
_MODEL_VENDOR = [
    ("claude-", "anthropic"),
    ("gpt-", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("o4", "openai"),
    ("gemini-", "google"),
]

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
    "claude-opus-5-5": "claude-sonnet-5",
    "claude-opus-5": "claude-sonnet-5",
    "claude-opus-4-8": "claude-sonnet-4-6",
    "claude-sonnet-5": "claude-haiku-4-5",
    "claude-sonnet-4-6": "claude-haiku-4-5",
    "gpt-6-astra": "gpt-6-sol",
    "gpt-6-sol": "gpt-6-luna",
    "gpt-5.6-sol": "gpt-5.6-terra",
    "gpt-5.6-terra": "gpt-5.6-luna",
    "gpt-5.4": "gpt-5.4-mini",
    "gpt-5.4-mini": "gpt-5.4-nano",
    "gpt-5": "gpt-5-mini",
    "gpt-5-mini": "gpt-5-nano",
    "gpt-4.1": "gpt-4.1-mini",
    "gpt-4.1-mini": "gpt-4.1-nano",
    "gpt-4o": "gpt-4o-mini",
    "gemini-3.5-flash": "gemini-3.5-flash-lite",
    "gemini-3.1-pro-preview": "gemini-3.1-flash-lite",
    "gemini-2.5-pro": "gemini-2.5-flash",
    "gemini-2.5-flash": "gemini-2.5-flash-lite",
}

# Cache/batch discount model (opt spec §7.1) — versioned with the price tables.
# CACHE_READ_MULT is the fraction of the input rate charged when input tokens are
# served from the provider's prompt cache. Only providers listed here support
# prompt caching we can price; others are left out so the measured optimizer never
# claims a caching saving we can't stand behind (drift shows up as a recon gap).
CACHE_READ_MULT: dict[str, Decimal] = {
    "anthropic": Decimal("0.10"),  # cache reads 10% of input
    "openai": Decimal("0.10"),  # current families; the 4o era is 0.50, below
    # Checked against the published table rather than recalled: gemini-2.5-flash
    # is $0.30 in and $0.03 cached, 2.5-pro $1.25 and $0.125. Both are a tenth,
    # not the quarter this said, which understated every Google caching finding.
    "google": Decimal("0.10"),
}

# ...except where it is not a provider-wide fact at all. Anthropic's newest
# models read cache at a quarter or a half of the usual tenth, and OpenAI's
# gpt-4o generation reads at FIVE times it. A provider-level constant was right
# by accident for the three models the book used to hold; it is wrong for a
# quarter of the models it holds now, so the model wins where it says so.
CACHE_READ_MULT_BY_MODEL: dict[str, Decimal] = {
    "claude-fable-5-1": Decimal("0.025"),
    "claude-mythos-5-1": Decimal("0.025"),
    "claude-opus-5-5": Decimal("0.05"),
    "gpt-4o": Decimal("0.50"),
    "gpt-4o-mini": Decimal("0.50"),
    "gpt-4.1": Decimal("0.25"),
    "gpt-4.1-mini": Decimal("0.25"),
    "gpt-4.1-nano": Decimal("0.25"),
    "o3": Decimal("0.25"),
    "o3-mini": Decimal("0.50"),
    "o4-mini": Decimal("0.25"),
}

# The smallest prefix a provider will cache AT ALL, per model. Below it there is
# no cache to read from, so a finding is not a conservative estimate — it is an
# instruction that cannot be carried out.
#
# Meter's own _MIN_PREFIX_TOKENS is a "worth the trouble" floor and was doing
# duty as both. It is 1,000, under every real minimum here, and Haiku's is four
# times it: a 1,200-token Haiku prefix was being offered as a saving when the
# provider would not have cached a byte of it.
#
# A model absent from this table has no published minimum we have checked, so
# the detector falls back to its own floor rather than inventing one.
MIN_CACHEABLE_TOKENS: dict[str, int] = {
    "claude-fable-5-1": 512,
    "claude-mythos-5-1": 512,
    "claude-fable-5": 512,
    "claude-mythos-5": 512,
    "claude-opus-5-5": 512,
    "claude-opus-5": 512,
    "claude-opus-4-8": 1024,
    "claude-opus-4-7": 2048,
    "claude-opus-4-6": 4096,
    "claude-opus-4-5": 4096,
    "claude-opus-4-1": 1024,
    "claude-opus-4": 1024,
    "claude-sonnet-5": 1024,
    "claude-sonnet-4-6": 1024,
    "claude-sonnet-4-5": 1024,
    "claude-sonnet-4": 1024,
    # The small models need a BIGGER prefix, not a smaller one.
    "claude-haiku-4-5": 4096,
    "claude-haiku-3-5": 2048,
    # OpenAI's pre-5.6 family varies with tools and images; 1,024 is the
    # documented floor and the safe reading of it.
    "gpt-4o": 1024,
    "gpt-4o-mini": 1024,
    "gpt-5.6-sol": 1024,
    "gpt-5.6-terra": 1024,
    "gpt-5.6-luna": 1024,
    "gemini-3.8-flash": 4096,
    "gemini-3.7-flash": 4096,
    "gemini-3.6-flash": 4096,
    "gemini-3.5-flash": 4096,
    "gemini-3.1-pro-preview": 4096,
    "gemini-2.5-pro": 2048,
    "gemini-2.5-flash": 2048,
}

# How a customer turns caching on, which is not the same question everywhere.
# Anthropic caches what you mark; OpenAI and Gemini 2.5+ cache automatically,
# so a prefix going uncached there is a prompt-shape problem and "set
# cache_control" is advice for a different API.
CACHE_IS_AUTOMATIC = {"openai", "google"}
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


def min_cacheable_tokens(model: str, provider: Optional[str] = None) -> Optional[int]:
    """Smallest prefix `model` will cache, or None when we have not checked."""
    return MIN_CACHEABLE_TOKENS.get(model)


def cache_is_automatic(provider: Optional[str]) -> bool:
    """True where the provider caches without being asked."""
    return provider in CACHE_IS_AUTOMATIC


def cache_read_mult(provider: Optional[str], model: Optional[str] = None) -> Optional[Decimal]:
    """Cache-read discount multiplier, or None if this provider isn't priced.

    The model's own rate wins where it differs from the provider's, which is
    often enough now that reading only the provider would be a real error.
    """
    if provider is None:
        return None
    if provider not in CACHE_READ_MULT:
        return None  # no priced cache discount -> never claim one
    if model is not None and model in CACHE_READ_MULT_BY_MODEL:
        return CACHE_READ_MULT_BY_MODEL[model]
    return CACHE_READ_MULT[provider]


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


def _vendor_of(model: str) -> Optional[str]:
    """Which provider publishes this single-vendor model."""
    for prefix, provider in _MODEL_VENDOR:
        if model.startswith(prefix):
            return provider
    return None


def catalog() -> dict:
    """The whole price book, as the pricing screen shows it.

    Read-only and tenant-independent: these are published list rates, not
    anybody's spend. Derived from the same tables that cost hook-metered
    traffic, so the screen cannot drift from what Meter actually charges — the
    point of showing it at all is that a customer can check our arithmetic.
    """
    rows: dict[str, list] = {}
    for model, (r_in, r_out) in _PRICES.items():
        provider = _vendor_of(model)
        if provider is None:
            continue
        rows.setdefault(provider, []).append(_catalog_row(provider, model, r_in, r_out))
    for (provider, model), (r_in, r_out) in _OSS_PRICES.items():
        rows.setdefault(provider, []).append(_catalog_row(provider, model, r_in, r_out))

    providers = []
    for provider, models in rows.items():
        meta = PROVIDER_SOURCES.get(provider, {})
        models.sort(key=lambda m: (Decimal(m["input_per_million"]), m["model"]))
        providers.append(
            {
                "provider": provider,
                "label": meta.get("label", provider),
                "source_url": meta.get("url"),
                # None where the table has not been reconciled against the
                # provider's own page. Saying "checked" of a rate nobody has
                # checked is the failure this column exists to prevent.
                "checked": meta.get("checked"),
                "models": models,
            }
        )
    providers.sort(key=lambda p: p["label"].lower())
    return {"version": PRICING_VERSION, "providers": providers}


def _catalog_row(provider: str, model: str, r_in: str, r_out: str) -> dict:
    read = cache_read_mult(provider, model)
    family = _MODEL_FAMILY.get((provider, model))
    return {
        "model": model,
        "input_per_million": r_in,
        "output_per_million": r_out,
        # Absent, not zero, where the provider has no priced prompt cache: a
        # blank column says "we do not claim to know", a 0 says "free".
        "cache_read_per_million": str(Decimal(r_in) * read) if read is not None else None,
        "cache_write_per_million": (
            str(Decimal(r_in) * cache_write_mult(provider)) if read is not None else None
        ),
        "cache_read_mult": str(read) if read is not None else None,
        "min_cacheable_tokens": MIN_CACHEABLE_TOKENS.get(model),
        "cache_is_automatic": provider in CACHE_IS_AUTOMATIC if read is not None else None,
        "downgrade_target": _DOWNGRADE_TARGET.get(model),
        "open_weights_family": _FAMILY_LABEL.get(family) if family else None,
    }
