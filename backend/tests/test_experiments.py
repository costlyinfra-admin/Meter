"""Test this (EX-1): content-free simulations, and what their results do.

A test replaces Meter's assumption with the customer's — how old a cached answer
may be, how long a prompt cache lives — and the recommendation is then priced
under it. These check the arithmetic, the three outcomes, and the rules for how
an outcome changes what the customer sees (docs/experiments-spec.md §7).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from meter import experiments, features, hook, optimize_measured, pricing
from meter.db import app_dsn, connect, tenant_tx

PERIOD = dt.date(2026, 6, 1)
WHEN = "2026-06-15T10:00:00Z"
MODEL = "claude-sonnet-4-6"  # $3 / $15 per million
ACTOR = "cto@acme.com"


def _call(feature_id, tokens_in, signal=None):
    event = {
        "provider": "anthropic",
        "model": MODEL,
        "tokens_in": tokens_in,
        "tokens_out": 0,
        "feature_id": feature_id,
        "occurred_at": WHEN,
    }
    if signal:
        event["signal"] = signal
    return event


def _repeat(feature_id, fingerprint="fp-a"):
    """One repeated request at the detector's 10-minute window: 1M input, $3."""
    return _call(
        feature_id,
        1_000_000,
        {
            "kind": "duplicate",
            "fingerprint": fingerprint,
            "count": 1,
            "fingerprint_version": "v2",
            "scope_kind": "explicit",
        },
    )


@pytest.fixture
def triage(tenant_id):
    """A feature that spent $306 in June, $6 of it on two repeated requests the
    detector found at its own 10-minute window."""
    feature = features.add_feature(tenant_id, "AI threat triage")
    hook.ingest_events(
        tenant_id,
        [_call(feature["id"], 100_000_000), _repeat(feature["id"]), _repeat(feature["id"])],
    )
    return feature["id"]


def _simulation(tenant_id, feature_id, scope="explicit", **counts):
    entry = dict.fromkeys(hook.SIMULATION_FIELDS, 0)
    entry.update(counts)
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        hook.upsert_simulation(
            conn, tenant_id, (feature_id, "anthropic", MODEL, PERIOD, scope), entry
        )


def _opp(tenant_id, feature_id, lever):
    result = optimize_measured.opportunities(tenant_id, feature_id, PERIOD)
    return next((o for o in result["opportunities"] if o["lever"] == lever), None), result


# ---------------------------------------------------------------------------
# Repeated requests
# ---------------------------------------------------------------------------
def test_a_freshness_limit_prices_the_calls_a_cache_that_old_would_serve(tenant_id, triage):
    # 400 calls a 1-hour cache would have served, 10M input tokens between them.
    _simulation(
        tenant_id,
        triage,
        calls=1000,
        hits_10m=200,
        hit_tokens_in_10m=2_000_000,
        hits_1h=400,
        hit_tokens_in_1h=10_000_000,
    )

    exp = experiments.create(
        tenant_id, triage, "duplicate_calls", ACTOR, ttl_seconds=3600, scoped_only=False
    )

    assert (exp["status"], exp["outcome"]) == ("completed", "passed")
    assert exp["result"]["monthly_saving"] == 30.0  # 10M x $3/M
    assert exp["result"]["hits"] == 400 and exp["result"]["hit_rate"] == 0.4
    # The whole ladder, so the trade-off is visible, not just the choice.
    assert [s["ttl_seconds"] for s in exp["result"]["ladder"]] == [60, 600, 3600, 86400]
    assert exp["result"]["ladder"][1]["monthly_saving"] == 6.0
    # What the recommendation said when the test began, frozen.
    assert exp["baseline"] == {
        "monthly": 6.0,
        "savings_type": "modeled_ceiling",
        "confidence": "med",
    }


def test_a_passing_test_replaces_the_figure_and_says_where_it_came_from(tenant_id, triage):
    _simulation(tenant_id, triage, calls=1000, hits_1h=400, hit_tokens_in_1h=10_000_000)
    experiments.create(
        tenant_id, triage, "duplicate_calls", ACTOR, ttl_seconds=3600, scoped_only=False
    )

    opp, _ = _opp(tenant_id, triage, "duplicate_calls")
    assert opp["projected_monthly_savings"] == 30.0
    assert opp["projected_annual_savings"] == 360.0
    assert opp["validation"] == "simulated"
    assert opp["evidence"].startswith("Simulated with answers reused for up to 1 hour")
    assert "up to 1 hour" in opp["fix"]
    # Simulated is not tested: the type does not change (spec §7.2).
    assert opp["savings_type"] == "modeled_ceiling"
    assert opp["priority_score"] == optimize_measured._priority(
        30.0, opp["confidence"], opp["engineering_effort"]
    )


def test_a_failing_test_keeps_the_card_but_leaves_the_totals_and_the_ranking(tenant_id, triage):
    _simulation(tenant_id, triage, calls=1000, hits_1m=1, hit_tokens_in_1m=1000)
    exp = experiments.create(
        tenant_id, triage, "duplicate_calls", ACTOR, ttl_seconds=60, scoped_only=False
    )
    assert exp["outcome"] == "failed"
    assert "not worth building" in exp["outcome_reason"]

    opp, result = _opp(tenant_id, triage, "duplicate_calls")
    assert opp is not None, "a failed test is evidence, not deletion"
    assert (opp["validation"], opp["test_failed"]) == ("failed", True)
    # The feature has another ceiling (model right-sizing); the total is that
    # alone, without the failed card's figure.
    others = round(
        sum(
            o["projected_monthly_savings"]
            for o in result["opportunities"]
            if o["savings_type"] == "modeled_ceiling"
            and o["lever"] != "duplicate_calls"
            and o["overlaps"] is None
        ),
        2,
    )
    assert others > 0
    assert result["totals"]["modeled_ceiling"] == others

    overview = optimize_measured.copilot_overview(tenant_id, PERIOD)
    assert not [o for o in overview["top_recommendations"] if o["lever"] == "duplicate_calls"]
    assert overview["totals"]["modeled_ceiling"] == others
    assert "duplicate_calls" not in {e["lever"] for e in overview["by_lever"]}


def test_too_few_calls_waits_and_changes_nothing_until_there_are_enough(tenant_id, triage):
    _simulation(tenant_id, triage, calls=50, hits_1h=40, hit_tokens_in_1h=10_000_000)
    exp = experiments.create(
        tenant_id, triage, "duplicate_calls", ACTOR, ttl_seconds=3600, scoped_only=False
    )
    assert (exp["status"], exp["outcome"]) == ("waiting_for_data", None)
    assert "50 of the 200 calls" in exp["outcome_reason"]

    opp, _ = _opp(tenant_id, triage, "duplicate_calls")
    assert (opp["validation"], opp["projected_monthly_savings"]) == ("untested", 6.0)
    assert opp["experiment"]["status"] == "waiting_for_data"

    # More traffic arrives; looking at the test again finishes it.
    _simulation(tenant_id, triage, calls=500)
    again = experiments.get(tenant_id, exp["id"])
    assert (again["status"], again["outcome"]) == ("completed", "passed")


def test_only_scoped_repeats_count_when_the_customer_asks_for_that(tenant_id, triage):
    _simulation(
        tenant_id, triage, scope="explicit", calls=600, hits_1h=100, hit_tokens_in_1h=1_000_000
    )
    _simulation(
        tenant_id, triage, scope="unscoped", calls=600, hits_1h=500, hit_tokens_in_1h=9_000_000
    )

    everyone = experiments.create(
        tenant_id, triage, "duplicate_calls", ACTOR, ttl_seconds=3600, scoped_only=False
    )
    scoped = experiments.create(
        tenant_id, triage, "duplicate_calls", ACTOR, ttl_seconds=3600, scoped_only=True
    )
    assert everyone["result"]["monthly_saving"] == 30.0
    assert "safely be shared is not known" in everyone["outcome_reason"]
    assert scoped["result"]["monthly_saving"] == 3.0
    # Every call is still in the denominator: scoping changes what may be
    # served, not how much traffic there was.
    assert scoped["result"]["calls"] == 1200


def test_a_limit_the_sdk_could_not_fully_remember_is_a_minimum(tenant_id, triage):
    _simulation(
        tenant_id, triage, calls=1000, hits_24h=500, hit_tokens_in_24h=10_000_000, evicted_24h=3
    )
    exp = experiments.create(
        tenant_id, triage, "duplicate_calls", ACTOR, ttl_seconds=86400, scoped_only=False
    )
    assert exp["result"]["ladder"][3]["lower_bound"] is True
    assert exp["result"]["ladder"][2]["lower_bound"] is False
    assert "That is a minimum" in exp["outcome_reason"]


def test_a_tested_figure_is_still_held_to_the_bill(tenant_id, triage):
    """Invariant 5 applies to a simulated saving like any other."""
    _simulation(tenant_id, triage, calls=1000, hits_24h=900, hit_tokens_in_24h=400_000_000)
    experiments.create(
        tenant_id, triage, "duplicate_calls", ACTOR, ttl_seconds=86400, scoped_only=False
    )
    opp, _ = _opp(tenant_id, triage, "duplicate_calls")
    assert opp["projected_monthly_savings"] == 306.0  # the feature's whole bill
    assert "capped at this feature's" in opp["evidence"]


def test_an_applied_fix_is_still_reconciled_on_the_basis_it_was_applied_on(tenant_id, triage):
    """A test must not move the yardstick under a fix already in place."""
    optimize_measured.mark_applied(tenant_id, triage, "duplicate_calls", 100.0, dt.date(2026, 5, 1))
    _simulation(tenant_id, triage, calls=1000, hits_1h=400, hit_tokens_in_1h=10_000_000)
    experiments.create(
        tenant_id, triage, "duplicate_calls", ACTOR, ttl_seconds=3600, scoped_only=False
    )

    _opp_, result = _opp(tenant_id, triage, "duplicate_calls")
    action = next(a for a in result["actions"] if a["lever"] == "duplicate_calls")
    assert action["current_avoidable"] == 6.0  # the detector's figure, not the test's $30


def test_a_new_test_replaces_one_still_waiting(tenant_id, triage):
    first = experiments.create(
        tenant_id, triage, "duplicate_calls", ACTOR, ttl_seconds=600, scoped_only=False
    )
    assert first["status"] == "waiting_for_data"
    second = experiments.create(
        tenant_id, triage, "duplicate_calls", ACTOR, ttl_seconds=3600, scoped_only=False
    )
    assert experiments.get(tenant_id, first["id"])["status"] == "cancelled"
    assert second["status"] == "waiting_for_data"
    assert [h["id"] for h in experiments.get(tenant_id, second["id"])["history"]] == [first["id"]]


def test_a_finished_test_cannot_be_cancelled_away(tenant_id, triage):
    _simulation(tenant_id, triage, calls=1000, hits_1h=400, hit_tokens_in_1h=10_000_000)
    exp = experiments.create(
        tenant_id, triage, "duplicate_calls", ACTOR, ttl_seconds=3600, scoped_only=False
    )
    assert experiments.cancel(tenant_id, exp["id"])["status"] == "completed"


@pytest.mark.parametrize(
    "lever, setting, message",
    [
        ("provider_switch", {}, "cannot be tested yet"),
        ("duplicate_calls", {}, "how old a cached answer may be"),
        ("duplicate_calls", {"ttl_seconds": 120}, "how old a cached answer may be"),
        ("duplicate_calls", {"ttl_seconds": 600, "cache_ttl": "1h"}, "prompt caching only"),
        ("prompt_caching", {"cache_ttl": "2h"}, "Choose a cache lifetime"),
        ("prompt_caching", {"cache_ttl": "1h", "ttl_seconds": 600}, "repeated requests only"),
    ],
)
def test_a_setting_the_lever_does_not_offer_is_refused_in_words(
    tenant_id, triage, lever, setting, message
):
    with pytest.raises(experiments.ExperimentError, match=message):
        experiments.create(tenant_id, triage, lever, ACTOR, **setting)


def test_another_tenant_cannot_see_a_test(tenant_id, triage, app_env):
    exp = experiments.create(
        tenant_id, triage, "duplicate_calls", ACTOR, ttl_seconds=600, scoped_only=False
    )
    other = str(
        app_env.execute("INSERT INTO tenant (name) VALUES ('Other') RETURNING id").fetchone()[0]
    )
    app_env.commit()
    assert experiments.get(other, exp["id"]) is None
    assert experiments.cancel(other, exp["id"]) is None


def test_a_malformed_id_is_not_found_rather_than_an_error(tenant_id):
    assert experiments.get(tenant_id, "not-a-uuid") is None


# ---------------------------------------------------------------------------
# Prompt caching: which cache lifetime
# ---------------------------------------------------------------------------
def _prefix(feature_id, windows_5m, windows_1h, provider="anthropic", model=MODEL):
    """1,000 uncached calls sharing a 5,000-token prefix the provider measured."""
    signal = {
        "kind": "prefix",
        "fingerprint": f"pfx-{provider}",
        "count": 1000,
        "cached_count": 0,
        "prefix_tokens": 5000,
        "prefix_tokens_sum": 5_000_000,
        "prefix_tokens_n": 1000,
        "prefix_measured": True,
        "tokens_in": 5_000_000,
        "tokens_out": 0,
        "write_calls": 0,
        "cache_windows": windows_5m,
    }
    if windows_1h is not None:
        signal["cache_windows_1h"] = windows_1h
    return {
        "provider": provider,
        "model": model,
        "tokens_in": 0,
        "tokens_out": 0,
        "feature_id": feature_id,
        "occurred_at": WHEN,
        "signal": signal,
    }


def _caching_saving(windows: int, ttl: str) -> float:
    rate = pricing.rate_in(MODEL, "anthropic")
    read = pricing.cache_read_mult("anthropic", MODEL)
    write = pricing.cache_write_mult("anthropic", ttl)
    # Rounded the way the detector rounds: through a float.
    return round(float(Decimal(5000) * rate * (1000 * (1 - read) - windows * (write - read))), 2)


def test_the_two_cache_lifetimes_are_priced_against_each_other(tenant_id, triage):
    hook.ingest_events(tenant_id, [_prefix(triage, windows_5m=300, windows_1h=50)])
    exp = experiments.create(tenant_id, triage, "prompt_caching", ACTOR, cache_ttl="1h")

    by_ttl = {s["cache_ttl"]: s for s in exp["result"]["lifetimes"]}
    assert by_ttl["5m"]["monthly_saving"] == _caching_saving(300, "5m")
    assert by_ttl["1h"]["monthly_saving"] == _caching_saving(50, "1h")
    assert by_ttl["1h"]["cache_writes"] == 50
    assert exp["outcome"] == "passed"
    assert by_ttl["1h"]["monthly_saving"] > by_ttl["5m"]["monthly_saving"]

    opp, _ = _opp(tenant_id, triage, "prompt_caching")
    assert opp["projected_monthly_savings"] == _caching_saving(50, "1h")
    assert 'ttl "1h"' in opp["fix"]
    # Priced exactly as the detector prices a lifetime, so its measured/ceiling
    # rule carries over: both sides were counted, the size was measured.
    assert (opp["savings_type"], opp["confidence"]) == ("measured", "high")


def test_a_lifetime_whose_writes_cost_more_than_its_reads_save_fails(tenant_id, triage):
    hook.ingest_events(tenant_id, [_prefix(triage, windows_5m=300, windows_1h=600)])
    exp = experiments.create(tenant_id, triage, "prompt_caching", ACTOR, cache_ttl="1h")
    assert exp["outcome"] == "failed"
    assert "too far apart" in exp["outcome_reason"]


def test_an_older_sdk_cannot_answer_for_the_one_hour_cache(tenant_id, triage):
    hook.ingest_events(tenant_id, [_prefix(triage, windows_5m=300, windows_1h=None)])
    exp = experiments.create(tenant_id, triage, "prompt_caching", ACTOR, cache_ttl="1h")
    assert exp["status"] == "waiting_for_data"
    assert "too old to count 1-hour cache writes" in exp["outcome_reason"]
    # The default lifetime needs no new count, so it can still be tested.
    assert (
        experiments.create(tenant_id, triage, "prompt_caching", ACTOR, cache_ttl="5m")["outcome"]
        == "passed"
    )


def test_caching_is_not_offered_for_testing_where_there_is_no_lifetime_to_choose(tenant_id):
    feature = features.add_feature(tenant_id, "Summarizer")["id"]
    hook.ingest_events(
        tenant_id,
        [
            _call(feature, 100_000_000),
            _prefix(feature, windows_5m=10, windows_1h=5, provider="openai", model="gpt-4o"),
        ],
    )
    opp, _ = _opp(tenant_id, feature, "prompt_caching")
    assert opp is not None and opp["testable"] is False
