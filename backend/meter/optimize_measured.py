"""Measured optimization opportunities (opt spec §7).

Unlike the heuristic estimator ([optimize.py](optimize.py)), every number here is
*measured*: it is computed from the SDK's `usage_signal` rows (counts of real
duplicate calls and repeated uncached prefixes) and priced from the price book
([pricing.py](pricing.py)) — never a flat percentage. The heuristic tier remains
below this as the zero-instrumentation fallback.

Two detectors in this slice:
  * **Duplicate calls** — the (N-1) repeats of a request are avoidable; savings
    is their real priced cost.
  * **Cacheable prompt prefix** — a large static prefix repeated across many
    uncached calls; savings is the repeated prefix tokens priced at
    (input rate − cached-read rate) from the price book.
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
        "title": "Duplicate calls",
        "source": "sdk",
        "savings_type": "measured",
        "confidence_reason": "Exact count of repeated requests, priced from the price book.",
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
        "validation": "Confirm the duplicates aren't intentional (idempotent retries, distinct "
        "users) before caching.",
        "verification": "The duplicate count for this feature drops next period; the "
        "reconciliation loop reports the realized saving.",
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


def _duplicate_opportunity(rows: list) -> Optional[dict]:
    """Rows: (provider, model, fingerprint, call_count, tokens_in, tokens_out)."""
    if not rows:
        return None
    repeats = sum(int(r[3]) for r in rows)
    # price() is linear in tokens, so summing each row's priced tokens gives the
    # exact cost of all the avoidable repeats — no averaging error.
    savings = sum((pricing.price(r[1], int(r[4]), int(r[5]), r[0]) for r in rows), Decimal("0"))
    if repeats <= 0 or float(savings) < _MIN_SAVINGS:
        return None
    trail = [
        {
            "fingerprint": _fp(r[2]),
            "provider": r[0],
            "model": r[1],
            "call_count": int(r[3]),
        }
        for r in sorted(rows, key=lambda r: int(r[3]), reverse=True)[:_MAX_TRAIL]
    ]
    return {
        "lever": "duplicate_calls",
        "savings": round(float(savings), 2),
        "confidence": "high",  # measured exactly; framed as a ceiling
        "evidence": (
            f"{repeats:,} duplicate calls across {len(rows):,} distinct requests this month"
        ),
        "fix": "Add response caching for identical requests (e.g. keyed on the request hash).",
        "trail": trail,
    }


def _prefix_opportunity(rows: list) -> Optional[dict]:
    """Rows: (provider, model, fingerprint, call_count, prefix_tokens, cached_count)."""
    total_savings = Decimal("0")
    total_uncached = 0
    max_prefix = 0
    trail = []
    for provider, model, fingerprint, call_count, prefix_tokens, cached_count in rows:
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
        trail.append(
            {
                "fingerprint": _fp(fingerprint),
                "provider": provider,
                "model": model,
                "calls": int(call_count),
                "prefix_tokens": p_tokens,
                "cached": int(cached_count),
            }
        )
    if float(total_savings) < _MIN_SAVINGS:
        return None
    return {
        "lever": "prompt_caching",
        "savings": round(float(total_savings), 2),
        "confidence": "high",
        "evidence": (
            f"a {max_prefix:,}-token static prefix repeated across "
            f"{total_uncached:,} uncached calls"
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


def _measured(conn, feature_id: str, start: dt.date) -> tuple[list, Optional[float]]:
    rows = conn.execute(
        """
        SELECT signal_kind, provider, model, fingerprint,
               call_count, prefix_tokens, tokens_in, tokens_out, cached_count
        FROM usage_signal
        WHERE feature_id = %s AND period = %s
        """,
        (feature_id, start),
    ).fetchall()

    dup_rows = [(r[1], r[2], r[3], r[4], r[6], r[7]) for r in rows if r[0] == "duplicate"]
    pfx_rows = [(r[1], r[2], r[3], r[4], r[5], r[8]) for r in rows if r[0] == "prefix"]

    opportunities = [
        opp
        for opp in (
            _duplicate_opportunity(dup_rows),
            _prefix_opportunity(pfx_rows),
            _arbitrage_opportunity(conn, feature_id, start),
            _rightsizing_opportunity(conn, feature_id, start),
        )
        if opp is not None
    ]
    cache_utilization = _cache_utilization(conn, feature_id, start, rows)
    return opportunities, cache_utilization


def _months_between(a: dt.date, b: dt.date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


# Periods a realized saving must hold before it's counted VERIFIED (opt spec §20).
_VERIFY_PERIODS = 2


def _actions(conn, feature_id, start, measured_by_lever: dict) -> list:
    """Applied optimizations, reconciled projected → realized → verified (opt spec §20).

    realized = frozen projection − the lever's CURRENT avoidable spend, once we're
    past the applied period. Status advances: pending (applied this period) →
    measured (one period reconciled) → verified (the realized drop has held for
    `_VERIFY_PERIODS` periods), the terminal Prove state.
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
        if elapsed <= 0:
            realized, status = None, "pending"  # applied this period, nothing to reconcile
        else:
            realized = round(projected - current, 2)
            status = "verified" if elapsed >= _VERIFY_PERIODS and realized > 0 else "measured"
        out.append(
            {
                "lever": lever,
                "applied_on": applied_on.isoformat(),
                "projected_monthly": projected,
                "current_avoidable": current,
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
    measured, cache_utilization = _measured(conn, feature_id, start)
    estimated = dashboard.heuristic_optimization(conn, feature_id, start)

    unified = [_unify_measured(o) for o in measured]
    unified += [_unify_directional(o) for o in estimated["opportunities"]]

    # Reconciliation reads the CURRENT avoidable spend per applied lever.
    by_lever = {
        o["lever"]: o["projected_monthly_savings"]
        for o in unified
        if o["savings_type"] in ("measured", "modeled_ceiling")
    }
    actions = _actions(conn, feature_id, start, by_lever)
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
        sdk_present = bool(
            conn.execute(
                "SELECT 1 FROM usage_signal WHERE period = %s LIMIT 1", (start,)
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
