"""Measured optimization opportunities (opt spec §7).

Unlike the heuristic estimator ([optimize.py](optimize.py)), every number here is
*counted*: it comes from the SDK's `usage_signal` rows and is priced from the
price book ([pricing.py](pricing.py)) — never a flat percentage. The heuristic
tier remains below this as the zero-instrumentation fallback.

What is counted and what is assumed are not the same thing, and the two
detectors differ in exactly that:

  * **Repeated request candidates** (`duplicate_calls`) — the (N-1) repeats of
    an identical request. The COUNT is exact. Whether those repeats were
    avoidable is not measured: it depends on freshness, authorization,
    deliberate sampling, external state and application policy, none of which
    reaches Meter. The dollars are a list-price ceiling, because the stored
    aggregate cannot tell how much of that input was already cache-served at a
    tenth of the rate. Classified `modeled_ceiling`, never `measured`.
  * **Cacheable prompt prefix** — a large static prefix repeated across many
    uncached calls. Savings is the repeated prefix tokens priced at
    (input rate − cached-read rate), MINUS the premium on the cache writes
    serving them would require, both from the price book. `measured` only when
    the provider reported the prefix size and the SDK counted the writes;
    `modeled_ceiling` when either is missing.

The two overlap — the same input tokens counted two ways — so they are mutually
exclusive in the totals rather than summed.

Every read of `inference_cost` here goes through dashboard's counting rule —
`_ACTIVE_ENV` and `_NOT_DOUBLE_COUNTED`. A provider with both a connector and
the SDK has described one month's spend twice, and a detector that sums both
proposes a saving on money that was never spent; spend the customer marked
`ignore` is not a saving either, because it is not in any total this product
reports. The rule lives in one place for exactly this reason: it was
re-introduced here once already.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Optional

from . import dashboard, optimize_billing, pricing
from .db import app_dsn, connect, tenant_tx

# Detection thresholds (opt spec §7). Kept deliberately conservative so a surfaced
# opportunity is always worth acting on.
# A prefix worth the trouble. NOT the same question as whether the provider
# will cache it at all — pricing.MIN_CACHEABLE_TOKENS answers that, per model,
# and every published minimum is above this number. Both gates apply.
_MIN_PREFIX_TOKENS = 1000
_MIN_CACHEABLE_CALLS = 100  # ...and repeated enough to matter
_MIN_SAVINGS = 1.0  # ignore sub-dollar noise, like the heuristic tier
_MAX_TRAIL = 25  # cap the evidence trail payload

# Per-lever presentation metadata for the unified opportunity model (opt spec §18).
# `savings_type` is the canonical taxonomy:
#   measured         — guaranteed given the traffic (sums into the measured total)
#   modeled_ceiling  — measured traffic, realization depends on an assumption ("up to")
#   directional      — a symptom/estimate; never contributes to a measured total
_LEVER_META = {
    "duplicate_calls": {
        # The slug is unchanged: it keys applied actions, exclusions and the
        # per-lever rollup, and renaming it would orphan a customer's history.
        # The TITLE is what was wrong. "Duplicate calls" states a conclusion —
        # that these calls were avoidable — when what was measured is that the
        # requests were identical. Whether the second could have been served
        # from the first depends on freshness, authorization, deliberate
        # sampling, external state and application policy, none of which Meter
        # can see.
        "title": "Repeated request candidates",
        "source": "sdk",
        # Not "measured". The repeat count is exact; the SAVING is a ceiling,
        # because it assumes every repeat was safely reusable and prices it at
        # list rate against aggregates that cannot reconstruct the billed cost.
        "savings_type": "modeled_ceiling",
        "confidence_reason": (
            "Exact count of identical requests; the saving assumes each repeat "
            "was safely reusable and is priced at list rate."
        ),
    },
    "prompt_caching": {
        "title": "Prompt caching",
        "source": "sdk",
        # The default. The detector overrides it per finding: an estimated
        # prefix size, or a write side it could not price, makes this a ceiling.
        "savings_type": "measured",
        "confidence_reason": (
            "Uncached prefix tokens priced at the cache-read discount, net of "
            "what writing the cache would cost."
        ),
    },
    "provider_switch": {
        "title": "Cheaper provider",
        "source": "connector",
        "savings_type": "measured",
        "confidence_reason": "Exact rate delta on identical open weights — no quality change.",
    },
    "model_rightsizing": {
        "title": "Model right-sizing",
        "source": "connector",
        "savings_type": "modeled_ceiling",
        "confidence_reason": (
            "Grounded ceiling; realization depends on quality holding after the downgrade."
        ),
    },
}

# Heuristic (directional) opportunity name -> a stable lever slug for the unified list.
_DIRECTIONAL_LEVER = {
    "Prompt caching": "prompt_caching_est",
    "Context reduction": "context_reduction",
    "Output token reduction": "output_reduction",
    "Semantic caching": "semantic_caching",
}

# Engineering effort is a per-lever CONSTANT (opt spec §19) — the difficulty is
# inherent to the fix type, so it's deterministic, not a per-instance guess.
_LEVER_EFFORT = {
    "provider_switch": "very_low",  # point the client at a cheaper host
    "prompt_caching": "low",  # set cache_control on the static block
    "duplicate_calls": "medium",  # add a response cache keyed on the request hash
    "model_rightsizing": "high",  # needs a quality eval before switching models
}
_DEFAULT_EFFORT = "medium"  # directional/heuristic levers: fix effort is unknown

# Deterministic priority = savings × confidence weight × effort weight (opt spec §19).
# Never a black box; the two weight tables are the whole model.
_CONFIDENCE_WEIGHT = {"high": 1.0, "med": 0.6, "low": 0.3}
_EFFORT_WEIGHT = {"very_low": 1.0, "low": 0.8, "medium": 0.5, "high": 0.3}


def _priority(monthly: float, confidence: str, effort: str) -> float:
    return round(
        monthly * _CONFIDENCE_WEIGHT.get(confidence, 0.3) * _EFFORT_WEIGHT.get(effort, 0.5), 2
    )


# Exclusion groups (opt spec §22) — levers that address overlapping spend. Within a
# group present on one feature, the strongest member wins (measured beats a
# directional estimate; then higher savings) and the rest are flagged `overlaps` and
# dropped from the totals. A read-time rule, NOT a graph engine.
#
# Repo reality (why so few): among the shipped MEASURED detectors, provider_switch
# (OSS models) and model_rightsizing (frontier models) are disjoint per-model, and
# duplicate_calls vs prompt_caching are ~independent (their overlap is a tiny slice).
# The real, present overlap is a measured finding SUPERSEDING the directional
# estimate of the same thing — so we don't show a rough estimate for what we've
# already measured precisely. (duplicate_calls ⊕ conditional-invocation, a true
# full overlap, activates when that detector lands — M-opt-15.)
_EXCLUSION_GROUPS = [
    # measured prefix caching supersedes the heuristic estimate of the same thing
    {"prompt_caching", "prompt_caching_est"},
    # measured exact-duplicate finding supersedes the semantic-cache estimate
    {"duplicate_calls", "semantic_caching"},
    # A repeated request and a repeated PREFIX are the same input tokens counted
    # two ways: caching the whole response makes the prefix saving moot, and
    # caching the prefix reduces what the duplicate would have cost. The overlap
    # cannot be quantified from these aggregates — the signals do not say which
    # repeats shared which prefix — so the smaller is dropped from the totals
    # rather than both being added as if they were independent.
    {"duplicate_calls", "prompt_caching"},
]
_SAVINGS_TYPE_RANK = {"measured": 2, "modeled_ceiling": 1, "directional": 0}


def _apply_exclusions(unified: list) -> None:
    """Flag overlapping opportunities so they're dropped from the totals (opt §22).

    Sets `overlaps` on every opportunity (None, or the winning member's title).
    """
    by_lever = {o["lever"]: o for o in unified}
    for o in unified:
        o["overlaps"] = None
    for group in _EXCLUSION_GROUPS:
        present = [by_lever[lever] for lever in group if lever in by_lever]
        if len(present) < 2:
            continue
        winner = max(
            present,
            key=lambda o: (_SAVINGS_TYPE_RANK[o["savings_type"]], o["projected_monthly_savings"]),
        )
        for o in present:
            if o is not winner:
                o["overlaps"] = winner["title"]


# Per-lever guidance (opt spec §20) — deterministic templates, never an LLM. The
# implementation one-liner is the detector's `fix`; these add "how to validate the
# change is safe" and "how Meter confirms it worked".
_DIRECTIONAL_GUIDANCE = {
    "validation": "Investigate whether this usage pattern really applies before acting.",
    "verification": "Install the metering SDK to turn this estimate into a measured, verifiable "
    "finding.",
}
_LEVER_GUIDANCE = {
    "provider_switch": {
        "validation": "Run your eval suite — the weights are identical, so quality parity is "
        "expected.",
        "verification": "Next month's provider row shifts to the cheaper host and the "
        "reconciliation loop reports the realized drop.",
    },
    "prompt_caching": {
        "validation": "Confirm the cached prefix is byte-identical across calls; responses are "
        "unaffected by caching.",
        "verification": "Cache utilization rises and this feature's input cost falls next period.",
    },
    "duplicate_calls": {
        "validation": (
            "Meter measured that these requests were identical. Whether the later ones could "
            "have been served from the first depends on things it cannot see: how fresh the "
            "answer had to be, whether the callers were authorized to the same data, whether "
            "the repeats were deliberate sampling or retries, and whether anything outside "
            "the prompt had changed. Check those before caching."
        ),
        "verification": (
            "The repeat count for this feature drops next period. The figure is a list-price "
            "ceiling, not an invoice-verified saving — confirm the drop against your bill."
        ),
    },
    "model_rightsizing": {
        "validation": "Run a quality eval on a sample before switching — this is a ceiling, not a "
        "guaranteed saving.",
        "verification": "After the switch, spend on the premium model drops and the reconciliation "
        "loop reports realized savings.",
    },
}


# One step down, for a finding the bill contradicts.
_CONFIDENCE_DOWN = {"high": "med", "med": "low", "low": "low"}


def _feature_spend(conn, feature_id: str, start: dt.date) -> float:
    """What this feature actually cost in this period, on the reconciled basis."""
    row = conn.execute(
        f"""
        SELECT COALESCE(SUM(amount), 0) FROM inference_cost
        WHERE feature_id = %s AND period = %s
          AND {dashboard._ACTIVE_ENV} {dashboard._NOT_DOUBLE_COUNTED}
        """,  # noqa: S608
        (feature_id, start, dashboard._connector_providers(conn, start)),
    ).fetchone()
    return float(row[0]) if row else 0.0


def _bound_by_spend(unified: list, spend: float) -> None:
    """No fix saves more than the thing it is fixing costs (design invariant 5).

    Savings are computed from SDK signals and priced from the price book; the
    bill is the connector's. The two are independent, so nothing stopped a
    finding claiming more than the feature spent — and nothing did. A feature
    billed $40 in a month was offered $1,080 of "measured savings", in the
    headline figure, on the screen a CFO reads. Signals are self-reported
    client state: a flush that double-reports, an SDK left running against the
    wrong feature, or a prefix summary whose call count outruns what was
    actually metered all produce exactly this.

    Provider cost APIs are authoritative on dollars, so the bill wins. The
    saving is bounded by it, and a bounded saving is no longer a counted one —
    the signals behind it disagree with the invoice, which makes every number
    derived from them suspect, not just the one that overflowed. It degrades to
    a ceiling, loses a step of confidence, and says so in its own evidence
    rather than being quietly reduced.

    Only applied when there IS a bill. A feature with no recorded spend has
    nothing to be measured against, and clamping to zero would delete real
    findings to punish missing cost data — a different problem, surfaced
    elsewhere.
    """
    if spend <= 0:
        return
    for o in unified:
        if o["savings_type"] == "directional":
            continue  # a rule of thumb is already labelled as not a number
        if o["projected_monthly_savings"] <= spend:
            continue
        o["projected_monthly_savings"] = round(spend, 2)
        o["projected_annual_savings"] = round(spend * 12, 2)
        o["savings_type"] = "modeled_ceiling"
        o["confidence"] = _CONFIDENCE_DOWN.get(o["confidence"], "low")
        o["evidence"] += (
            f" — capped at this feature's ${spend:,.2f} of billed spend, which "
            "the finding exceeded. The reported traffic does not agree with the "
            "bill, so treat the whole finding as an upper bound"
        )
        o["priority_score"] = _priority(
            o["projected_monthly_savings"], o["confidence"], o["engineering_effort"]
        )


def _unify_measured(opp: dict) -> dict:
    """Normalize a measured/ceiling detector output into the unified shape (§18)."""
    meta = _LEVER_META[opp["lever"]]
    savings = opp["savings"]
    # Most levers have one savings_type for all time. Prompt caching does not:
    # whether it is a guaranteed saving or a ceiling depends on whether this
    # tenant's telemetry priced both sides of the trade, so the detector says.
    savings_type = opp.get("savings_type") or meta["savings_type"]
    effort = _LEVER_EFFORT.get(opp["lever"], _DEFAULT_EFFORT)
    guidance = _LEVER_GUIDANCE.get(opp["lever"], _DIRECTIONAL_GUIDANCE)
    return {
        "lever": opp["lever"],
        "title": meta["title"],
        "source": meta["source"],
        "savings_type": savings_type,
        "confidence": opp["confidence"],
        "confidence_reason": meta["confidence_reason"],
        "projected_monthly_savings": savings,
        "projected_annual_savings": round(savings * 12, 2),
        "engineering_effort": effort,
        "priority_score": _priority(savings, opp["confidence"], effort),
        "evidence": opp["evidence"],
        "fix": opp["fix"],
        "validation_guidance": guidance["validation"],
        "verification": guidance["verification"],
        "trail": opp["trail"],
        "status": "detected",
    }


def _unify_directional(opp: dict) -> dict:
    """Normalize a heuristic estimate into the unified shape — always directional."""
    savings = opp["savings"]
    slug = _DIRECTIONAL_LEVER.get(opp["opportunity"], opp["opportunity"].lower().replace(" ", "_"))
    return {
        "lever": slug,
        "title": opp["opportunity"],
        "source": "heuristic",
        "savings_type": "directional",
        "confidence": opp["confidence"],
        "confidence_reason": "Directional rule of thumb from this feature's usage shape.",
        "projected_monthly_savings": savings,
        "projected_annual_savings": round(savings * 12, 2),
        "engineering_effort": _DEFAULT_EFFORT,
        "priority_score": _priority(savings, opp["confidence"], _DEFAULT_EFFORT),
        "evidence": opp["rationale"],
        "fix": None,
        "validation_guidance": _DIRECTIONAL_GUIDANCE["validation"],
        "verification": _DIRECTIONAL_GUIDANCE["verification"],
        "trail": [],
        "status": "detected",
    }


def _fp(fingerprint: str) -> str:
    """A short, opaque handle for a salted hash (never any prompt content)."""
    return fingerprint[:12]


def _duplicate_opportunity(rows: list, legacy_repeats: int = 0) -> Optional[dict]:
    """Rows: (provider, model, fingerprint, call_count, tokens_in, tokens_out, scope_kind).

    What is measured here is that the requests were IDENTICAL — same provider,
    same model, same whole request body, inside one application, feature,
    operation, environment and customer scope, within the SDK's window. What is
    not measured is whether the later ones could have been served from the
    first. That depends on freshness, authorization, deliberate sampling,
    external state and application policy, none of which reaches Meter. So this
    is a ceiling on an opportunity, not a saving anybody has banked.

    The dollars are a LIST-PRICE ceiling for a second reason. `pricing.price`
    prices tokens at the price book, and the aggregate behind these rows carries
    only total input and output. It cannot tell how much of that input was
    already served from a provider cache at a tenth of the rate, so the real
    billed cost of a repeat is at most this and often less. Inventing a cache
    discount here would be inventing a number; the label says ceiling instead.
    """
    if not rows:
        return None
    repeats = sum(int(r[3]) for r in rows)
    # price() is linear in tokens, so summing each row's priced tokens gives the
    # list-price cost of all the repeats — no averaging error, but no invoice
    # behind it either.
    savings = sum((pricing.price(r[1], int(r[4]), int(r[5]), r[0]) for r in rows), Decimal("0"))
    if repeats <= 0 or float(savings) < _MIN_SAVINGS:
        return None
    # Whether anybody named a boundary these responses could be reused across.
    # One unscoped group is enough to stop the whole finding claiming safety.
    unscoped = any((r[6] or "unscoped") == "unscoped" for r in rows)
    trail = [
        {
            "fingerprint": _fp(r[2]),
            "provider": r[0],
            "model": r[1],
            "call_count": int(r[3]),
            "scope": r[6] or "unscoped",
        }
        for r in sorted(rows, key=lambda r: int(r[3]), reverse=True)[:_MAX_TRAIL]
    ]
    evidence = (
        f"{repeats:,} repeated requests across {len(rows):,} distinct request shapes "
        f"this month, priced at list rate"
    )
    if unscoped:
        # Absence of scope is reported as absence. It is not permission.
        evidence += " — no customer or cache scope was set, so reuse safety is unverified"
    if legacy_repeats:
        # Saying so matters: the number visibly dropped, and the reason was a
        # correction to the detector, not a change in the customer's traffic.
        evidence += (
            f". {legacy_repeats:,} further repeats were matched by an older, incomplete "
            "fingerprint and are excluded"
        )
    return {
        "lever": "duplicate_calls",
        "savings": round(float(savings), 2),
        # The COUNT is exact. The saving is a ceiling that assumes every repeat
        # was safely reusable, so the confidence describes the saving, not the
        # counting: medium, and never "guaranteed".
        "confidence": "med" if not unscoped else "low",
        "evidence": evidence,
        "fix": (
            "Cache responses for identical requests (key on the request hash) where the "
            "answer can safely be reused."
        ),
        "trail": trail,
    }


# How you actually turn this on, which is not one instruction. Anthropic caches
# the blocks you mark. OpenAI and Gemini 2.5+ cache automatically, so a prefix
# going uncached there is a prompt-SHAPE problem — something variable sits ahead
# of the static head, or it changes between calls — and telling those customers
# to set cache_control sends them looking for a parameter their API does not
# have.
_CACHING_FIX_EXPLICIT = "Enable prompt caching (set cache_control on the static system block)."
_CACHING_FIX_AUTOMATIC = (
    "This provider caches automatically, so the prefix is not being matched: move the "
    "static content to the very start of the request and keep it byte-identical between "
    "calls."
)


def _prefix_opportunity(rows: list) -> Optional[dict]:
    """Rows: (provider, model, fingerprint, call_count, prefix_tokens, cached_count,
    write_calls, cache_windows, prefix_tokens_sum, prefix_tokens_n).

    Caching is not a discount, it is a trade: reads are cheap and WRITES cost
    more than sending the prefix uncached. So the saving for one prefix is

        prefix_tokens x input_rate x [ uncached x (1 - read_mult)
                                       - writes x (write_mult - 1) ]

    where `writes` is how many times the entry would have to be created — once
    per gap longer than the provider's TTL, which is what the SDK counts as
    `cache_windows`. Leave that term out and sparse traffic reads as a saving
    when enabling caching would raise its bill: at fewer than about one read
    per write, there is nothing here to win.

    Calls the provider already wrote to cache are not an opportunity either.
    They are what keeping a live cache warm costs, so they come off the
    uncached count rather than being offered back as savings.

    Three inputs are counted exactly: the call counts, the cache discount and
    the write premium. The prefix SIZE is exact only when the provider reported
    it (Anthropic returns `cache_creation_input_tokens` — the size of the prefix
    it actually cached); otherwise the SDK's figure is characters over four. And
    a row from an SDK too old to count windows cannot have its write side
    priced at all. Either one moves the finding off a measured saving, and the
    evidence names which.
    """
    total_savings = Decimal("0")
    total_uncached = 0
    total_writes = 0
    max_prefix = 0
    automatic = False  # does any of this run on a provider that caches unasked?
    estimated_calls = 0
    unpriced_writes = 0  # calls whose write side could not be priced
    trail = []
    for (
        provider,
        model,
        fingerprint,
        call_count,
        prefix_tokens,
        cached_count,
        write_calls,
        cache_windows,
        tokens_sum,
        tokens_n,
    ) in rows:
        mult = pricing.cache_read_mult(provider, model)
        if mult is None:  # provider has no priced cache discount -> don't claim one
            continue
        written = int(write_calls or 0)
        # A cache creation is already cached. It is neither a saving nor a call
        # that could be made cheaper by enabling what is evidently enabled.
        cacheable = int(call_count) - int(cached_count) - written
        # The MEAN of what the provider actually cached, where it reported
        # anything. Not the largest: a creation count varies between calls
        # sharing a static block, so the month's high-water mark values the
        # whole group at its most expensive member and the bias only ever
        # points one way. Falling back to the SDK's character estimate is what
        # makes this a `med` finding rather than a measured one.
        samples = int(tokens_n or 0)
        prefix_measured = samples > 0
        p_tokens = (
            int(int(tokens_sum or 0) / samples) if prefix_measured else int(prefix_tokens or 0)
        )
        # Below the provider's own minimum there is no cache to read from, so
        # the finding would not be a cautious estimate — it would be an
        # instruction that cannot be carried out. Unknown models fall back to
        # Meter's floor rather than to a guess.
        floor = max(_MIN_PREFIX_TOKENS, pricing.min_cacheable_tokens(model, provider) or 0)
        if cacheable < _MIN_CACHEABLE_CALLS or p_tokens < floor:
            continue
        input_rate = pricing.rate_in(model, provider)
        unit = Decimal(p_tokens) * input_rate
        # If all of them became reads. The writes are taken back out below —
        # a call that writes the entry is not also reading it.
        read_gain = Decimal(cacheable) * (Decimal("1") - mult)
        if cache_windows is None:
            # No window count: the write side is unknown. Price the reads alone
            # and mark the row, so the finding degrades to a ceiling instead of
            # quietly assuming writes are free.
            writes = 0
            unpriced_writes += cacheable
        else:
            # Not capped at the call count, though windows can exceed it:
            # they are counted per process and over every call sharing the
            # prefix, while `cacheable` excludes the ones already served from
            # cache. A count that high means the entry would be rewritten as
            # often as it is used, which is a loss for every provider in the
            # price book and is dropped below on its own.
            writes = int(cache_windows)
        # What a write costs RELATIVE TO THE READ it replaces, not relative to
        # an uncached call: the write call pays the write rate instead of the
        # cached rate, so the gap is (write_mult - read_mult). Against 1 it
        # would be 1.25 - 1 = 0.25 for Anthropic, understating the cost of a
        # write by more than four times and making sparse traffic — which
        # writes on every call and never reads — look like a saving.
        forgone = pricing.cache_write_mult(provider) - mult
        saving = unit * (read_gain - Decimal(writes) * forgone)
        if saving <= 0:
            # Caching this prefix costs more than it saves. Not a finding.
            continue
        total_savings += saving
        total_uncached += cacheable
        total_writes += writes
        max_prefix = max(max_prefix, p_tokens)
        if not prefix_measured:
            estimated_calls += cacheable
        if pricing.cache_is_automatic(provider):
            automatic = True
        trail.append(
            {
                "fingerprint": _fp(fingerprint),
                "provider": provider,
                "model": model,
                "calls": int(call_count),
                "prefix_tokens": p_tokens,
                "prefix_measured": prefix_measured,
                # How many provider counts that average is over, so a mean
                # taken from one sample is visible as one rather than reading
                # like a settled figure.
                "measured_samples": samples,
                "cached": int(cached_count),
                "already_written": written,
                "cache_writes_needed": None if cache_windows is None else writes,
            }
        )
    if float(total_savings) < _MIN_SAVINGS:
        return None
    sized = estimated_calls == 0
    priced_writes = unpriced_writes == 0
    basis = (
        "measured by the provider's own cache-creation token count"
        if sized
        else "prefix size estimated from request length"
    )
    if priced_writes:
        cost_side = f"net of the {total_writes:,} cache writes it would take to serve them"
    else:
        cost_side = (
            "before the cost of writing the cache, which the reporting SDK is too old "
            "to count — upgrade it to turn this ceiling into a measured saving"
        )
    return {
        "lever": "prompt_caching",
        # A saving is only guaranteed when both sides of the trade were counted.
        # An estimated prefix size or an unpriced write side makes it a ceiling,
        # and a ceiling does not belong in the measured total.
        "savings_type": "measured" if (sized and priced_writes) else "modeled_ceiling",
        "savings": round(float(total_savings), 2),
        "confidence": "high" if (sized and priced_writes) else "med",
        "evidence": (
            f"a {max_prefix:,}-token static prefix repeated across "
            f"{total_uncached:,} uncached calls, {cost_side} — {basis}"
        ),
        "fix": _CACHING_FIX_AUTOMATIC if automatic else _CACHING_FIX_EXPLICIT,
        "trail": trail[:_MAX_TRAIL],
    }


def _cache_utilization(conn, feature_id, start, signal_rows) -> Optional[float]:
    """Share of input already served from the provider's prompt cache (opt spec §8).

    Prefers connector/hook cache token fields (Tier A — works WITHOUT the SDK):
    cached input tokens / total input tokens. Falls back to the SDK prefix signals'
    call-level ratio when no provider cache data is available. None when neither.
    """
    row = conn.execute(
        f"""
        SELECT COALESCE(SUM(cached_tokens_in), 0),
               COALESCE(SUM(tokens_in), 0),
               COUNT(cached_tokens_in)
        FROM inference_cost
        WHERE feature_id = %s AND period = %s
          AND {dashboard._ACTIVE_ENV} {dashboard._NOT_DOUBLE_COUNTED}
        """,  # noqa: S608
        (feature_id, start, dashboard._connector_providers(conn, start)),
    ).fetchone()
    # If any row reported cache tokens, the ratio is over ALL input (a floor —
    # providers that don't report cache count as uncached, never overstated).
    if row and row[2] and row[1]:
        return round(int(row[0]) / int(row[1]), 4)

    # Fallback: SDK prefix signals (share of prefixed calls served from cache).
    total_calls = sum(int(r[4]) for r in signal_rows if r[0] == "prefix")
    total_cached = sum(int(r[8]) for r in signal_rows if r[0] == "prefix")
    return round(total_cached / total_calls, 4) if total_calls else None


def _arbitrage_opportunity(conn, feature_id, start) -> Optional[dict]:
    """Cross-provider price arbitrage (opt spec §16, M-opt-8).

    The same open weights are served by multiple hosts at different rates. For each
    of the feature's hosted-open-model rows, if a cheaper host serves the identical
    model, the saving is the exact rate delta at the feature's own token mix — no
    quality change. Connector data only; no SDK needed.
    """
    rows = conn.execute(
        f"""
        SELECT provider, model,
               SUM(COALESCE(tokens_in, 0)), SUM(COALESCE(tokens_out, 0))
        FROM inference_cost
        WHERE feature_id = %s AND period = %s AND provider IS NOT NULL AND model IS NOT NULL
          AND {dashboard._ACTIVE_ENV} {dashboard._NOT_DOUBLE_COUNTED}
        GROUP BY provider, model
        """,  # noqa: S608
        (feature_id, start, dashboard._connector_providers(conn, start)),
    ).fetchall()

    total_savings = Decimal("0")
    trail = []
    top = None  # the single largest switch, for the headline sentence
    for provider, model, tin, tout in rows:
        alt = pricing.cheapest_equivalent(provider, model, int(tin or 0), int(tout or 0))
        if alt is None or alt["savings"] <= 0:
            continue
        total_savings += alt["savings"]
        pct = round(float(alt["savings"] / alt["current_cost"]) * 100)
        trail.append(
            {
                "model": alt["family_label"],
                "note": (
                    f"{alt['from_provider']} → {alt['to_provider']} · "
                    f"save {_usd(alt['savings'])}/mo ({pct}% less)"
                ),
            }
        )
        if top is None or alt["savings"] > top["savings"]:
            top = {**alt, "pct": pct}

    if top is None or float(total_savings) < _MIN_SAVINGS:
        return None
    return {
        "lever": "provider_switch",
        "savings": round(float(total_savings), 2),
        "confidence": "high",  # exact rate delta on identical weights
        "evidence": (
            f"{top['family_label']} runs on {top['from_provider']}; {top['to_provider']} "
            f"serves the same weights for ~{top['pct']}% less"
        ),
        "fix": (
            f"Route {top['family_label']} to {top['to_provider']} — identical open "
            f"weights, ~{top['pct']}% cheaper at list prices."
        ),
        "trail": trail[:_MAX_TRAIL],
    }


def _usd(value) -> str:
    return f"${float(value):,.2f}"


def _rightsizing_opportunity(conn, feature_id, start) -> Optional[dict]:
    """Model right-sizing ceiling (opt spec §16, M-opt-7).

    Model choice is usually the dominant cost driver. For each model with a cheaper
    same-vendor tier, the ceiling = the feature's REAL spend on that model × the
    rate saving at its token mix (from the price book). Quality-gated: a ceiling
    ("up to $X where quality holds"), med confidence — never summed into the
    guaranteed savings headline.
    """
    rows = conn.execute(
        f"""
        SELECT model, SUM(amount),
               SUM(COALESCE(tokens_in, 0)), SUM(COALESCE(tokens_out, 0))
        FROM inference_cost
        WHERE feature_id = %s AND period = %s AND model IS NOT NULL
          AND {dashboard._ACTIVE_ENV} {dashboard._NOT_DOUBLE_COUNTED}
        GROUP BY model
        """,  # noqa: S608
        (feature_id, start, dashboard._connector_providers(conn, start)),
    ).fetchall()

    total = Decimal("0")
    trail = []
    top = None
    for model, amount, tin, tout in rows:
        dc = pricing.downgrade_ceiling(model, int(tin or 0), int(tout or 0))
        if dc is None:
            continue
        saving = Decimal(str(amount)) * Decimal(str(dc["save_fraction"]))
        if saving <= 0:
            continue
        total += saving
        pct = round(dc["save_fraction"] * 100)
        trail.append(
            {
                "model": f"{model} → {dc['target']}",
                "note": f"up to {_usd(saving)}/mo ({pct}% cheaper)",
            }
        )
        if top is None or saving > top["saving"]:
            top = {"model": model, "target": dc["target"], "pct": pct, "saving": saving}

    if top is None or float(total) < _MIN_SAVINGS:
        return None
    return {
        "lever": "model_rightsizing",
        "savings": round(float(total), 2),
        "confidence": "med",  # a quality-gated ceiling, not a guaranteed saving
        "evidence": (
            f"{top['model']} handles this feature; {top['target']} is ~{top['pct']}% "
            f"cheaper at the same token mix"
        ),
        "fix": (
            f"Move {top['model']} → {top['target']} where quality allows — up to {_usd(total)}/mo."
        ),
        "trail": trail[:_MAX_TRAIL],
    }


def _measured(conn, feature_id: str, start: dt.date) -> tuple[list, Optional[float], dict]:
    rows = conn.execute(
        """
        -- Read positionally below, so the order is load-bearing. prefix_measured
        -- is deliberately absent: since 0063 a prefix is measured when it has
        -- provider counts to average, and that column survives only to tell
        -- pre-0063 rows apart, which having no counts already does.
        SELECT signal_kind, provider, model, fingerprint,
               call_count, prefix_tokens, tokens_in, tokens_out, cached_count,
               fingerprint_version, scope_kind,
               write_calls, cache_windows, prefix_tokens_sum, prefix_tokens_n
        FROM usage_signal
        WHERE feature_id = %s AND period = %s
        """,
        (feature_id, start),
    ).fetchall()

    # v1 fingerprints compared provider, model and messages and nothing else, so
    # calls differing in temperature, system, tools or response format hashed
    # identically. Those rows are kept — they are real history and they back
    # existing applied actions — but an incomplete match must not be presented
    # as an exact one, so the finding is built from v2 alone.
    dup_rows = [
        (r[1], r[2], r[3], r[4], r[6], r[7], r[10])
        for r in rows
        if r[0] == "duplicate" and r[9] == "v2"
    ]
    legacy_dups = sum(int(r[4]) for r in rows if r[0] == "duplicate" and r[9] != "v2")
    pfx_rows = [
        (r[1], r[2], r[3], r[4], r[5], r[8], r[11], r[12], r[13], r[14])
        for r in rows
        if r[0] == "prefix"
    ]

    opportunities = [
        opp
        for opp in (
            _duplicate_opportunity(dup_rows, legacy_dups),
            _prefix_opportunity(pfx_rows),
            _arbitrage_opportunity(conn, feature_id, start),
            _rightsizing_opportunity(conn, feature_id, start),
        )
        if opp is not None
    ]
    cache_utilization = _cache_utilization(conn, feature_id, start, rows)
    # Which levers could still SEE anything this period. "No repeats found" and
    # "nothing was looking" are different facts, and only the first is evidence
    # that a fix worked — so a lever is observable when its KIND of telemetry
    # arrived at all, not when it happened to find something.
    observable = {
        # A v2 DUPLICATE row. "Any v2 row" was the same thing only by accident:
        # prefix summaries carry no version and default to v1, so the moment one
        # of them is ever stamped, a feature with prefix telemetry and no
        # duplicate telemetry would say the duplicate lever was looking — and an
        # applied dedup action would reconcile against a silence it had
        # mistaken for a measurement.
        "duplicate_calls": any(r[0] == "duplicate" and r[9] == "v2" for r in rows),
        "prompt_caching": any(r[0] == "prefix" for r in rows),
    }
    return opportunities, cache_utilization, observable


def _months_between(a: dt.date, b: dt.date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


# Periods a realized saving must hold before it's counted VERIFIED (opt spec §20).
_VERIFY_PERIODS = 2


def _unit_cost(conn, feature_id: str, period: dt.date) -> Optional[tuple[float, str]]:
    """What a unit of this feature's work cost in one period, and what the unit is.

    Cost per call where the provider reports a request count, else cost per
    1,000 input tokens. None when neither is available, which is a real answer:
    without it there is nothing to compare a claimed saving against.

    Read on the reconciled basis, so this is the BILL — the connector's number
    where there is one — not a figure derived from the same signals that
    produced the projection.
    """
    row = conn.execute(
        f"""
        SELECT COALESCE(SUM(amount), 0),
               COALESCE(SUM(request_count), 0),
               COALESCE(SUM(tokens_in), 0)
        FROM inference_cost
        WHERE feature_id = %s AND period = %s
          AND {dashboard._ACTIVE_ENV} {dashboard._NOT_DOUBLE_COUNTED}
        """,  # noqa: S608
        (feature_id, period, dashboard._connector_providers(conn, period)),
    ).fetchone()
    if not row:
        return None
    amount, calls, tokens_in = float(row[0]), int(row[1]), int(row[2])
    if amount <= 0:
        return None
    if calls > 0:
        return round(amount / calls, 6), "call"
    if tokens_in > 0:
        # Per MILLION, the unit every provider quotes and the price book uses.
        return round(amount / tokens_in * 1_000_000, 6), "1M input tokens"
    return None


def _corroborated(before, after) -> tuple[Optional[bool], Optional[str]]:
    """Did the bill move the way a realized saving says it did?

    Returns (verdict, why-not). None means the question could not be asked at
    all, which is not the same as a no.
    """
    if before is None or after is None:
        return None, "no billed cost to compare in one of the two periods"
    if before[1] != after[1]:
        # Cost per call and cost per 1,000 tokens are not the same measurement.
        return None, "the two periods are measured in different units"
    if after[0] < before[0]:
        return True, None
    return False, "the feature's cost per unit of work did not fall"


def _actions(conn, feature_id, start, measured_by_lever: dict, observable: dict) -> list:
    """Applied optimizations, reconciled projected → realized → verified (opt spec §20).

    realized = frozen projection − the lever's CURRENT avoidable spend, once we're
    past the applied period. Status advances: pending (applied this period) →
    measured (one period reconciled) → verified (the realized drop has held for
    `_VERIFY_PERIODS` periods), the terminal Prove state.

    `observable` says whether each lever could still SEE anything this period.
    Without it, "current avoidable spend is zero" is ambiguous in the worst
    possible direction: a customer who turned optimize mode off, or whose old
    v1 signals are no longer trusted, would show realized = the full projection
    and, after two periods, "verified". Meter would be reporting a success it
    had simply stopped being able to look for. A lever with no input this period
    reconciles to nothing and says so.

    VERIFIED additionally requires the bill to agree. `realized` is a projection
    minus a projection: both sides come from the same signals, and neither has
    ever touched an invoice. Traffic halving for reasons that have nothing to do
    with the fix halves the lever's current avoidable spend, and that read as a
    realized saving which turned "verified" — the terminal Prove state — two
    periods later. So verification asks the connector's own numbers a separate
    question: did a unit of this feature's work get cheaper? Fewer calls at the
    same price each is not a saving, and invariant 5 says the provider's cost
    API is what decides. Where that question cannot be asked, the action stays
    `measured` and says which part was missing rather than being promoted on
    the strength of the half that could be checked.
    """
    rows = conn.execute(
        """
        SELECT lever, applied_on, projected_monthly
        FROM optimization_action
        WHERE feature_id = %s
        ORDER BY applied_on
        """,
        (feature_id,),
    ).fetchall()
    out = []
    # The bill, once per period rather than once per action.
    now_cost = _unit_cost(conn, feature_id, start)
    before_cache: dict[dt.date, Optional[tuple[float, str]]] = {}
    for lever, applied_on, projected in rows:
        projected = round(float(projected), 2)
        current = round(float(measured_by_lever.get(lever, 0.0)), 2)
        elapsed = _months_between(applied_on, start)
        seen = observable.get(lever, True)
        if applied_on not in before_cache:
            before_cache[applied_on] = _unit_cost(conn, feature_id, applied_on)
        before = before_cache[applied_on]
        agrees, why_not = _corroborated(before, now_cost)
        note = None
        if elapsed <= 0:
            realized, status = None, "pending"  # applied this period, nothing to reconcile
        elif not seen:
            # Nothing to compare against. Absence of evidence is reported as
            # absence, never as a realized saving.
            realized, status = None, "unverifiable"
        else:
            realized = round(projected - current, 2)
            status = "measured"
            if elapsed >= _VERIFY_PERIODS and realized > 0:
                if agrees:
                    status = "verified"
                else:
                    # Everything the signals can show is satisfied. Say what
                    # the bill would not confirm, rather than leaving the
                    # reader to wonder why this one never advanced.
                    note = f"Not verified against the bill: {why_not}."
        out.append(
            {
                "lever": lever,
                "applied_on": applied_on.isoformat(),
                "projected_monthly": projected,
                "current_avoidable": current if seen else None,
                "realized_monthly": realized,
                "status": status,
                # The billed side, shown rather than merely consulted: a Prove
                # state nobody can audit is the thing this product exists not
                # to ship.
                "unit_cost_before": before[0] if before else None,
                "unit_cost_now": now_cost[0] if now_cost else None,
                # Only where the two are the same measurement. Naming one unit
                # for a pair that does not share it invites the reader to
                # compare two numbers that cannot be compared.
                "unit_cost_unit": before[1] if agrees is not None else None,
                "bill_agrees": agrees,
                "verification_note": note,
            }
        )
    return out


def opportunities(
    tenant_id: str,
    feature_id: str,
    start: Optional[dt.date] = None,
    end: Optional[dt.date] = None,
    range_token: Optional[str] = None,
) -> Optional[dict]:
    """Unified optimization opportunities for one feature (opt spec §18).

    One list, one shape. Each opportunity carries a `savings_type`
    (measured | modeled_ceiling | directional); the three totals are computed
    separately and never combined. Returns None if the feature doesn't exist.

    Opportunities are per-month estimates, so a selected review range is honoured
    by anchoring the analysis on the range's latest month.
    """
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        _start, anchor = dashboard._resolve_range(conn, range_token, start, end)
        if conn.execute("SELECT 1 FROM feature WHERE id = %s", (feature_id,)).fetchone() is None:
            return None
        result = _feature_opportunities(conn, feature_id, anchor)
    return {"period": anchor.isoformat(), **result}


def _feature_opportunities(conn, feature_id: str, start: dt.date) -> dict:
    """Compute the unified opportunities for one feature within an open connection.

    Shared by the per-feature endpoint and the tenant Overview (opt spec §21), so
    the Overview aggregates over the SAME numbers a feature page shows.
    """
    measured, cache_utilization, observable = _measured(conn, feature_id, start)
    estimated = dashboard.heuristic_optimization(conn, feature_id, start)

    unified = [_unify_measured(o) for o in measured]
    unified += [_unify_directional(o) for o in estimated["opportunities"]]

    # Before anything is ranked or totalled: nothing saves more than the bill.
    _bound_by_spend(unified, _feature_spend(conn, feature_id, start))

    # Reconciliation reads the CURRENT avoidable spend per applied lever.
    by_lever = {
        o["lever"]: o["projected_monthly_savings"]
        for o in unified
        if o["savings_type"] in ("measured", "modeled_ceiling")
    }
    actions = _actions(conn, feature_id, start, by_lever, observable)
    # An opportunity's lifecycle status follows its applied action: detected →
    # applied → verified (opt spec §20).
    action_status = {a["lever"]: a["status"] for a in actions}
    for o in unified:
        st = action_status.get(o["lever"])
        if st == "verified":
            o["status"] = "verified"
        elif st is not None:
            o["status"] = "applied"

    # Suppress double-counting: a measured finding supersedes overlapping estimates.
    _apply_exclusions(unified)

    # Rank by priority (savings × confidence × effort) — "what to fix first".
    unified.sort(key=lambda o: o["priority_score"], reverse=True)
    totals = {
        kind: round(
            sum(
                o["projected_monthly_savings"]
                for o in unified
                if o["savings_type"] == kind and o["overlaps"] is None
            ),
            2,
        )
        for kind in ("measured", "modeled_ceiling", "directional")
    }
    return {
        "opportunities": unified,
        "totals": totals,  # measured / modeled_ceiling / directional — never combined
        "cache_utilization": cache_utilization,
        "actions": actions,  # applied optimizations: projected vs realized (opt spec §11)
    }


def copilot_overview(tenant_id: str, period: Optional[dt.date] = None) -> dict:
    """Tenant-wide optimization Overview (opt spec §21) — "where's the money and
    what do I fix first" across every feature. Aggregates the SAME per-feature
    opportunity computations; no new tables. Measured, modeled and verified savings
    are kept strictly separate and never combined into one number.
    """
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        start = dashboard._resolve_period(conn, period)
        features = conn.execute("SELECT id, name FROM feature ORDER BY name").fetchall()

        totals = {"measured": 0.0, "modeled_ceiling": 0.0, "directional": 0.0}
        verified_monthly = 0.0
        actionable = []  # measured + modeled_ceiling opps, tagged with their feature
        by_feature = []
        lever_map: dict[str, dict] = {}
        applied: list[dict] = []

        for fid, fname in features:
            r = _feature_opportunities(conn, str(fid), start)
            for kind in totals:
                totals[kind] += r["totals"][kind]
            by_feature.append({"feature_id": str(fid), "name": fname, **r["totals"]})
            for o in r["opportunities"]:
                if o["savings_type"] == "directional" or o["overlaps"] is not None:
                    continue
                actionable.append({**o, "feature_id": str(fid), "feature_name": fname})
                entry = lever_map.setdefault(
                    o["lever"],
                    {
                        "lever": o["lever"],
                        "title": o["title"],
                        "savings_type": o["savings_type"],
                        "monthly": 0.0,
                        "count": 0,
                    },
                )
                entry["monthly"] += o["projected_monthly_savings"]
                entry["count"] += 1
            for a in r["actions"]:
                applied.append({**a, "feature_id": str(fid), "feature_name": fname})
                if a["status"] == "verified" and a["realized_monthly"]:
                    verified_monthly += a["realized_monthly"]

        # Billing-only recommendations (no SDK required). Kept in their OWN key and
        # deliberately excluded from `totals` — they are spend to review, growth and
        # governance signals, never measured or modelled savings.
        # Two different questions, and the screen was only ever asking one.
        #
        # "Does request telemetry arrive at all" decides whether to offer the
        # SDK — and asking it of `usage_signal` alone is the narrowest possible
        # way to have it: a customer with fully traced agent runs was being told
        # to go and install what they had already installed.
        #
        # "Are optimize-mode signals arriving" decides whether the duplicate and
        # prefix levers can fire. Someone with telemetry but no optimize mode
        # needs a flag, not an install.
        optimize_present = bool(
            conn.execute(
                "SELECT 1 FROM usage_signal WHERE period = %s LIMIT 1", (start,)
            ).fetchone()
        )
        sdk_present = optimize_present or bool(
            conn.execute(
                """
                SELECT 1 WHERE EXISTS (SELECT 1 FROM inference_cost
                                       WHERE period = %s AND source = 'hook')
                            OR EXISTS (SELECT 1 FROM ai_trace)
                """,
                (start,),
            ).fetchone()
        )
        billing_present = bool(
            conn.execute(
                """
                SELECT 1 WHERE EXISTS (SELECT 1 FROM inference_cost WHERE period = %s)
                            OR EXISTS (SELECT 1 FROM build_cost WHERE period = %s)
                """,
                (start, start),
            ).fetchone()
        )

        top = sorted(actionable, key=lambda o: o["priority_score"], reverse=True)[:8]
        by_feature.sort(key=lambda f: f["measured"] + f["modeled_ceiling"], reverse=True)
        by_lever = sorted(lever_map.values(), key=lambda e: e["monthly"], reverse=True)
        for e in by_lever:
            e["monthly"] = round(e["monthly"], 2)

    return {
        "period": start.isoformat(),
        "totals": {k: round(v, 2) for k, v in totals.items()},
        "verified_monthly_savings": round(verified_monthly, 2),
        "verified_annual_savings": round(verified_monthly * 12, 2),
        "top_recommendations": top,
        "by_feature": by_feature,
        "by_lever": by_lever,
        "applied": applied,
        # --- Billing-only path (no SDK required) -------------------------------
        # Separate from every total above: these are spend-to-review, visibility,
        # concentration, growth and control gaps — never measured/modelled savings.
        "has_sdk_telemetry": sdk_present,
        # Telemetry is arriving, but not the salted signals the duplicate-call
        # and prompt-caching levers are computed from.
        "has_optimize_signals": optimize_present,
        "has_billing_data": billing_present,
        "billing_opportunities": optimize_billing.billing_opportunities(tenant_id, start, start),
    }


def mark_applied(
    tenant_id: str,
    feature_id: str,
    lever: str,
    projected_monthly: float,
    period: Optional[dt.date] = None,
) -> Optional[dict]:
    """Freeze a measured opportunity's projection as of a period (opt spec §11).

    Returns None if the feature doesn't exist. Idempotent per (feature, lever):
    re-applying updates the applied period and frozen projection.
    """
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        if conn.execute("SELECT 1 FROM feature WHERE id = %s", (feature_id,)).fetchone() is None:
            return None
        applied_on = dashboard._resolve_period(conn, period)
        conn.execute(
            """
            INSERT INTO optimization_action (tenant_id, feature_id, lever, applied_on,
                                             projected_monthly)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, feature_id, lever) DO UPDATE
            SET applied_on = EXCLUDED.applied_on,
                projected_monthly = EXCLUDED.projected_monthly
            """,
            (tenant_id, feature_id, lever, applied_on, projected_monthly),
        )
    return {"lever": lever, "applied_on": applied_on.isoformat()}


def unmark_applied(tenant_id: str, feature_id: str, lever: str) -> None:
    """Remove an applied optimization action (undo)."""
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        conn.execute(
            "DELETE FROM optimization_action WHERE feature_id = %s AND lever = %s",
            (feature_id, lever),
        )
