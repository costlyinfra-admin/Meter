"""Measured optimization opportunities: savings computed from seeded signals
must match a hand-calc from the price book (opt spec M-opt-3)."""

from __future__ import annotations

import datetime as dt

import pytest
from meter import features, hook, optimize_measured
from meter.db import app_dsn, connect, tenant_tx

PERIOD = dt.date(2026, 6, 1)


def _dup_event(feature_id, fingerprint, tokens_in, version="v2", scope="explicit"):
    """A repeated-request signal as the current SDK sends one.

    `version` defaults to v2 — the identity that compares the whole request.
    Pass "v1" for a row as the old SDK wrote it: messages only, no scope, and
    deliberately excluded from the finding.
    """
    signal = {"kind": "duplicate", "fingerprint": fingerprint, "count": 1}
    if version == "v2":
        signal["fingerprint_version"] = "v2"
        signal["scope_kind"] = scope
    return {
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "tokens_in": tokens_in,
        "tokens_out": 0,
        "feature_id": feature_id,
        "occurred_at": "2026-06-15T10:00:00Z",
        "signal": signal,
    }


def _prefix_event(
    feature_id,
    fingerprint,
    count,
    prefix_tokens,
    cached_count,
    tokens_in,
    measured=False,
    write_calls=0,
    windows=0,
    samples=1,
    model="claude-sonnet-4-6",
):
    """A flushed prefix summary as the current SDK sends one.

    `windows` is how many cache writes enabling caching would take. Pass None
    for a summary from an SDK too old to count them — the write side then
    cannot be priced and the finding is a ceiling, not a saving.
    """
    signal = {
        "kind": "prefix",
        "fingerprint": fingerprint,
        "count": count,
        # The SDK's character estimate, always in its own field.
        "prefix_tokens": prefix_tokens,
        "cached_count": cached_count,
        "tokens_in": tokens_in,
        "tokens_out": 0,
        "prefix_measured": measured,
        "write_calls": write_calls,
    }
    if measured:
        # One provider count of exactly that size, so the mean is the figure
        # the test named. `samples` overrides it where the average matters.
        signal["prefix_tokens_sum"] = prefix_tokens * samples
        signal["prefix_tokens_n"] = samples
    if windows is not None:
        signal["cache_windows"] = windows
    return {
        "provider": "anthropic",
        "model": model,
        "feature_id": feature_id,
        "occurred_at": "2026-06-16T10:00:00Z",
        "signal": signal,
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

    # 2M input tokens @ $3/M (claude-sonnet-4-6) = $6.00 at LIST price.
    assert dup["projected_monthly_savings"] == 6.0
    assert dup["source"] == "sdk"
    # A ceiling, not a guaranteed saving. The repeat COUNT is exact; whether the
    # later calls could have been served from the first depends on freshness,
    # authorization, deliberate sampling and application policy, none of which
    # Meter can see — and the dollars are list rate against aggregates that
    # cannot reconstruct the billed cost.
    assert dup["savings_type"] == "modeled_ceiling"
    assert dup["confidence"] == "med"
    assert dup["title"] == "Repeated request candidates"
    # Guidance says what is measured and what is assumed.
    assert "identical" in dup["validation_guidance"]
    assert "list-price ceiling" in dup["verification"]
    assert dup["status"] == "detected"
    assert "2 repeated requests across 1 distinct request shapes" in dup["evidence"]
    assert "list rate" in dup["evidence"]
    assert dup["trail"][0]["call_count"] == 2
    # Fingerprints in the trail are short salted-hash handles, never prompt text.
    assert dup["trail"][0]["fingerprint"] == "fp-a"[:12]


def test_prefix_caching_savings_match_the_price_book(tenant_id):
    triage = features.add_feature(tenant_id, "AI threat triage")
    # A 4,000-token static prefix across 1,000 uncached calls in 20 TTL windows.
    hook.ingest_events(
        tenant_id,
        [_prefix_event(triage["id"], "fp-p", 1000, 4000, 0, 4_000_000, windows=20)],
    )

    result = optimize_measured.opportunities(tenant_id, triage["id"], PERIOD)
    prefix = _opp(result, "prompt_caching")

    # 4,000 tokens * $3/M = $0.012 a call.
    #   reads:  1,000 * (1 - 0.10)    =  900 call-equivalents saved
    #   writes:    20 * (1.25 - 0.10) =   23 call-equivalents spent
    # (900 - 23) * $0.012 = $10.524, to $10.52.
    assert prefix["projected_monthly_savings"] == 10.52
    assert "4,000-token static prefix" in prefix["evidence"]
    assert "1,000 uncached calls" in prefix["evidence"]
    assert "net of the 20 cache writes" in prefix["evidence"]
    # The provider reported no prefix size here, so 4,000 is the SDK's
    # characters-over-four estimate. Priced, but not called a measurement.
    assert prefix["confidence"] == "med"
    assert "estimated from request length" in prefix["evidence"]
    # No calls were cached yet.
    assert result["cache_utilization"] == 0.0


def test_a_prefix_the_provider_measured_is_reported_as_measured(tenant_id):
    """Anthropic's cache_creation_input_tokens is a real token count.

    Same dollars, different claim: with the provider's own figure behind the
    token count there is nothing estimated left in the arithmetic, and the
    finding can say so.
    """
    triage = features.add_feature(tenant_id, "AI threat triage")
    hook.ingest_events(
        tenant_id,
        [
            _prefix_event(
                triage["id"], "fp-p", 1000, 4000, 0, 4_000_000, measured=True, windows=20
            )
        ],
    )

    prefix = _opp(
        optimize_measured.opportunities(tenant_id, triage["id"], PERIOD), "prompt_caching"
    )
    assert prefix["projected_monthly_savings"] == 10.52
    assert prefix["confidence"] == "high"
    assert prefix["savings_type"] == "measured"
    assert "measured by the provider" in prefix["evidence"]
    assert prefix["trail"][0]["prefix_measured"] is True


def test_one_estimated_prefix_keeps_the_whole_finding_off_measured(tenant_id):
    """A finding is only as measured as its least measured input."""
    triage = features.add_feature(tenant_id, "AI threat triage")
    hook.ingest_events(
        tenant_id,
        [
            _prefix_event(
                triage["id"], "fp-real", 1000, 4000, 0, 4_000_000, measured=True, windows=20
            ),
            _prefix_event(
                triage["id"], "fp-guess", 1000, 4000, 0, 4_000_000, measured=False, windows=20
            ),
        ],
    )

    prefix = _opp(
        optimize_measured.opportunities(tenant_id, triage["id"], PERIOD), "prompt_caching"
    )
    assert prefix["confidence"] == "med"


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
    # The metered traffic has to be able to account for the signals: a prefix
    # summary describing 1,000 calls on a feature billed for two is a
    # contradiction, and _bound_by_spend now treats it as one. 2 x 10M input
    # tokens on Sonnet is $60 of spend, comfortably above what is claimed here.
    hook.ingest_events(
        tenant_id,
        [
            _dup_event(triage["id"], "fp-a", 10_000_000),
            _dup_event(triage["id"], "fp-a", 10_000_000),
            _prefix_event(triage["id"], "fp-p", 1000, 4000, 0, 4_000_000,
                          measured=True, windows=20),
        ],
    )
    result = optimize_measured.opportunities(tenant_id, triage["id"], PERIOD)

    # Measured total is prompt caching alone. The repeated-request finding is a
    # ceiling, not a measured saving, so it is not in here.
    assert result["totals"]["measured"] == 10.52
    measured = [o for o in result["opportunities"] if o["savings_type"] == "measured"]
    assert measured[0]["lever"] == "prompt_caching"

    # ...and it is not added to the ceiling total either, because a repeated
    # request and a repeated PREFIX are the same input tokens counted two ways.
    # The overlap cannot be quantified from these aggregates, so the smaller is
    # dropped rather than both being summed as if they were independent.
    dup = _opp(result, "duplicate_calls")
    assert dup["overlaps"] == "Prompt caching"
    assert set(result["totals"]) == {"measured", "modeled_ceiling", "directional"}
    # Only the right-sizing ceiling remains ($60 × 0.6667 = $40.00).
    assert result["totals"]["modeled_ceiling"] == 40.0


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
    # positive realized drop AND a bill that agrees -> VERIFIED.
    triage = features.add_feature(tenant_id, "AI threat triage")
    hook.ingest_events(
        tenant_id,
        [_dup_event(triage["id"], "fp-a", 1_000_000), _dup_event(triage["id"], "fp-a", 1_000_000)],
    )  # June: 2 calls, $6 billed -> $3 a call
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        # April: 10 calls for $60 -> $6 a call. Work got cheaper per unit, which
        # is the part the signals cannot tell you and the invoice can.
        conn.execute(
            """
            INSERT INTO inference_cost (tenant_id, feature_id, provider, model, amount,
                                        period, tokens_in, tokens_out, request_count,
                                        source, confidence)
            VALUES (%s, %s, 'anthropic', 'claude-sonnet-4-6', 60.00, %s,
                    20000000, 0, 10, 'cost_api', 'high')
            """,
            (tenant_id, triage["id"], dt.date(2026, 4, 1)),
        )
    optimize_measured.mark_applied(
        tenant_id, triage["id"], "duplicate_calls", 100.0, dt.date(2026, 4, 1)
    )
    result = optimize_measured.opportunities(tenant_id, triage["id"], PERIOD)  # June
    action = next(a for a in result["actions"] if a["lever"] == "duplicate_calls")
    assert action["status"] == "verified"  # 2 periods, realized > 0, bill agrees
    assert action["realized_monthly"] == 94.0
    assert action["bill_agrees"] is True
    assert (action["unit_cost_before"], action["unit_cost_now"]) == (6.0, 3.0)
    assert action["unit_cost_unit"] == "call"
    assert action["verification_note"] is None
    # The opportunity's lifecycle status reflects the verified action.
    assert _opp(result, "duplicate_calls")["status"] == "verified"


def test_traffic_falling_for_its_own_reasons_is_not_a_verified_saving(tenant_id):
    """The failure this gate exists for.

    `realized` is a projection minus a projection — both sides come from the
    same signals, neither has touched an invoice. A feature whose traffic
    halves for reasons that have nothing to do with the fix has a smaller
    avoidable spend, and that read as a realized saving that turned VERIFIED
    two periods later. Cost per call is unchanged here: fewer calls at the same
    price each is not money saved.
    """
    triage = features.add_feature(tenant_id, "AI threat triage")
    hook.ingest_events(
        tenant_id,
        [_dup_event(triage["id"], "fp-a", 1_000_000), _dup_event(triage["id"], "fp-a", 1_000_000)],
    )  # June: 2 calls, $6 -> $3 a call
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        # April: 20 calls for $60 -> $3 a call. Twice the traffic, same price.
        conn.execute(
            """
            INSERT INTO inference_cost (tenant_id, feature_id, provider, model, amount,
                                        period, tokens_in, tokens_out, request_count,
                                        source, confidence)
            VALUES (%s, %s, 'anthropic', 'claude-sonnet-4-6', 60.00, %s,
                    20000000, 0, 20, 'cost_api', 'high')
            """,
            (tenant_id, triage["id"], dt.date(2026, 4, 1)),
        )
    optimize_measured.mark_applied(
        tenant_id, triage["id"], "duplicate_calls", 100.0, dt.date(2026, 4, 1)
    )
    action = next(
        a
        for a in optimize_measured.opportunities(tenant_id, triage["id"], PERIOD)["actions"]
        if a["lever"] == "duplicate_calls"
    )
    # Everything the signals can show is satisfied — two periods, a positive
    # realized figure — and it still does not advance.
    assert action["realized_monthly"] == 94.0
    assert action["status"] == "measured"
    assert action["bill_agrees"] is False
    assert "cost per unit of work did not fall" in action["verification_note"]


def test_a_saving_with_no_bill_to_check_it_against_is_not_verified(tenant_id):
    """No billed cost in the applied period.

    The signals still say the avoidable spend fell, and that is still worth
    showing — but "verified" is the terminal Prove state, and promoting an
    action on the strength of the only half that could be checked is how a
    number nobody can audit ends up in front of a CFO.
    """
    triage = features.add_feature(tenant_id, "AI threat triage")
    hook.ingest_events(
        tenant_id,
        [_dup_event(triage["id"], "fp-a", 1_000_000), _dup_event(triage["id"], "fp-a", 1_000_000)],
    )
    optimize_measured.mark_applied(
        tenant_id, triage["id"], "duplicate_calls", 100.0, dt.date(2026, 4, 1)
    )
    action = next(
        a
        for a in optimize_measured.opportunities(tenant_id, triage["id"], PERIOD)["actions"]
        if a["lever"] == "duplicate_calls"
    )
    assert action["status"] == "measured"
    assert action["bill_agrees"] is None  # the question could not be asked
    assert "no billed cost to compare" in action["verification_note"]


def test_cost_per_unit_falls_back_to_tokens_when_calls_are_not_reported(tenant_id):
    """Connector rows often carry no request count. Tokens are the next unit.

    A prefix summary is a flushed client-side count that is never re-costed, so
    this feature's only billed rows are the connector's — which is the shape
    almost every real tenant is in.
    """
    triage = features.add_feature(tenant_id, "AI threat triage")
    hook.ingest_events(
        tenant_id,
        [_prefix_event(triage["id"], "fp-p", 1000, 4000, 0, 4_000_000, windows=20)],
    )
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        for period, amount in ((dt.date(2026, 4, 1), 60.00), (PERIOD, 10.00)):
            # 10M input tokens both months: $6 per 1M, then $1 per 1M.
            conn.execute(
                """
                INSERT INTO inference_cost (tenant_id, feature_id, provider, model, amount,
                                            period, tokens_in, tokens_out, source, confidence)
                VALUES (%s, %s, 'openai', 'gpt-4o', %s, %s, 10000000, 0, 'cost_api', 'high')
                """,
                (tenant_id, triage["id"], amount, period),
            )
    optimize_measured.mark_applied(
        tenant_id, triage["id"], "prompt_caching", 100.0, dt.date(2026, 4, 1)
    )
    action = next(
        a
        for a in optimize_measured.opportunities(tenant_id, triage["id"], PERIOD)["actions"]
        if a["lever"] == "prompt_caching"
    )
    assert action["unit_cost_unit"] == "1M input tokens"
    assert (action["unit_cost_before"], action["unit_cost_now"]) == (6.0, 1.0)
    assert action["status"] == "verified"


def test_two_periods_measured_in_different_units_are_not_compared(tenant_id):
    """Cost per call and cost per million tokens are not the same number.

    A connector that starts reporting request counts mid-year would otherwise
    look like a collapse in unit cost, and verify everything outstanding.
    """
    triage = features.add_feature(tenant_id, "AI threat triage")
    hook.ingest_events(
        tenant_id,
        [_prefix_event(triage["id"], "fp-p", 1000, 4000, 0, 4_000_000, windows=20)],
    )
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        conn.execute(
            """
            INSERT INTO inference_cost (tenant_id, feature_id, provider, model, amount,
                                        period, tokens_in, tokens_out, source, confidence)
            VALUES (%s, %s, 'openai', 'gpt-4o', 60.00, %s, 10000000, 0, 'cost_api', 'high')
            """,
            (tenant_id, triage["id"], dt.date(2026, 4, 1)),
        )
        conn.execute(
            """
            INSERT INTO inference_cost (tenant_id, feature_id, provider, model, amount,
                                        period, tokens_in, tokens_out, request_count,
                                        source, confidence)
            VALUES (%s, %s, 'openai', 'gpt-4o', 10.00, %s, 10000000, 0, 500,
                    'cost_api', 'high')
            """,
            (tenant_id, triage["id"], PERIOD),
        )
    optimize_measured.mark_applied(
        tenant_id, triage["id"], "prompt_caching", 100.0, dt.date(2026, 4, 1)
    )
    action = next(
        a
        for a in optimize_measured.opportunities(tenant_id, triage["id"], PERIOD)["actions"]
        if a["lever"] == "prompt_caching"
    )
    # Two periods elapsed and a positive realized figure — everything the
    # signals can show — and it still does not advance.
    assert action["realized_monthly"] > 0
    assert action["status"] == "measured"
    assert action["bill_agrees"] is None
    assert action["unit_cost_unit"] is None
    assert "different units" in action["verification_note"]


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
    # sonnet $18 vs haiku $6 per (1M,1M) -> 66.67% saving on the $100 spend.
    assert rs["projected_monthly_savings"] == 66.67
    assert rs["confidence"] == "med"
    assert rs["savings_type"] == "modeled_ceiling"  # quality-gated ceiling
    # High effort (needs a quality eval); priority = 66.67 × med 0.6 × high 0.3 = 12.0.
    assert rs["engineering_effort"] == "high"
    assert rs["priority_score"] == 12.0
    assert "claude-haiku-4-5" in rs["evidence"] and "67%" in rs["evidence"]
    assert rs["trail"][0]["note"].startswith("up to")
    # The ceiling is counted in the modeled total, NOT the guaranteed measured total.
    assert result["totals"]["measured"] == 0.0
    assert result["totals"]["modeled_ceiling"] == 66.67


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
        tenant_id,
        [_prefix_event(triage["id"], "fp-p", 1000, 4000, 0, 4_000_000, windows=20)],
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


# --- request identity, honestly labelled ----------------------------------
def test_legacy_fingerprints_are_excluded_and_said_so(tenant_id):
    """v1 compared provider, model and messages and nothing else, so calls
    differing in temperature, system or tools hashed identically. Those rows are
    real history and are kept, but an incomplete match must never be presented
    as an exact one."""
    triage = features.add_feature(tenant_id, "AI threat triage")
    hook.ingest_events(
        tenant_id,
        [
            _dup_event(triage["id"], "fp-old", 1_000_000, version="v1"),
            _dup_event(triage["id"], "fp-old", 1_000_000, version="v1"),
            _dup_event(triage["id"], "fp-new", 1_000_000),
            _dup_event(triage["id"], "fp-new", 1_000_000),
        ],
    )

    dup = _opp(optimize_measured.opportunities(tenant_id, triage["id"], PERIOD), "duplicate_calls")
    # Only the v2 pair is priced: 2M tokens @ $3/M, not 4M.
    assert dup["projected_monthly_savings"] == 6.0
    # And the drop is explained rather than silent — the number fell because the
    # detector was corrected, not because the customer's traffic changed.
    assert "2 further repeats were matched by an older, incomplete fingerprint" in dup["evidence"]


def test_a_v1_and_a_v2_row_for_one_fingerprint_stay_separate(tenant_id):
    """Different comparisons of the same request are different facts."""
    triage = features.add_feature(tenant_id, "AI threat triage")
    hook.ingest_events(
        tenant_id,
        [
            _dup_event(triage["id"], "same-fp", 1_000_000, version="v1"),
            _dup_event(triage["id"], "same-fp", 1_000_000),
        ],
    )
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        rows = conn.execute(
            "SELECT fingerprint_version, call_count FROM usage_signal "
            "WHERE signal_kind = 'duplicate' ORDER BY fingerprint_version"
        ).fetchall()
    assert [(r[0], r[1]) for r in rows] == [("v1", 1), ("v2", 1)]


def test_an_unscoped_repeat_is_never_presented_as_safe_reuse(tenant_id):
    """Nobody said the two calls belong to the same user, tenant or cache."""
    triage = features.add_feature(tenant_id, "AI threat triage")
    hook.ingest_events(
        tenant_id,
        [
            _dup_event(triage["id"], "fp-a", 1_000_000, scope="unscoped"),
            _dup_event(triage["id"], "fp-a", 1_000_000, scope="unscoped"),
        ],
    )

    dup = _opp(optimize_measured.opportunities(tenant_id, triage["id"], PERIOD), "duplicate_calls")
    assert "reuse safety is unverified" in dup["evidence"]
    assert dup["confidence"] == "low"  # below a scoped repeat, never above it
    assert dup["savings_type"] == "modeled_ceiling"


# --- what absent telemetry may not prove ----------------------------------
def test_an_applied_action_is_not_verified_just_because_telemetry_stopped(tenant_id):
    """The trap that excluding legacy signals would otherwise spring.

    `realized = projected - current avoidable`. If the current figure falls to
    zero because nobody is reporting any more — optimize mode switched off, or
    v1 rows no longer trusted — the action reads as a complete success and, two
    periods on, as VERIFIED. Meter would be reporting a win it had simply
    stopped being able to look for.
    """
    triage = features.add_feature(tenant_id, "AI threat triage")
    optimize_measured.mark_applied(
        tenant_id, triage["id"], "duplicate_calls", 40.0, dt.date(2026, 3, 1)
    )
    # No v2 signals at all this period: nothing is observing this lever.
    result = optimize_measured.opportunities(tenant_id, triage["id"], PERIOD)
    action = next(a for a in result["actions"] if a["lever"] == "duplicate_calls")

    assert action["status"] == "unverifiable"
    assert action["realized_monthly"] is None, "absence of evidence is not a realized saving"
    assert action["current_avoidable"] is None
    assert action["projected_monthly"] == 40.0  # the history itself is preserved


def test_telemetry_that_is_arriving_and_finding_nothing_does_verify(tenant_id):
    """The other half: optimize mode running and reporting no repeats IS
    evidence the fix worked, and must still be able to reach verified."""
    triage = features.add_feature(tenant_id, "AI threat triage")
    optimize_measured.mark_applied(
        tenant_id, triage["id"], "duplicate_calls", 40.0, dt.date(2026, 3, 1)
    )
    # A v2 prefix-free signal for a DIFFERENT fingerprint: the SDK is live and
    # reporting, it simply has no repeats to report for the applied lever.
    hook.ingest_events(tenant_id, [_dup_event(triage["id"], "fp-other", 1)])

    result = optimize_measured.opportunities(tenant_id, triage["id"], PERIOD)
    action = next(a for a in result["actions"] if a["lever"] == "duplicate_calls")
    assert action["status"] in ("measured", "verified")
    assert action["realized_monthly"] is not None


# ---------------------------------------------------------------------------
# Counting one dollar once, here too (dashboard._NOT_DOUBLE_COUNTED)
# ---------------------------------------------------------------------------
# A provider with both a connector and the SDK describes the same spend twice.
# dashboard.py reconciles that everywhere it reads inference_cost; this module
# reads the same table for three of its four detectors and did not, so a
# customer running both had every connector-sourced finding computed from
# doubled tokens and doubled dollars.


def _cost_row(conn, tenant_id, feature_id, **over):
    row = {
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "amount": 100.00,
        "tokens_in": 1_000_000,
        "tokens_out": 1_000_000,
        "cached_tokens_in": None,
        "source": "cost_api",
        "environment": None,
    }
    row.update(over)
    conn.execute(
        """
        INSERT INTO inference_cost (tenant_id, feature_id, provider, model, amount,
                                    period, tokens_in, tokens_out, cached_tokens_in,
                                    source, confidence, environment)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'high', %s)
        """,
        (
            tenant_id, feature_id, row["provider"], row["model"], row["amount"],
            PERIOD, row["tokens_in"], row["tokens_out"], row["cached_tokens_in"],
            row["source"], row["environment"],
        ),
    )


def test_rightsizing_does_not_double_count_a_metered_connector_provider(tenant_id):
    triage = features.add_feature(tenant_id, "AI threat triage")
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        # The connector reports the bill; the SDK reports the calls behind it.
        # One month's spend, described twice.
        _cost_row(conn, tenant_id, triage["id"], source="cost_api")
        _cost_row(conn, tenant_id, triage["id"], source="hook")

    rs = _opp(optimize_measured.opportunities(tenant_id, triage["id"], PERIOD),
              "model_rightsizing")
    # The same $100 month as test_model_rightsizing_ceiling_from_real_spend, so
    # the same ceiling. Summing both rows would propose $133.34 of savings on a
    # $100 bill.
    assert rs["projected_monthly_savings"] == 66.67


def test_rightsizing_ignores_spend_marked_ignore(tenant_id):
    triage = features.add_feature(tenant_id, "AI threat triage")
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        _cost_row(conn, tenant_id, triage["id"])
        # Spend the user excluded from reporting. Proposing a saving on it
        # offers money back from a bill Meter has been told not to count.
        _cost_row(conn, tenant_id, triage["id"], amount=900.00, environment="ignore")

    rs = _opp(optimize_measured.opportunities(tenant_id, triage["id"], PERIOD),
              "model_rightsizing")
    assert rs["projected_monthly_savings"] == 66.67


def test_arbitrage_does_not_double_count_a_metered_connector_provider(tenant_id):
    enrich = features.add_feature(tenant_id, "Log enrichment")
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        for source in ("cost_api", "hook"):
            _cost_row(conn, tenant_id, enrich["id"], provider="together",
                      model="meta-llama-3.1-70b-instruct", amount=8.80,
                      tokens_in=10_000_000, tokens_out=0, source=source)

    arb = _opp(optimize_measured.opportunities(tenant_id, enrich["id"], PERIOD),
               "provider_switch")
    # 10M tokens, not 20M: $8.80 on Together vs $3.50 on DeepInfra.
    assert arb["projected_monthly_savings"] == 5.3


def test_cache_utilization_is_read_from_the_billed_rows_only(tenant_id):
    report = features.add_feature(tenant_id, "Report generator")
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        # The connector sees the cache reads; the SDK rows for the same calls
        # carry no cache field at all. Counting both puts the SDK's input in the
        # denominator with nothing in the numerator, and halves the ratio.
        _cost_row(conn, tenant_id, report["id"], tokens_in=1_000_000,
                  tokens_out=0, cached_tokens_in=800_000, source="cost_api")
        _cost_row(conn, tenant_id, report["id"], tokens_in=1_000_000,
                  tokens_out=0, source="hook")
        # ...and ignored spend is not part of the question either.
        _cost_row(conn, tenant_id, report["id"], tokens_in=3_000_000,
                  tokens_out=0, source="cost_api", environment="ignore")

    result = optimize_measured.opportunities(tenant_id, report["id"], PERIOD)
    assert result["cache_utilization"] == 0.8  # 800k / 1M


# ---------------------------------------------------------------------------
# Caching costs something (opt spec §7.1)
# ---------------------------------------------------------------------------


def test_sparse_traffic_that_would_only_ever_write_is_not_a_saving(tenant_id):
    """150 calls a month, one every few hours, is one cache write per call.

    A 5-minute entry is gone before the next call arrives, so caching this
    prefix means paying 1.25x the input rate every time and never reading it
    back — a 25% INCREASE. Pricing the read discount alone reported $8.10 of
    savings for making the bill worse.
    """
    triage = features.add_feature(tenant_id, "AI threat triage")
    hook.ingest_events(
        tenant_id,
        [_prefix_event(triage["id"], "fp-sparse", 150, 20_000, 0, 3_000_000, windows=150)],
    )

    result = optimize_measured.opportunities(tenant_id, triage["id"], PERIOD)
    assert "prompt_caching" not in _measured_levers(result)


def test_a_prefix_the_provider_is_already_writing_is_not_an_opportunity(tenant_id):
    """Caching on, working, and the recommendation was to turn it on.

    Steady traffic on a 5-minute TTL writes the entry back thousands of times a
    month. Each of those calls reports a cache CREATION and no cache read, so
    every one of them was counted as an uncached call — and a creation is also
    the only source of a provider-measured prefix size, so those same calls
    pushed the finding to HIGH confidence. Meter told customers who had already
    done this to do it, and was most certain about it for exactly them.
    """
    triage = features.add_feature(tenant_id, "AI threat triage")
    hits, writes = 100_000, 8_640
    hook.ingest_events(
        tenant_id,
        [
            _prefix_event(
                triage["id"], "fp-live", hits + writes, 4000, hits,
                (hits + writes) * 4000, measured=True, write_calls=writes, windows=writes,
            )
        ],
    )

    result = optimize_measured.opportunities(tenant_id, triage["id"], PERIOD)
    assert "prompt_caching" not in _measured_levers(result)


def test_an_sdk_that_cannot_count_writes_yields_a_ceiling_not_a_saving(tenant_id):
    """Rows written before 0062 have no window count.

    The read side is still real, so the finding stands — but half the trade is
    unpriced, and an unpriced cost side is exactly what turns a saving into an
    upper bound.
    """
    triage = features.add_feature(tenant_id, "AI threat triage")
    hook.ingest_events(
        tenant_id,
        [
            _prefix_event(
                triage["id"], "fp-old", 1000, 4000, 0, 4_000_000, measured=True, windows=None
            )
        ],
    )

    result = optimize_measured.opportunities(tenant_id, triage["id"], PERIOD)
    prefix = _opp(result, "prompt_caching")
    assert prefix["savings_type"] == "modeled_ceiling"
    assert prefix["confidence"] == "med"
    assert prefix["projected_monthly_savings"] == 10.8  # reads only, no write premium
    assert "too old to count" in prefix["evidence"]
    # A ceiling never lands in the guaranteed total.
    assert result["totals"]["measured"] == 0.0
    assert result["totals"]["modeled_ceiling"] == 10.8


def test_the_write_premium_follows_the_provider_price_book(tenant_id):
    """Only Anthropic charges a separate write premium; the rest bill it as input.

    OpenAI has no cache-write charge, so there is no cost side to net off and
    the whole read discount stands.
    """
    report = features.add_feature(tenant_id, "Report generator")
    event = _prefix_event(report["id"], "fp-oai", 1000, 4000, 0, 4_000_000,
                          measured=True, windows=20, model="gpt-4o")
    event["provider"] = "openai"
    hook.ingest_events(tenant_id, [event])

    prefix = _opp(
        optimize_measured.opportunities(tenant_id, report["id"], PERIOD), "prompt_caching"
    )
    # OpenAI bills a write as ordinary input, so a write costs no PREMIUM —
    # but it still forgoes the discount the read would have had:
    #   (1,000 * (1 - 0.50) - 20 * (1 - 0.50)) * 4,000 * $2.50/M = $4.90.
    assert prefix["projected_monthly_savings"] == 4.9
    assert prefix["savings_type"] == "measured"


def test_a_prefix_written_as_often_as_it_is_read_is_not_offered(tenant_id):
    """Windows are counted per process; a provider's cache is account-wide.

    Replicas each open their own window for an entry the account wrote once, so
    the count can exceed the calls it is meant to explain. Capped at one write
    per call, that is a prefix rewritten every time it is used — which is what
    caching costs money for, so there is no finding rather than a big one.
    """
    triage = features.add_feature(tenant_id, "AI threat triage")
    hook.ingest_events(
        tenant_id,
        [
            _prefix_event(triage["id"], "fp-fanout", 1000, 4000, 0, 4_000_000,
                          measured=True, windows=50_000)
        ],
    )

    result = optimize_measured.opportunities(tenant_id, triage["id"], PERIOD)
    assert "prompt_caching" not in _measured_levers(result)


def test_window_counts_from_several_processes_add_up(tenant_id):
    """Each replica flushes its own summary for the same prefix fingerprint."""
    triage = features.add_feature(tenant_id, "AI threat triage")
    hook.ingest_events(
        tenant_id,
        [
            _prefix_event(triage["id"], "fp-p", 500, 4000, 0, 2_000_000,
                          measured=True, windows=8),
            _prefix_event(triage["id"], "fp-p", 500, 4000, 0, 2_000_000,
                          measured=True, windows=12),
        ],
    )

    prefix = _opp(
        optimize_measured.opportunities(tenant_id, triage["id"], PERIOD), "prompt_caching"
    )
    assert "net of the 20 cache writes" in prefix["evidence"]


def test_one_reporter_without_a_window_count_leaves_the_whole_finding_a_ceiling(tenant_id):
    """An old SDK on one replica and a current one on another.

    Summing them would price part of the write side and silently drop the
    rest, which reads as a smaller cost than was incurred. The finding degrades
    instead — the same rule prefix_measured already follows.
    """
    triage = features.add_feature(tenant_id, "AI threat triage")
    hook.ingest_events(
        tenant_id,
        [
            _prefix_event(triage["id"], "fp-new", 1000, 4000, 0, 4_000_000,
                          measured=True, windows=20),
            _prefix_event(triage["id"], "fp-old", 1000, 4000, 0, 4_000_000,
                          measured=True, windows=None),
        ],
    )

    prefix = _opp(
        optimize_measured.opportunities(tenant_id, triage["id"], PERIOD), "prompt_caching"
    )
    assert prefix["savings_type"] == "modeled_ceiling"
    assert prefix["confidence"] == "med"


def test_calls_already_written_to_cache_are_left_out_of_the_saving(tenant_id):
    """A partly-cached prefix: reads, writes, and calls doing neither.

    On a 1-hour TTL a live cache is rewritten rarely, so the writes and the
    genuinely uncached calls are different populations and both are visible.
    Only the second is an opportunity; counting the writes in it charges the
    customer's own cache maintenance back to them as a saving.
    """
    triage = features.add_feature(tenant_id, "AI threat triage")
    hook.ingest_events(
        tenant_id,
        [
            _prefix_event(
                triage["id"], "fp-partial", 100_000, 4000, 90_000, 400_000_000,
                measured=True, write_calls=1_000, windows=1_000,
            )
        ],
    )

    prefix = _opp(
        optimize_measured.opportunities(tenant_id, triage["id"], PERIOD), "prompt_caching"
    )
    # 100,000 - 90,000 read - 1,000 written = 9,000 uncached, at $0.012 a call:
    #   (9,000 * 0.90 - 1,000 * 1.15) * $0.012 = $83.40.
    # Counting the 1,000 writes as uncached would offer $94.20.
    assert prefix["projected_monthly_savings"] == 83.4
    assert prefix["trail"][0]["already_written"] == 1_000


def test_a_prefix_caching_would_cost_money_on_cannot_fund_another_one(tenant_id):
    """Two prefixes, one worth caching and one not.

    Caching them is two independent decisions: you would enable it on the first
    and leave the second alone. Letting the loss-maker net off against the
    winner reports a smaller saving than taking the advice would produce, and
    hides the fact that one of the two should not be touched.
    """
    triage = features.add_feature(tenant_id, "AI threat triage")
    hook.ingest_events(
        tenant_id,
        [
            # Dense traffic: 1,000 calls, 20 windows. Worth caching.
            _prefix_event(triage["id"], "fp-good", 1000, 4000, 0, 4_000_000,
                          measured=True, windows=20),
            # Sparse traffic: every call would write and none would read.
            _prefix_event(triage["id"], "fp-bad", 500, 20_000, 0, 10_000_000,
                          measured=True, windows=500),
        ],
    )

    prefix = _opp(
        optimize_measured.opportunities(tenant_id, triage["id"], PERIOD), "prompt_caching"
    )
    assert prefix["projected_monthly_savings"] == 10.52  # the good prefix, undiluted
    assert [t["fingerprint"] for t in prefix["trail"]] == ["fp-good"]


# ---------------------------------------------------------------------------
# Nothing saves more than the bill (design invariant 5)
# ---------------------------------------------------------------------------


def test_a_saving_larger_than_the_bill_is_capped_at_the_bill(tenant_id):
    """Signals are self-reported client state; the invoice is not.

    A double-reported flush, an SDK left pointed at the wrong feature, or a
    prefix summary whose call count outruns what was metered all produce a
    finding bigger than the month it claims to be about. Seeded here: 50,000
    calls with an 8,000-token prefix says $1,080 of savings on a feature the
    connector billed $40 for, and it said it in the headline "Measured
    savings" figure.
    """
    triage = features.add_feature(tenant_id, "AI threat triage")
    hook.ingest_events(
        tenant_id,
        [
            _prefix_event(triage["id"], "fp-huge", 50_000, 8000, 0, 400_000_000,
                          measured=True, windows=500)
        ],
    )
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        _cost_row(conn, tenant_id, triage["id"], amount=40.00,
                  tokens_in=400_000, tokens_out=10_000)

    result = optimize_measured.opportunities(tenant_id, triage["id"], PERIOD)
    prefix = _opp(result, "prompt_caching")
    assert prefix["projected_monthly_savings"] == 40.0
    assert prefix["projected_annual_savings"] == 480.0
    # A number that had to be bounded by the bill is not a counted saving, and
    # the signals behind it are suspect for every other number too.
    assert prefix["savings_type"] == "modeled_ceiling"
    assert prefix["confidence"] == "med"  # down one step from high
    assert "capped at this feature's $40.00 of billed spend" in prefix["evidence"]
    # ...so it lands in the ceiling total, not the guaranteed one.
    assert result["totals"]["measured"] == 0.0


def test_a_saving_within_the_bill_is_left_alone(tenant_id):
    triage = features.add_feature(tenant_id, "AI threat triage")
    hook.ingest_events(
        tenant_id,
        [
            _prefix_event(triage["id"], "fp-p", 1000, 4000, 0, 4_000_000,
                          measured=True, windows=20)
        ],
    )
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        _cost_row(conn, tenant_id, triage["id"], amount=500.00,
                  tokens_in=4_000_000, tokens_out=100_000)

    prefix = _opp(
        optimize_measured.opportunities(tenant_id, triage["id"], PERIOD), "prompt_caching"
    )
    assert prefix["projected_monthly_savings"] == 10.52
    assert prefix["savings_type"] == "measured"
    assert prefix["confidence"] == "high"
    assert "capped at" not in prefix["evidence"]


def test_the_cap_reads_the_bill_on_the_same_reconciled_basis(tenant_id):
    """The bound is only as good as the spend figure behind it.

    Read without the counting rule, a provider running both a connector and the
    SDK looks twice as expensive as it is, and the cap lets through twice what
    the feature actually cost.
    """
    triage = features.add_feature(tenant_id, "AI threat triage")
    hook.ingest_events(
        tenant_id,
        [
            _prefix_event(triage["id"], "fp-huge", 50_000, 8000, 0, 400_000_000,
                          measured=True, windows=500)
        ],
    )
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        _cost_row(conn, tenant_id, triage["id"], amount=40.00, source="cost_api")
        _cost_row(conn, tenant_id, triage["id"], amount=40.00, source="hook")
        _cost_row(conn, tenant_id, triage["id"], amount=900.00, environment="ignore")

    prefix = _opp(
        optimize_measured.opportunities(tenant_id, triage["id"], PERIOD), "prompt_caching"
    )
    assert prefix["projected_monthly_savings"] == 40.0


def test_a_feature_with_no_recorded_spend_is_not_capped_to_nothing(tenant_id):
    """No bill is not a bill of zero.

    Capping to zero where cost data is simply absent would delete real findings
    to punish a gap in a different pipeline.
    """
    triage = features.add_feature(tenant_id, "AI threat triage")
    hook.ingest_events(
        tenant_id,
        [
            _prefix_event(triage["id"], "fp-p", 1000, 4000, 0, 4_000_000,
                          measured=True, windows=20)
        ],
    )

    prefix = _opp(
        optimize_measured.opportunities(tenant_id, triage["id"], PERIOD), "prompt_caching"
    )
    assert prefix["projected_monthly_savings"] == 10.52
    assert prefix["savings_type"] == "measured"


def test_a_prefix_the_provider_will_not_cache_is_not_offered(tenant_id):
    """Haiku needs 4,096 tokens before Anthropic caches anything.

    Meter's own floor is 1,000, which is below every published minimum. A
    1,200-token Haiku prefix cleared it and was offered as $43/mo of savings —
    for a change the provider would have ignored.
    """
    triage = features.add_feature(tenant_id, "AI threat triage")
    hook.ingest_events(
        tenant_id,
        [
            _prefix_event(triage["id"], "fp-haiku", 50_000, 1200, 0, 60_000_000,
                          measured=True, windows=500, model="claude-haiku-4-5")
        ],
    )

    result = optimize_measured.opportunities(tenant_id, triage["id"], PERIOD)
    assert "prompt_caching" not in _measured_levers(result)


def test_the_same_prefix_is_offered_on_a_model_that_would_cache_it(tenant_id):
    """The gate is the model's minimum, not the size of the prefix alone."""
    triage = features.add_feature(tenant_id, "AI threat triage")
    hook.ingest_events(
        tenant_id,
        [
            _prefix_event(triage["id"], "fp-sonnet", 50_000, 1200, 0, 60_000_000,
                          measured=True, windows=500, model="claude-sonnet-4-6")
        ],
    )

    prefix = _opp(
        optimize_measured.opportunities(tenant_id, triage["id"], PERIOD), "prompt_caching"
    )
    assert prefix["projected_monthly_savings"] > 0


def test_a_provider_that_caches_unasked_is_told_something_it_can_act_on(tenant_id):
    """OpenAI and Gemini 2.5+ cache automatically.

    "Set cache_control on the static system block" sends those customers
    looking for a parameter their API does not have. An uncached prefix there
    is a prompt-shape problem, and that is what the fix has to say.
    """
    report = features.add_feature(tenant_id, "Report generator")
    event = _prefix_event(report["id"], "fp-oai", 1000, 4000, 0, 4_000_000,
                          measured=True, windows=20, model="gpt-4o")
    event["provider"] = "openai"
    hook.ingest_events(tenant_id, [event])

    prefix = _opp(
        optimize_measured.opportunities(tenant_id, report["id"], PERIOD), "prompt_caching"
    )
    assert "cache_control" not in prefix["fix"]
    assert "caches automatically" in prefix["fix"]


def test_anthropic_is_still_told_to_mark_the_block(tenant_id):
    triage = features.add_feature(tenant_id, "AI threat triage")
    hook.ingest_events(
        tenant_id,
        [_prefix_event(triage["id"], "fp-p", 1000, 4000, 0, 4_000_000,
                       measured=True, windows=20)],
    )

    prefix = _opp(
        optimize_measured.opportunities(tenant_id, triage["id"], PERIOD), "prompt_caching"
    )
    assert "cache_control" in prefix["fix"]


def test_the_bill_check_reads_spend_the_way_the_rest_of_the_product_does(tenant_id):
    """The gate is only as good as the spend figure behind it.

    Two ways to get that wrong, both of which make a real saving look like a
    rise in unit cost and quietly withhold a verification the customer earned:
    counting a provider's connector rows and its SDK rows as separate money,
    and counting spend the customer marked ignore.
    """
    triage = features.add_feature(tenant_id, "AI threat triage")
    hook.ingest_events(
        tenant_id,
        [_prefix_event(triage["id"], "fp-p", 1000, 4000, 0, 4_000_000, windows=20)],
    )
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        # April: $60 over 10M tokens -> $6 per million.
        conn.execute(
            """
            INSERT INTO inference_cost (tenant_id, feature_id, provider, model, amount,
                                        period, tokens_in, tokens_out, source, confidence)
            VALUES (%s, %s, 'openai', 'gpt-4o', 60.00, %s, 10000000, 0, 'cost_api', 'high')
            """,
            (tenant_id, triage["id"], dt.date(2026, 4, 1)),
        )
        # June: $10 over 10M tokens -> $1 per million. Work got cheaper.
        conn.execute(
            """
            INSERT INTO inference_cost (tenant_id, feature_id, provider, model, amount,
                                        period, tokens_in, tokens_out, source, confidence)
            VALUES (%s, %s, 'openai', 'gpt-4o', 10.00, %s, 10000000, 0, 'cost_api', 'high')
            """,
            (tenant_id, triage["id"], PERIOD),
        )
        # The SDK's description of those same June calls. It carries a request
        # count the connector does not, so counting it would switch June to
        # cost-per-CALL and leave the two periods incomparable.
        conn.execute(
            """
            INSERT INTO inference_cost (tenant_id, feature_id, provider, model, amount,
                                        period, tokens_in, tokens_out, request_count,
                                        source, confidence)
            VALUES (%s, %s, 'openai', 'gpt-4o', 10.00, %s, 10000000, 0, 500,
                    'hook', 'high')
            """,
            (tenant_id, triage["id"], PERIOD),
        )
        # ...and spend excluded from every other total in the product, which
        # would otherwise read as June costing nine times what April did.
        conn.execute(
            """
            INSERT INTO inference_cost (tenant_id, feature_id, provider, model, amount,
                                        period, tokens_in, tokens_out, source, confidence,
                                        environment)
            VALUES (%s, %s, 'openai', 'gpt-4o', 900.00, %s, 1000, 0, 'cost_api', 'high',
                    'ignore')
            """,
            (tenant_id, triage["id"], PERIOD),
        )
    optimize_measured.mark_applied(
        tenant_id, triage["id"], "prompt_caching", 100.0, dt.date(2026, 4, 1)
    )
    action = next(
        a
        for a in optimize_measured.opportunities(tenant_id, triage["id"], PERIOD)["actions"]
        if a["lever"] == "prompt_caching"
    )
    assert action["unit_cost_unit"] == "1M input tokens"
    assert (action["unit_cost_before"], action["unit_cost_now"]) == (6.0, 1.0)
    assert action["status"] == "verified"


# ---------------------------------------------------------------------------
# The directional tier splits dollars, not token counts
# ---------------------------------------------------------------------------


def _directional(result, title):
    return next(
        (o for o in result["opportunities"] if o["title"] == title and o["source"] == "heuristic"),
        None,
    )


def test_the_directional_split_prices_tokens_rather_than_counting_them(tenant_id):
    """An input token and an output token do not cost the same.

    gpt-4o is $2.50 in and $10 out. A month with equal token counts is 20%
    input cost — splitting the bill by COUNT called it 50%, so the caching and
    context estimates were inflated two and a half times and the output-token
    estimate was understated. The two biggest levers move in opposite
    directions, so the error did not cancel out either.
    """
    report = features.add_feature(tenant_id, "Report generator")
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        # 10M in + 10M out on gpt-4o = $25.00 + $100.00 = $125.00 billed.
        conn.execute(
            """
            INSERT INTO inference_cost (tenant_id, feature_id, provider, model, amount,
                                        period, tokens_in, tokens_out, request_count,
                                        source, confidence)
            VALUES (%s, %s, 'openai', 'gpt-4o', 125.00, %s, 10000000, 10000000, 1000,
                    'cost_api', 'high')
            """,
            (tenant_id, report["id"], PERIOD),
        )

    result = optimize_measured.opportunities(tenant_id, report["id"], PERIOD)
    # Priced: input is $25 of the $125, output $100.
    assert _directional(result, "Output token reduction")["projected_monthly_savings"] == 8.0
    assert _directional(result, "Context reduction")["projected_monthly_savings"] == 1.25
    # Counted, the old way, input would have been $62.50 — so output reduction
    # would have read $5.00, context $3.13, and prompt caching would have fired
    # at all, because its gate is a 50% input SHARE it only reached by counting.
    assert _directional(result, "Prompt caching") is None


def test_an_input_heavy_month_is_still_recognised_as_input_heavy(tenant_id):
    """The correction must not simply suppress the input levers."""
    report = features.add_feature(tenant_id, "Report generator")
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        # 10M in, 100k out on gpt-4o = $25.00 + $1.00 = $26.00.
        conn.execute(
            """
            INSERT INTO inference_cost (tenant_id, feature_id, provider, model, amount,
                                        period, tokens_in, tokens_out, request_count,
                                        source, confidence)
            VALUES (%s, %s, 'openai', 'gpt-4o', 26.00, %s, 10000000, 100000, 5000,
                    'cost_api', 'high')
            """,
            (tenant_id, report["id"], PERIOD),
        )

    result = optimize_measured.opportunities(tenant_id, report["id"], PERIOD)
    caching = _directional(result, "Prompt caching")
    # $25 of $26 is input, so this clears the 70% bar for high confidence.
    assert caching is not None and caching["confidence"] == "high"
    assert caching["projected_monthly_savings"] == 3.0  # 12% of $25.00


@pytest.mark.parametrize(
    "model, tokens_in, tokens_out, why",
    [
        # A connector row that reports dollars but no token counts.
        ("gpt-4o", 0, 0, "no counts to price"),
        # A model the price book does not know: real counts, no rates, so the
        # split cannot be computed from them either.
        ("some-unreleased-model", 10_000_000, 10_000_000, "no rates to price with"),
    ],
)
def test_a_split_that_cannot_be_priced_keeps_the_stated_fallback(
    tenant_id, model, tokens_in, tokens_out, why
):
    """70/30 is the documented guess, and it stays a guess.

    It must not become a division by zero, and it must not become a silent
    50/50 from counting tokens nobody can put a price on.
    """
    report = features.add_feature(tenant_id, "Report generator")
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        conn.execute(
            """
            INSERT INTO inference_cost (tenant_id, feature_id, provider, model, amount,
                                        period, tokens_in, tokens_out, request_count,
                                        source, confidence)
            VALUES (%s, %s, 'openai', %s, 100.00, %s, %s, %s, 1000, 'cost_api', 'high')
            """,
            (tenant_id, report["id"], model, PERIOD, tokens_in, tokens_out),
        )

    result = optimize_measured.opportunities(tenant_id, report["id"], PERIOD)
    caching = _directional(result, "Prompt caching")
    assert caching is not None, why
    assert caching["projected_monthly_savings"] == 8.4  # 12% of 70% of $100


def test_each_model_is_split_at_its_own_rates(tenant_id):
    """One feature, two models with opposite cost shapes.

    A single feature-wide ratio would average them into a number that
    describes neither.
    """
    report = features.add_feature(tenant_id, "Report generator")
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        for provider, model, amount, tin, tout in (
            # gpt-4o-mini: $0.15 in / $0.60 out. 10M in, 1M out = $1.50 + $0.60.
            ("openai", "gpt-4o-mini", 2.10, 10_000_000, 1_000_000),
            # claude-opus-4-8: $5 in / $25 out. 1M in, 10M out = $5 + $250.
            ("anthropic", "claude-opus-4-8", 255.00, 1_000_000, 10_000_000),
        ):
            conn.execute(
                """
                INSERT INTO inference_cost (tenant_id, feature_id, provider, model, amount,
                                            period, tokens_in, tokens_out, request_count,
                                            source, confidence)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 500, 'cost_api', 'high')
                """,
                (tenant_id, report["id"], provider, model, amount, PERIOD, tin, tout),
            )

    result = optimize_measured.opportunities(tenant_id, report["id"], PERIOD)
    # Input cost is $1.50 of the mini row and $5.00 of the opus row = $6.50 of
    # $257.10. Output is $250.60, so output reduction is 8% of that.
    out = _directional(result, "Output token reduction")
    assert out is not None and out["projected_monthly_savings"] == 20.05


def test_prefix_telemetry_does_not_vouch_for_the_duplicate_lever(tenant_id):
    """Two levers, two kinds of evidence, and they are not interchangeable.

    "Is anything looking?" is asked so that a lever finding nothing can be told
    apart from a lever nobody is running. Answering it with signals of the
    WRONG kind lets an applied dedup action reconcile against a silence it has
    mistaken for a measurement — and, two periods on, call that verified.
    """
    triage = features.add_feature(tenant_id, "AI threat triage")
    # Prefix telemetry only: optimize mode is on, but nothing is comparing
    # whole requests for this feature. Written straight to the table with a v2
    # stamp, because the question is what the DETECTOR does with a row the
    # schema allows — today's SDK leaves prefix summaries unversioned, which is
    # the only reason reading "any v2 row" has not already gone wrong.
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        conn.execute(
            """
            INSERT INTO usage_signal (tenant_id, feature_id, provider, model, period,
                                      signal_kind, fingerprint, call_count, prefix_tokens,
                                      tokens_in, tokens_out, cached_count, prefix_measured,
                                      fingerprint_version, cache_windows)
            VALUES (%s, %s, 'anthropic', 'claude-sonnet-4-6', %s, 'prefix', 'fp-p',
                    1000, 4000, 4000000, 0, 0, true, 'v2', 20)
            """,
            (tenant_id, triage["id"], PERIOD),
        )
    optimize_measured.mark_applied(
        tenant_id, triage["id"], "duplicate_calls", 100.0, dt.date(2026, 4, 1)
    )
    action = next(
        a
        for a in optimize_measured.opportunities(tenant_id, triage["id"], PERIOD)["actions"]
        if a["lever"] == "duplicate_calls"
    )
    assert action["status"] == "unverifiable"
    assert action["realized_monthly"] is None
    assert action["current_avoidable"] is None


# ---------------------------------------------------------------------------
# A prefix is worth its average size, not its largest (0063)
# ---------------------------------------------------------------------------


def test_a_prefix_is_priced_at_the_average_the_provider_reported(tenant_id):
    """Three creations of 3k, 4k and 5k tokens is a 4k prefix, not a 5k one.

    What a provider caches varies between calls that share a static block: the
    breakpoint moves and the conversation in front of it grows. Taking the
    month's high-water mark and multiplying it by every uncached call values
    the whole group at its most expensive member, and the bias only ever points
    one way.
    """
    triage = features.add_feature(tenant_id, "AI threat triage")
    hook.ingest_events(
        tenant_id,
        [
            # Sum 12,000 over 3 samples -> a 4,000-token mean.
            _prefix_event(triage["id"], "fp-p", 1000, 4000, 0, 4_000_000,
                          measured=True, windows=20, samples=3)
        ],
    )

    prefix = _opp(
        optimize_measured.opportunities(tenant_id, triage["id"], PERIOD), "prompt_caching"
    )
    # The same $10.52 as a single 4,000-token measurement. At the old maximum
    # of 5,000 it would have been $13.15.
    assert prefix["projected_monthly_savings"] == 10.52
    assert "4,000-token static prefix" in prefix["evidence"]
    assert prefix["trail"][0]["measured_samples"] == 3


def test_measurements_from_several_processes_average_rather_than_compete(tenant_id):
    """Replicas each report what they saw. The fold has to keep a mean.

    A max survives being folded again — max(max) is a max — but a mean does
    not, which is why the sum and the count travel rather than the average.
    """
    triage = features.add_feature(tenant_id, "AI threat triage")
    # Two separate deliveries, which is what replicas actually do: the second
    # updates the row the first inserted, so the fold has to be right in the
    # UPDATE path as well as in the batch accumulator.
    hook.ingest_events(
        tenant_id,
        [_prefix_event(triage["id"], "fp-p", 500, 2000, 0, 1_000_000,
                       measured=True, windows=10, samples=1)],
    )
    hook.ingest_events(
        tenant_id,
        [_prefix_event(triage["id"], "fp-p", 500, 6000, 0, 1_000_000,
                       measured=True, windows=10, samples=1)],
    )

    prefix = _opp(
        optimize_measured.opportunities(tenant_id, triage["id"], PERIOD), "prompt_caching"
    )
    # (2,000 + 6,000) / 2 = 4,000. The old GREATEST would have said 6,000.
    assert "4,000-token static prefix" in prefix["evidence"]
    assert prefix["trail"][0]["measured_samples"] == 2


def test_a_character_estimate_can_no_longer_outrank_a_provider_count(tenant_id):
    """One process saw a cache creation; another never did.

    They shared a column folded with GREATEST, which cannot tell a character
    count from a token count. The estimate won whenever it was larger, and
    prefix_measured — which latches on — then labelled the row as the
    provider's own number. That is the failure 0056 exists to prevent,
    reintroduced one layer up.
    """
    triage = features.add_feature(tenant_id, "AI threat triage")
    hook.ingest_events(
        tenant_id,
        [
            # An estimate of 5,000 characters-over-four, no measurement.
            _prefix_event(triage["id"], "fp-p", 500, 5000, 0, 1_000_000, windows=10),
            # The provider's actual count, which is smaller.
            _prefix_event(triage["id"], "fp-p", 500, 3000, 0, 1_000_000,
                          measured=True, windows=10),
        ],
    )

    prefix = _opp(
        optimize_measured.opportunities(tenant_id, triage["id"], PERIOD), "prompt_caching"
    )
    # 3,000 — the measurement — not 5,000, and it is honestly called measured.
    assert "3,000-token static prefix" in prefix["evidence"]
    assert "measured by the provider" in prefix["evidence"]
    assert prefix["confidence"] == "high"


def test_a_row_written_before_the_mean_existed_is_read_as_an_estimate(tenant_id):
    """Legacy rows carry a maximum with no way left to tell which kind it is.

    They are not deleted — they are real history and they back applied actions
    — but a number that might be either a measurement or a character count is
    only safely read as the weaker of the two.
    """
    triage = features.add_feature(tenant_id, "AI threat triage")
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        conn.execute(
            """
            INSERT INTO usage_signal (tenant_id, feature_id, provider, model, period,
                                      signal_kind, fingerprint, call_count, prefix_tokens,
                                      tokens_in, tokens_out, cached_count, prefix_measured,
                                      cache_windows)
            VALUES (%s, %s, 'anthropic', 'claude-sonnet-4-6', %s, 'prefix', 'fp-old',
                    1000, 4000, 4000000, 0, 0, true, 20)
            """,
            (tenant_id, triage["id"], PERIOD),
        )

    prefix = _opp(
        optimize_measured.opportunities(tenant_id, triage["id"], PERIOD), "prompt_caching"
    )
    # The size is still used — the finding is real — but it is no longer
    # presented as something the provider counted.
    assert "4,000-token static prefix" in prefix["evidence"]
    assert "estimated from request length" in prefix["evidence"]
    assert prefix["confidence"] == "med"
    assert prefix["savings_type"] == "modeled_ceiling"
    assert prefix["trail"][0]["measured_samples"] == 0
