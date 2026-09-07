"""Measured optimization opportunities: savings computed from seeded signals
must match a hand-calc from the price book (opt spec M-opt-3)."""

from __future__ import annotations

import datetime as dt

from meter import features, hook, optimize_measured
from meter.db import app_dsn, connect, tenant_tx

PERIOD = dt.date(2026, 6, 1)


def _dup_event(feature_id, fingerprint, tokens_in):
    return {
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "tokens_in": tokens_in,
        "tokens_out": 0,
        "feature_id": feature_id,
        "occurred_at": "2026-06-15T10:00:00Z",
        "signal": {"kind": "duplicate", "fingerprint": fingerprint, "count": 1},
    }


def _prefix_event(feature_id, fingerprint, count, prefix_tokens, cached_count, tokens_in):
    return {
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "feature_id": feature_id,
        "occurred_at": "2026-06-16T10:00:00Z",
        "signal": {
            "kind": "prefix",
            "fingerprint": fingerprint,
            "count": count,
            "prefix_tokens": prefix_tokens,
            "cached_count": cached_count,
            "tokens_in": tokens_in,
            "tokens_out": 0,
        },
    }


def _opp(result, lever):
    return next(o for o in result["opportunities"] if o["lever"] == lever)


def _measured_levers(result):
    # Levers that carry real (measured or modeled-ceiling) dollars, not directional.
    return {
        o["lever"]
        for o in result["opportunities"]
        if o["savings_type"] in ("measured", "modeled_ceiling")
    }


def test_duplicate_savings_match_the_price_book(tenant_id):
    triage = features.add_feature(tenant_id, "AI threat triage")
    # Two repeats of the same request, 1M input tokens each -> 2M avoidable input.
    hook.ingest_events(
        tenant_id,
        [
            _dup_event(triage["id"], "fp-a", 1_000_000),
            _dup_event(triage["id"], "fp-a", 1_000_000),
        ],
    )

    result = optimize_measured.opportunities(tenant_id, triage["id"], PERIOD)
    dup = _opp(result, "duplicate_calls")

    # 2M input tokens @ $3/M (claude-sonnet-4-6) = $6.00, exactly.
    assert dup["projected_monthly_savings"] == 6.0
    assert dup["savings_type"] == "measured"
    assert dup["source"] == "sdk"
    assert dup["confidence"] == "high"
    # Per-lever effort + deterministic priority (6.0 × high 1.0 × medium 0.5 = 3.0).
    assert dup["engineering_effort"] == "medium"
    assert dup["priority_score"] == 3.0
    # Guidance templates (opt spec §20): how to validate + how Meter verifies.
    assert "idempotent retries" in dup["validation_guidance"]
    assert "duplicate count" in dup["verification"]
    assert dup["status"] == "detected"
    assert "2 duplicate calls across 1 distinct requests" in dup["evidence"]
    assert dup["trail"][0]["call_count"] == 2
    # Fingerprints in the trail are short salted-hash handles, never prompt text.
    assert dup["trail"][0]["fingerprint"] == "fp-a"[:12]


def test_prefix_caching_savings_match_the_price_book(tenant_id):
    triage = features.add_feature(tenant_id, "AI threat triage")
    # A 4,000-token static prefix across 1,000 uncached calls.
    hook.ingest_events(
        tenant_id,
        [_prefix_event(triage["id"], "fp-p", 1000, 4000, 0, 4_000_000)],
    )

    result = optimize_measured.opportunities(tenant_id, triage["id"], PERIOD)
    prefix = _opp(result, "prompt_caching")

    # 1,000 calls * 4,000 tokens * $3/M * (1 - 0.10 cache-read) = $10.80.
    assert prefix["projected_monthly_savings"] == 10.8
    assert prefix["confidence"] == "high"
    assert "4,000-token static prefix" in prefix["evidence"]
    assert "1,000 uncached calls" in prefix["evidence"]
    # No calls were cached yet.
    assert result["cache_utilization"] == 0.0


def test_prefix_below_threshold_is_not_flagged(tenant_id):
    triage = features.add_feature(tenant_id, "AI threat triage")
    # Small prefix (900 tokens) and few calls (50): below both thresholds.
    hook.ingest_events(
        tenant_id,
        [_prefix_event(triage["id"], "fp-small", 50, 900, 0, 45_000)],
    )
    result = optimize_measured.opportunities(tenant_id, triage["id"], PERIOD)
    assert "prompt_caching" not in _measured_levers(result)


def test_combines_measured_and_estimated_tiers(tenant_id):
    triage = features.add_feature(tenant_id, "AI threat triage")
    hook.ingest_events(
        tenant_id,
        [
            _dup_event(triage["id"], "fp-a", 1_000_000),
            _dup_event(triage["id"], "fp-a", 1_000_000),
            _prefix_event(triage["id"], "fp-p", 1000, 4000, 0, 4_000_000),
        ],
    )
    result = optimize_measured.opportunities(tenant_id, triage["id"], PERIOD)

    # Measured total = duplicate $6 + prompt caching $10.80 = $16.80 (guaranteed only).
    assert result["totals"]["measured"] == 16.8
    # Highest-savings measured lever sorts first.
    measured = [o for o in result["opportunities"] if o["savings_type"] == "measured"]
    assert measured[0]["lever"] == "prompt_caching"
    # The three savings types are tracked separately and never combined. The Sonnet
    # duplicate spend ($6) also surfaces a right-sizing ceiling ($6 × 0.733 = $4.40),
    # counted in modeled_ceiling — NOT in the guaranteed measured total.
    assert set(result["totals"]) == {"measured", "modeled_ceiling", "directional"}
    assert result["totals"]["modeled_ceiling"] == 4.4


def test_no_signals_no_cost_yields_no_opportunities(tenant_id):
    triage = features.add_feature(tenant_id, "AI threat triage")
    result = optimize_measured.opportunities(tenant_id, triage["id"], PERIOD)
    assert result["opportunities"] == []  # no signals, no inference cost
    assert result["totals"] == {"measured": 0.0, "modeled_ceiling": 0.0, "directional": 0.0}
    assert result["cache_utilization"] is None


def test_cache_utilization_surfaces_from_connector_without_sdk(tenant_id):
    # Tier A (opt spec §8, M-opt-5): the provider cost API reported cached input
    # tokens. Utilization must surface with NO SDK (usage_signal) rows at all.
    report = features.add_feature(tenant_id, "Report generator")
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        # One row reports cache tokens (openai), one doesn't (anthropic). The ratio
        # is over ALL input — a floor: 720k cached / (6M + 3M) input = 8%.
        conn.execute(
            """
            INSERT INTO inference_cost (tenant_id, feature_id, provider, model, amount,
                                        period, tokens_in, tokens_out, cached_tokens_in,
                                        source, confidence)
            VALUES
              (%s, %s, 'openai', 'gpt-4o-mini', 1250, %s, 6000000, 750000, 720000,
               'cost_api', 'med'),
              (%s, %s, 'anthropic', 'claude-haiku-4-5', 200, %s, 3000000, 100000, NULL,
               'cost_api', 'med')
            """,
            (tenant_id, report["id"], PERIOD, tenant_id, report["id"], PERIOD),
        )

    result = optimize_measured.opportunities(tenant_id, report["id"], PERIOD)
    assert result["cache_utilization"] == 0.08  # 720k / 9M, no usage_signal rows
    assert _measured_levers(result) == set()  # no measured/ceiling levers here


def test_applied_action_shows_projected_vs_realized(tenant_id):
    # opt spec §11: apply dedup in an earlier month; a later month reconciles
    # realized = projected − the lever's current avoidable spend.
    triage = features.add_feature(tenant_id, "AI threat triage")
    # This month's duplicates are worth $6 (2M input @ $3/M).
    hook.ingest_events(
        tenant_id,
        [
            _dup_event(triage["id"], "fp-a", 1_000_000),
            _dup_event(triage["id"], "fp-a", 1_000_000),
        ],
    )
    # Applied last month with a $100/mo projection.
    applied = optimize_measured.mark_applied(
        tenant_id, triage["id"], "duplicate_calls", 100.0, dt.date(2026, 5, 1)
    )
    assert applied["applied_on"] == "2026-05-01"

    result = optimize_measured.opportunities(tenant_id, triage["id"], PERIOD)  # June
    action = next(a for a in result["actions"] if a["lever"] == "duplicate_calls")
    assert action["status"] == "measured"
    assert action["projected_monthly"] == 100.0
    assert action["current_avoidable"] == 6.0
    assert action["realized_monthly"] == 94.0  # 100 − 6


def test_realized_saving_verifies_after_two_periods(tenant_id):
    # opt spec §20: applied in April, viewed in June (2 periods later) with a
    # positive realized drop -> the action and its opportunity are VERIFIED.
    triage = features.add_feature(tenant_id, "AI threat triage")
    hook.ingest_events(
        tenant_id,
        [_dup_event(triage["id"], "fp-a", 1_000_000), _dup_event(triage["id"], "fp-a", 1_000_000)],
    )
    optimize_measured.mark_applied(
        tenant_id, triage["id"], "duplicate_calls", 100.0, dt.date(2026, 4, 1)
    )
    result = optimize_measured.opportunities(tenant_id, triage["id"], PERIOD)  # June
    action = next(a for a in result["actions"] if a["lever"] == "duplicate_calls")
    assert action["status"] == "verified"  # 2 periods elapsed, realized > 0
    assert action["realized_monthly"] == 94.0
    # The opportunity's lifecycle status reflects the verified action.
    assert _opp(result, "duplicate_calls")["status"] == "verified"


def test_applied_this_period_is_pending_until_next(tenant_id):
    triage = features.add_feature(tenant_id, "AI threat triage")
    hook.ingest_events(tenant_id, [_dup_event(triage["id"], "fp-a", 1_000_000)])
    optimize_measured.mark_applied(tenant_id, triage["id"], "duplicate_calls", 50.0, PERIOD)

    result = optimize_measured.opportunities(tenant_id, triage["id"], PERIOD)
    action = result["actions"][0]
    assert action["status"] == "pending"  # applied this period — nothing to reconcile yet
    assert action["realized_monthly"] is None


def test_unmark_removes_the_action(tenant_id):
    triage = features.add_feature(tenant_id, "AI threat triage")
    optimize_measured.mark_applied(tenant_id, triage["id"], "prompt_caching", 20.0, PERIOD)
    assert optimize_measured.opportunities(tenant_id, triage["id"], PERIOD)["actions"]
    optimize_measured.unmark_applied(tenant_id, triage["id"], "prompt_caching")
    assert optimize_measured.opportunities(tenant_id, triage["id"], PERIOD)["actions"] == []


def test_mark_applied_unknown_feature_returns_none(tenant_id):
    assert (
        optimize_measured.mark_applied(
            tenant_id, "00000000-0000-0000-0000-000000000000", "duplicate_calls", 10.0
        )
        is None
    )


def test_cross_provider_arbitrage_from_connector_rows(tenant_id):
    # opt spec §16 M-opt-8: a feature on Together's Llama-70B is cheaper on
    # DeepInfra (same weights). Surfaces from connector data, no SDK signals.
    enrich = features.add_feature(tenant_id, "Log enrichment")
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        conn.execute(
            """
            INSERT INTO inference_cost (tenant_id, feature_id, provider, model, amount,
                                        period, tokens_in, tokens_out, source, confidence)
            VALUES (%s, %s, 'together', 'meta-llama-3.1-70b-instruct', 8.80, %s,
                    10000000, 0, 'cost_api', 'med')
            """,
            (tenant_id, enrich["id"], PERIOD),
        )

    result = optimize_measured.opportunities(tenant_id, enrich["id"], PERIOD)
    arb = _opp(result, "provider_switch")
    # 10M in: Together $0.88/M = $8.80 -> DeepInfra $0.35/M = $3.50, save $5.30 (60%).
    assert arb["projected_monthly_savings"] == 5.3
    assert arb["savings_type"] == "measured" and arb["source"] == "connector"
    assert arb["confidence"] == "high"
    # Provider switch is the lowest-effort lever (config change, identical weights).
    assert arb["engineering_effort"] == "very_low"
    assert arb["priority_score"] == 5.3  # 5.30 × high 1.0 × very_low 1.0
    assert "deepinfra" in arb["evidence"]
    assert "60% less" in arb["evidence"]
    assert arb["trail"][0]["note"].startswith("together → deepinfra")


def test_model_rightsizing_ceiling_from_real_spend(tenant_id):
    # opt spec §16 M-opt-7: a Sonnet feature could move to Haiku. The ceiling is
    # the feature's REAL spend × the rate saving at its token mix — quality-gated,
    # med confidence, and NOT counted in the guaranteed savings headline.
    triage = features.add_feature(tenant_id, "AI threat triage")
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        conn.execute(
            """
            INSERT INTO inference_cost (tenant_id, feature_id, provider, model, amount,
                                        period, tokens_in, tokens_out, source, confidence)
            VALUES (%s, %s, 'anthropic', 'claude-sonnet-4-6', 100.00, %s,
                    1000000, 1000000, 'cost_api', 'high')
            """,
            (tenant_id, triage["id"], PERIOD),
        )

    result = optimize_measured.opportunities(tenant_id, triage["id"], PERIOD)
    rs = _opp(result, "model_rightsizing")
    # sonnet $18 vs haiku $4.80 per (1M,1M) -> 73.33% saving on the $100 spend.
    assert rs["projected_monthly_savings"] == 73.33
    assert rs["confidence"] == "med"
    assert rs["savings_type"] == "modeled_ceiling"  # quality-gated ceiling
    # High effort (needs a quality eval); priority = 73.33 × med 0.6 × high 0.3 = 13.2.
    assert rs["engineering_effort"] == "high"
    assert rs["priority_score"] == 13.2
    assert "claude-haiku-4-5" in rs["evidence"] and "73%" in rs["evidence"]
    assert rs["trail"][0]["note"].startswith("up to")
    # The ceiling is counted in the modeled total, NOT the guaranteed measured total.
    assert result["totals"]["measured"] == 0.0
    assert result["totals"]["modeled_ceiling"] == 73.33


def test_measured_finding_supersedes_directional_estimate(tenant_id):
    # opt spec §22: a big input-heavy Sonnet feature yields BOTH a measured prompt
    # caching finding and a directional "Prompt caching" estimate. The estimate is
    # flagged as overlapping and dropped from the directional total.
    triage = features.add_feature(tenant_id, "AI threat triage")
    # Connector spend (input-heavy) drives the heuristic estimate...
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        conn.execute(
            """
            INSERT INTO inference_cost (tenant_id, feature_id, provider, model, amount,
                                        period, tokens_in, tokens_out, source, confidence)
            VALUES (%s, %s, 'anthropic', 'claude-haiku-4-5', 1000.00, %s,
                    9000000, 1000000, 'cost_api', 'high')
            """,
            (tenant_id, triage["id"], PERIOD),
        )
    # ...and a measured prompt-caching finding from the SDK.
    hook.ingest_events(
        tenant_id, [_prefix_event(triage["id"], "fp-p", 1000, 4000, 0, 4_000_000)]
    )

    result = optimize_measured.opportunities(tenant_id, triage["id"], PERIOD)
    measured_pc = _opp(result, "prompt_caching")
    directional_pc = _opp(result, "prompt_caching_est")
    # The measured finding wins; the estimate is flagged as overlapping it.
    assert measured_pc["overlaps"] is None
    assert directional_pc["overlaps"] == "Prompt caching"
    # The superseded estimate is NOT counted in the directional total.
    non_overlapped = sum(
        o["projected_monthly_savings"]
        for o in result["opportunities"]
        if o["savings_type"] == "directional" and o["overlaps"] is None
    )
    assert round(non_overlapped, 2) == result["totals"]["directional"]
    # Including the superseded estimate would have inflated the total.
    assert directional_pc["projected_monthly_savings"] > 0


def test_priority_favors_easy_high_confidence_wins():
    # opt spec §19: priority = savings × confidence × effort. A cheap, guaranteed,
    # very-low-effort fix should outrank a bigger but risky, high-effort one.
    easy = optimize_measured._priority(100.0, "high", "very_low")  # 100
    risky = optimize_measured._priority(300.0, "low", "high")  # 300 × 0.3 × 0.3 = 27
    assert easy > risky


def test_unknown_feature_returns_none(tenant_id):
    assert (
        optimize_measured.opportunities(tenant_id, "00000000-0000-0000-0000-000000000000", PERIOD)
        is None
    )
