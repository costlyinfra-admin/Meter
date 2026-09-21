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
    uncached calls; savings is the repeated prefix tokens priced at
    (input rate − cached-read rate) from the price book. `measured` where the
    provider reported the prefix size, `modeled_ceiling` where it is an estimate.

The two overlap — the same input tokens counted two ways — so they are mutually
exclusive in the totals rather than summed.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Optional

from . import dashboard, optimize_billing, pricing
from .db import app_dsn, connect, tenant_tx

# Detection thresholds (opt spec §7). Kept deliberately conservative so a surfaced
# opportunity is always worth acting on.
_MIN_PREFIX_TOKENS = 1000  # a prefix worth caching is large
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
        "savings_type": "measured",
        "confidence_reason": "Measured uncached prefix tokens priced at the cache-read discount.",
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


def _unify_measured(opp: dict) -> dict:
    """Normalize a measured/ceiling detector output into the unified shape (§18)."""
    meta = _LEVER_META[opp["lever"]]
    savings = opp["savings"]
    effort = _LEVER_EFFORT.get(opp["lever"], _DEFAULT_EFFORT)
    guidance = _LEVER_GUIDANCE.get(opp["lever"], _DIRECTIONAL_GUIDANCE)
    return {
        "lever": opp["lever"],
        "title": meta["title"],
        "source": meta["source"],
        "savings_type": meta["savings_type"],
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


def _prefix_opportunity(rows: list) -> Optional[dict]:
    """Rows: (provider, model, fingerprint, call_count, prefix_tokens, cached_count,
    prefix_measured).

    The dollars here are `uncached calls x prefix tokens x input rate x the
    cache discount`. Three of those four are counted exactly; the prefix size is
    only exact when the provider reported it (Anthropic returns
    `cache_creation_input_tokens` — the size of the prefix it actually cached).
    Where it did not, the SDK's figure is characters over four, and this is an
    estimate wearing a measurement's clothes unless it says so. So the
    confidence follows the input: `high` only when every priced row carried a
    real count, `med` otherwise, and the evidence names which.
    """
    total_savings = Decimal("0")
    total_uncached = 0
    max_prefix = 0
    estimated_calls = 0
    trail = []
    for (
        provider,
        model,
        fingerprint,
        call_count,
        prefix_tokens,
        cached_count,
        prefix_measured,
    ) in rows:
        mult = pricing.cache_read_mult(provider)
        if mult is None:  # provider has no priced cache discount -> don't claim one
            continue
        cacheable = int(call_count) - int(cached_count)
        p_tokens = int(prefix_tokens or 0)
        if cacheable < _MIN_CACHEABLE_CALLS or p_tokens < _MIN_PREFIX_TOKENS:
            continue
        input_rate = pricing.rate_in(model, provider)
        saving = Decimal(cacheable) * Decimal(p_tokens) * input_rate * (Decimal("1") - mult)
        total_savings += saving
        total_uncached += cacheable
        max_prefix = max(max_prefix, p_tokens)
        if not prefix_measured:
            estimated_calls += cacheable
        trail.append(
            {
                "fingerprint": _fp(fingerprint),
                "provider": provider,
                "model": model,
                "calls": int(call_count),
                "prefix_tokens": p_tokens,
                "prefix_measured": bool(prefix_measured),
                "cached": int(cached_count),
            }
        )
    if float(total_savings) < _MIN_SAVINGS:
        return None
    measured = estimated_calls == 0
    basis = (
        "measured by the provider's own cache-creation token count"
        if measured
        else "prefix size estimated from request length"
    )
    return {
        "lever": "prompt_caching",
        "savings": round(float(total_savings), 2),
        # The call counts and the cache discount are exact either way; what
        # moves this off "high" is pricing an estimated number of tokens.
        "confidence": "high" if measured else "med",
        "evidence": (
            f"a {max_prefix:,}-token static prefix repeated across "
            f"{total_uncached:,} uncached calls — {basis}"
        ),
        "fix": "Enable prompt caching (set cache_control on the static system block).",
        "trail": trail[:_MAX_TRAIL],
    }


def _cache_utilization(conn, feature_id, start, signal_rows) -> Optional[float]:
    """Share of input already served from the provider's prompt cache (opt spec §8).

    Prefers connector/hook cache token fields (Tier A — works WITHOUT the SDK):
    cached input tokens / total input tokens. Falls back to the SDK prefix signals'
    call-level ratio when no provider cache data is available. None when neither.
    """
    row = conn.execute(
        """
        SELECT COALESCE(SUM(cached_tokens_in), 0),
               COALESCE(SUM(tokens_in), 0),
               COUNT(cached_tokens_in)
        FROM inference_cost
        WHERE feature_id = %s AND period = %s
        """,
        (feature_id, start),
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
        """
        SELECT provider, model,
               SUM(COALESCE(tokens_in, 0)), SUM(COALESCE(tokens_out, 0))
        FROM inference_cost
        WHERE feature_id = %s AND period = %s AND provider IS NOT NULL AND model IS NOT NULL
        GROUP BY provider, model
        """,
        (feature_id, start),
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
        """
        SELECT model, SUM(amount),
               SUM(COALESCE(tokens_in, 0)), SUM(COALESCE(tokens_out, 0))
        FROM inference_cost
        WHERE feature_id = %s AND period = %s AND model IS NOT NULL
        GROUP BY model
        """,
        (feature_id, start),
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
        SELECT signal_kind, provider, model, fingerprint,
               call_count, prefix_tokens, tokens_in, tokens_out, cached_count,
               prefix_measured, fingerprint_version, scope_kind
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
        (r[1], r[2], r[3], r[4], r[6], r[7], r[11])
        for r in rows
        if r[0] == "duplicate" and r[10] == "v2"
    ]
    legacy_dups = sum(int(r[4]) for r in rows if r[0] == "duplicate" and r[10] != "v2")
    pfx_rows = [(r[1], r[2], r[3], r[4], r[5], r[8], r[9]) for r in rows if r[0] == "prefix"]

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
        # v2 signals of any kind prove optimize mode is live and reporting on a
        # comparison this detector trusts. v1 rows do not: they are excluded
        # from the finding, so they cannot witness its absence either.
        "duplicate_calls": any(r[10] == "v2" for r in rows),
        "prompt_caching": any(r[0] == "prefix" for r in rows),
    }
    return opportunities, cache_utilization, observable


def _months_between(a: dt.date, b: dt.date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


# Periods a realized saving must hold before it's counted VERIFIED (opt spec §20).
_VERIFY_PERIODS = 2


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
    for lever, applied_on, projected in rows:
        projected = round(float(projected), 2)
        current = round(float(measured_by_lever.get(lever, 0.0)), 2)
        elapsed = _months_between(applied_on, start)
        seen = observable.get(lever, True)
        if elapsed <= 0:
            realized, status = None, "pending"  # applied this period, nothing to reconcile
        elif not seen:
            # Nothing to compare against. Absence of evidence is reported as
            # absence, never as a realized saving.
            realized, status = None, "unverifiable"
        else:
            realized = round(projected - current, 2)
            status = "verified" if elapsed >= _VERIFY_PERIODS and realized > 0 else "measured"
        out.append(
            {
                "lever": lever,
                "applied_on": applied_on.isoformat(),
                "projected_monthly": projected,
                "current_avoidable": current if seen else None,
                "realized_monthly": realized,
                "status": status,
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
