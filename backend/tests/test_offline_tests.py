"""Offline tests (EX-2): a cheaper model, tested on the customer's own cases.

The run happens on the customer's machine; these tests stand in for it by
posting the numbers a run would post. They check the rule, the arithmetic,
what a result does to the recommendation, and the credential's limits.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from decimal import Decimal

import pytest
from meter import experiments, features, hook, offline_tests, optimize_measured, pricing
from meter.db import app_dsn, connect, tenant_tx

PERIOD = dt.date(2026, 6, 1)
WHEN = "2026-06-15T10:00:00Z"
SONNET, HAIKU = "claude-sonnet-4-6", "claude-haiku-4-5"
ACTOR = "cto@acme.com"


def _spend(feature_id, model, tokens_in, provider="anthropic"):
    return {
        "provider": provider,
        "model": model,
        "tokens_in": tokens_in,
        "tokens_out": 0,
        "feature_id": feature_id,
        "occurred_at": WHEN,
    }


@pytest.fixture
def triage(tenant_id):
    """$300 of Sonnet in June: 100M input tokens."""
    feature = features.add_feature(tenant_id, "AI threat triage")
    hook.ingest_events(tenant_id, [_spend(feature["id"], SONNET, 100_000_000)])
    return feature["id"]


def _start(tenant_id, feature_id, **over):
    kw = {"control_model": SONNET, "candidate_model": HAIKU}
    kw.update(over)
    return experiments.create(tenant_id, feature_id, "model_rightsizing", ACTOR, **kw)


def _case(
    i, verdict="same", control=(1000, 200, 900, False), candidate=(1000, 200, 400, False), checks=0
):
    def call(t):
        return {"tokens_in": t[0], "tokens_out": t[1], "latency_ms": t[2], "error": t[3]}

    return {
        "case": hashlib.sha256(f"case-{i}".encode()).hexdigest(),
        "control": call(control),
        "candidate": call(candidate),
        "check_failures": checks,
        "verdict": verdict,
    }


def _payload(cases, **over):
    body = {
        "source": "runner",
        "runner_version": "2.5.0",
        "judge_model": SONNET,
        "judge_usage": None,
        "cases": cases,
    }
    body.update(over)
    return body


def _cases(better=0, same=0, worse=0, **kw):
    verdicts = ["better"] * better + ["same"] * same + ["worse"] * worse
    return [_case(i, v, **kw) for i, v in enumerate(verdicts)]


def _submit(tenant_id, exp, cases, **over):
    return offline_tests.submit(tenant_id, exp["id"], _payload(cases, **over))


def _token_row(tenant_id, experiment_id):
    """(used_at,) for the run's token, or None once it has been revoked."""
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        return conn.execute(
            "SELECT used_at FROM experiment_token WHERE experiment_id = %s", (experiment_id,)
        ).fetchone()


def _opp(tenant_id, feature_id, lever="model_rightsizing"):
    result = optimize_measured.opportunities(tenant_id, feature_id, PERIOD)
    return next((o for o in result["opportunities"] if o["lever"] == lever), None), result


# ---------------------------------------------------------------------------
# What can be tested
# ---------------------------------------------------------------------------
def test_the_options_are_the_feature_s_own_models_and_cheaper_ones_from_the_same_vendor(
    tenant_id, triage
):
    hook.ingest_events(tenant_id, [_spend(triage, "gemini-2.5-pro", 10_000_000, "google")])
    opts = offline_tests.options(tenant_id, triage)
    assert [c["model"] for c in opts["controls"]] == [SONNET]  # Gemini: no runner yet
    sonnet = opts["controls"][0]
    assert sonnet["default_candidate"] == HAIKU  # the recommendation's own target
    assert all(pricing._vendor_of(c["model"]) == "anthropic" for c in sonnet["candidates"])
    assert opts["rule"]["loss_margin"] == 0.10 and opts["rule"]["min_cases"] == 20


@pytest.mark.parametrize(
    "over, message",
    [
        ({"control_model": "claude-opus-4-8"}, "used this month"),
        ({"candidate_model": "gpt-4o-mini"}, "cheaper model from the same provider"),
        ({"candidate_model": "claude-opus-4-8"}, "cheaper model from the same provider"),
        ({"loss_margin": 0.6}, "between 0% and 50%"),
        ({"min_cases": 5}, "between 10 and 500"),
        ({"ttl_seconds": 600}, "does not apply here"),
    ],
)
def test_a_setting_outside_what_can_be_tested_is_refused_in_words(tenant_id, triage, over, message):
    with pytest.raises(experiments.ExperimentError, match=message):
        _start(tenant_id, triage, **over)


# ---------------------------------------------------------------------------
# The run's credential
# ---------------------------------------------------------------------------
def test_starting_a_test_hands_back_its_token_once(tenant_id, triage):
    exp = _start(tenant_id, triage)
    assert exp["status"] == "waiting_for_results"
    assert exp["token"].startswith("mtx_")
    assert offline_tests.resolve_token(exp["token"]) == (tenant_id, exp["id"])
    # Never again: reading the test does not show it.
    assert "token" not in experiments.get(tenant_id, exp["id"])
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        stored = conn.execute("SELECT token_hash FROM experiment_token").fetchone()[0]
    assert exp["token"] not in stored


def test_the_spec_says_what_to_run_and_nothing_about_the_business(tenant_id, triage):
    exp = _start(tenant_id, triage)
    spec = offline_tests.spec(tenant_id, exp["id"])
    assert (spec["control_model"], spec["candidate_model"], spec["provider"]) == (
        SONNET,
        HAIKU,
        "anthropic",
    )
    assert spec["rule"] == {"loss_margin": 0.1, "min_cases": 20}
    assert spec["judge"]["protocol"] == "both_orders"
    assert set(spec) == {
        "experiment_id",
        "lever",
        "provider",
        "control_model",
        "candidate_model",
        "rule",
        "max_cases",
        "checks",
        "judge",
    }


def test_results_spend_the_token(tenant_id, triage):
    exp = _start(tenant_id, triage)
    assert _submit(tenant_id, exp, _cases(same=25))["status"] == "completed"
    # Spent in its own right, not only because the test is now finished.
    assert _token_row(tenant_id, exp["id"])[0] is not None
    assert offline_tests.resolve_token(exp["token"]) is None
    assert _submit(tenant_id, exp, _cases(same=25)) is None


def test_cancelling_a_test_revokes_its_token(tenant_id, triage):
    exp = _start(tenant_id, triage)
    experiments.cancel(tenant_id, exp["id"])
    assert _token_row(tenant_id, exp["id"]) is None  # deleted, not merely unusable
    assert offline_tests.resolve_token(exp["token"]) is None


def test_a_new_test_revokes_the_old_one_s_token(tenant_id, triage):
    first = _start(tenant_id, triage)
    _start(tenant_id, triage, candidate_model="claude-sonnet-5")
    assert _token_row(tenant_id, first["id"]) is None
    assert offline_tests.resolve_token(first["token"]) is None
    assert experiments.get(tenant_id, first["id"])["status"] == "cancelled"


def test_a_case_may_appear_only_once(tenant_id, triage):
    exp = _start(tenant_id, triage)
    cases = _cases(same=25)
    cases[1]["case"] = cases[0]["case"]
    with pytest.raises(offline_tests.OfflineTestError, match="only once"):
        _submit(tenant_id, exp, cases)


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------
def test_a_cheaper_model_that_holds_up_passes_and_is_priced_by_meter(tenant_id, triage):
    exp = _start(tenant_id, triage)
    done = _submit(tenant_id, exp, _cases(better=5, same=18, worse=2))
    assert done["outcome"] == "passed", done["outcome_reason"]
    r = done["result"]
    control = pricing.price(SONNET, 25_000, 5_000, "anthropic") / 25
    candidate = pricing.price(HAIKU, 25_000, 5_000, "anthropic") / 25
    assert r["control"]["cost_per_call"] == float(control.quantize(Decimal("0.000001")))
    assert r["candidate"]["cost_per_call"] == float(candidate.quantize(Decimal("0.000001")))
    fraction = float((control - candidate) / control)
    assert r["saving_fraction"] == pytest.approx(fraction)
    # Applied to what the feature actually spent on Sonnet this month.
    assert r["monthly_saving"] == round(300.0 * fraction, 2)
    assert (r["better"], r["same"], r["worse"], r["broken"]) == (5, 18, 2, 0)
    assert r["candidate"]["latency_ms"] == {"p50": 400, "p95": 400}


def test_one_broken_answer_fails_the_test(tenant_id, triage):
    exp = _start(tenant_id, triage)
    cases = _cases(better=5, same=20)
    cases[3] = _case(3, "same", checks=1)
    assert _submit(tenant_id, exp, cases)["outcome"] == "failed"


def test_a_failed_call_from_the_candidate_is_a_broken_answer(tenant_id, triage):
    exp = _start(tenant_id, triage)
    cases = _cases(same=25)
    cases[0] = _case(0, "unjudged", candidate=(1000, 0, 100, True))
    done = _submit(tenant_id, exp, cases)
    assert done["outcome"] == "failed"
    assert "came back broken" in done["outcome_reason"]


def test_losing_more_than_the_allowance_fails(tenant_id, triage):
    exp = _start(tenant_id, triage)
    done = _submit(tenant_id, exp, _cases(better=5, same=10, worse=10))
    assert done["outcome"] == "failed"
    assert "more losses than the rule allows" in done["outcome_reason"]


def test_too_few_cases_is_inconclusive(tenant_id, triage):
    exp = _start(tenant_id, triage)
    done = _submit(tenant_id, exp, _cases(same=15))
    assert done["outcome"] == "inconclusive"
    assert "this test needs 20" in done["outcome_reason"]


def test_a_case_the_current_model_failed_is_left_out_not_counted(tenant_id, triage):
    exp = _start(tenant_id, triage)
    cases = _cases(same=20) + [_case(99, "unjudged", control=(1000, 0, 100, True))]
    done = _submit(tenant_id, exp, cases)
    assert done["outcome"] == "passed"
    assert (done["result"]["compared"], done["result"]["control_errors"]) == (20, 1)


def test_a_model_that_is_not_cheaper_on_these_cases_fails(tenant_id, triage):
    exp = _start(tenant_id, triage)
    # Haiku writing ten times as much as Sonnet on every case.
    done = _submit(tenant_id, exp, _cases(same=25, candidate=(1000, 20_000, 400, False)))
    assert done["outcome"] == "failed"
    assert "did not cost less per call" in done["outcome_reason"]


def test_a_loosened_rule_is_allowed_and_said_out_loud(tenant_id, triage):
    exp = _start(tenant_id, triage, loss_margin=0.25, min_cases=10)
    assert "under a loosened rule" in exp["setting_label"]
    done = _submit(tenant_id, exp, _cases(better=2, same=7, worse=3))
    assert done["outcome"] == "passed"
    assert "Decided under a loosened rule" in done["outcome_reason"]
    assert done["result"]["rule"] == {"loss_margin": 0.25, "min_cases": 10, "relaxed": True}


def test_what_the_run_cost_is_priced_by_meter_judge_included(tenant_id, triage):
    exp = _start(tenant_id, triage)
    done = _submit(
        tenant_id,
        exp,
        _cases(same=25),
        judge_usage={"provider": "anthropic", "tokens_in": 100_000, "tokens_out": 1_000},
    )
    expected = (
        pricing.price(SONNET, 1000, 200, "anthropic") * 25
        + pricing.price(HAIKU, 1000, 200, "anthropic") * 25
        + pricing.price(SONNET, 100_000, 1_000, "anthropic")
    )
    assert done["test_cost"] == float(expected)
    assert (done["results_source"], done["judge_model"]) == ("runner", SONNET)


# ---------------------------------------------------------------------------
# What a result does to the recommendation
# ---------------------------------------------------------------------------
def test_a_pass_makes_the_saving_tested_and_counts_it_on_its_own(tenant_id, triage):
    before, _ = _opp(tenant_id, triage)
    assert before["savings_type"] == "modeled_ceiling" and before["testable"] is True
    exp = _start(tenant_id, triage)
    done = _submit(tenant_id, exp, _cases(better=5, same=20))

    opp, result = _opp(tenant_id, triage)
    assert opp["savings_type"] == "tested"
    assert opp["validation"] == "tested_offline"
    assert opp["confidence"] == "med"  # a sample is not the traffic
    assert opp["projected_monthly_savings"] == done["result"]["monthly_saving"]
    assert opp["evidence"].startswith("Tested on 25 of your own cases")
    assert result["totals"]["tested"] == opp["projected_monthly_savings"]
    assert result["totals"]["modeled_ceiling"] == 0.0

    overview = optimize_measured.copilot_overview(tenant_id, PERIOD)
    assert overview["totals"]["tested"] == opp["projected_monthly_savings"]


def test_a_failed_test_keeps_the_card_out_of_every_total(tenant_id, triage):
    exp = _start(tenant_id, triage)
    _submit(tenant_id, exp, _cases(better=2, same=10, worse=13))
    opp, result = _opp(tenant_id, triage)
    assert (opp["validation"], opp["test_failed"]) == ("failed", True)
    assert result["totals"]["modeled_ceiling"] == 0.0 and result["totals"]["tested"] == 0.0


def test_an_inconclusive_test_changes_nothing(tenant_id, triage):
    before, _ = _opp(tenant_id, triage)
    _submit(tenant_id, _start(tenant_id, triage), _cases(same=5))
    opp, _ = _opp(tenant_id, triage)
    assert opp["validation"] == "inconclusive"
    assert opp["projected_monthly_savings"] == before["projected_monthly_savings"]
    assert opp["savings_type"] == "modeled_ceiling"


def test_a_test_is_compared_with_its_own_model_s_part_not_the_whole_figure(tenant_id, triage):
    hook.ingest_events(tenant_id, [_spend(triage, "gpt-4o", 40_000_000, "openai")])
    before, _ = _opp(tenant_id, triage)
    sonnet_part = next(t["monthly"] for t in before["trail"] if t["from_model"] == SONNET)
    exp = _start(tenant_id, triage)
    assert exp["baseline"]["monthly"] == sonnet_part
    assert sonnet_part < before["projected_monthly_savings"]


def test_testing_one_of_two_models_leaves_the_other_a_ceiling(tenant_id, triage):
    hook.ingest_events(tenant_id, [_spend(triage, "gpt-4o", 40_000_000, "openai")])
    before, _ = _opp(tenant_id, triage)
    gpt_part = next(t["monthly"] for t in before["trail"] if t["from_model"] == "gpt-4o")

    done = _submit(tenant_id, _start(tenant_id, triage), _cases(better=5, same=20))
    opp, result = _opp(tenant_id, triage)
    assert opp["savings_type"] == "modeled_ceiling"  # not every part was tested
    assert opp["validation"] == "tested_offline"
    assert opp["projected_monthly_savings"] == round(gpt_part + done["result"]["monthly_saving"], 2)
    assert "Other models in this figure are untested ceilings" in opp["evidence"]
    assert result["totals"]["tested"] == 0.0


def test_a_failed_test_of_one_of_two_models_removes_only_its_part(tenant_id, triage):
    hook.ingest_events(tenant_id, [_spend(triage, "gpt-4o", 40_000_000, "openai")])
    before, _ = _opp(tenant_id, triage)
    gpt_part = next(t["monthly"] for t in before["trail"] if t["from_model"] == "gpt-4o")
    _submit(tenant_id, _start(tenant_id, triage), _cases(better=1, same=10, worse=14))
    opp, _ = _opp(tenant_id, triage)
    assert opp["test_failed"] is False
    assert opp["projected_monthly_savings"] == gpt_part
    assert "did not hold, so its part is left out" in opp["evidence"]


def test_testing_each_model_in_turn_keeps_both_results(tenant_id, triage):
    """Testing a second model must not throw away what the first test found."""
    hook.ingest_events(tenant_id, [_spend(triage, "gpt-4o", 40_000_000, "openai")])
    sonnet = _submit(tenant_id, _start(tenant_id, triage), _cases(better=5, same=20))
    gpt = _submit(
        tenant_id,
        _start(tenant_id, triage, control_model="gpt-4o", candidate_model="gpt-4o-mini"),
        _cases(better=3, same=22),
    )
    assert sonnet["outcome"] == gpt["outcome"] == "passed"

    opp, result = _opp(tenant_id, triage)
    assert opp["savings_type"] == "tested"  # every part tested, and held
    expected = round(sonnet["result"]["monthly_saving"] + gpt["result"]["monthly_saving"], 2)
    assert opp["projected_monthly_savings"] == pytest.approx(expected, abs=0.01)
    assert result["totals"]["tested"] == opp["projected_monthly_savings"]
    assert "gpt-4o-mini in place of gpt-4o" in opp["evidence"]
    assert "claude-haiku-4-5 in place of claude-sonnet-4-6" in opp["evidence"]


def test_what_testing_cost_is_shown_on_the_feature(tenant_id, triage):
    done = _submit(tenant_id, _start(tenant_id, triage), _cases(same=25))
    result = optimize_measured.opportunities(tenant_id, triage, PERIOD)
    assert result["testing_spend"] == round(done["test_cost"], 2) > 0


def test_only_providers_the_runner_can_call_are_offered_a_test(tenant_id):
    feature = features.add_feature(tenant_id, "Summaries")["id"]
    hook.ingest_events(tenant_id, [_spend(feature, "gemini-2.5-pro", 100_000_000, "google")])
    opp, _ = _opp(tenant_id, feature)
    assert opp is not None and opp["testable"] is False


def test_the_demo_shows_a_fully_tested_right_sizing_saving(tenant_id, app_env):
    from meter.sampledata import DEFAULT_PERIOD, insert_sample_data

    insert_sample_data(app_env, tenant_id, extended=True)
    app_env.commit()
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        triage = conn.execute(
            "SELECT id::text FROM feature WHERE name = 'AI threat triage'"
        ).fetchone()[0]
    result = optimize_measured.opportunities(tenant_id, triage, DEFAULT_PERIOD)
    rs = next(o for o in result["opportunities"] if o["lever"] == "model_rightsizing")
    assert (rs["savings_type"], rs["validation"]) == ("tested", "tested_offline")
    assert result["totals"]["tested"] == rs["projected_monthly_savings"] > 0
    assert result["testing_spend"] > 0
    overview = optimize_measured.copilot_overview(tenant_id, DEFAULT_PERIOD)
    assert overview["totals"]["tested"] >= rs["projected_monthly_savings"]
