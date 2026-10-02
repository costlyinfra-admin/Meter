"""Live tests (EX-3): a cheaper model on a share of real traffic.

These stand in for a customer's application by sending the tagged calls and the
quality scores its SDK would send, through traces.ingest. They check what is
accepted, the rule (no verdict before the minimum; guardrails stop it early;
cost clearly lower; quality not clearly worse), the guardrail alert, and what a
result does to the recommendation.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from meter import (
    alerts,
    alerts_eval,
    experiments,
    features,
    hook,
    live_tests,
    optimize_measured,
    traces,
)
from meter.db import app_dsn, connect, tenant_tx

PERIOD = dt.date(2026, 6, 1)
WHEN = "2026-06-15T10:00:00Z"
SONNET, HAIKU = "claude-sonnet-4-6", "claude-haiku-4-5"
ACTOR = "cto@acme.com"


@pytest.fixture
def triage(tenant_id):
    """$300 of Sonnet in June."""
    feature = features.add_feature(tenant_id, "AI threat triage")
    hook.ingest_events(
        tenant_id,
        [
            {
                "provider": "anthropic",
                "model": SONNET,
                "tokens_in": 100_000_000,
                "tokens_out": 0,
                "feature_id": feature["id"],
                "occurred_at": WHEN,
            }
        ],
    )
    return feature["id"]


def _start(tenant_id, feature_id, **dials):
    live = {"min_calls": 100, "min_days": 1, **dials}
    return experiments.create(
        tenant_id,
        feature_id,
        "model_rightsizing",
        ACTOR,
        mode="live",
        control_model=SONNET,
        candidate_model=HAIKU,
        live=live,
    )


def _calls(
    tenant_id,
    exp_id,
    group,
    n,
    *,
    model=None,
    tokens=(1000, 200),
    latency=800,
    failed=0,
    feature_id=None,
    vary=False,
    spread=40,
):
    """n calls tagged into a group, `failed` of them failing."""
    model = model or (SONNET if group == "control" else HAIKU)
    events = []
    for i in range(n):
        tin = tokens[0] + ((i % 7) * spread if vary else 0)
        events.append(
            {
                "event_type": "span.failed" if i < failed else "span.completed",
                "trace_id": f"t-{group}-{uuid.uuid4().hex[:12]}",
                "span_id": f"s-{i}",
                "span_kind": "llm",
                "provider": "anthropic",
                "model": model,
                "tokens_in": 0 if i < failed else tin,
                "tokens_out": 0 if i < failed else tokens[1],
                "latency_ms": latency,
                "occurred_at": WHEN,
                "feature_id": feature_id,
                "experiment_id": exp_id,
                "experiment_group": group,
            }
        )
    for start in range(0, len(events), traces.MAX_EVENTS_PER_BATCH):
        traces.ingest(tenant_id, events[start : start + traces.MAX_EVENTS_PER_BATCH])


def _scores(tenant_id, exp_id, group, values):
    traces.ingest(
        tenant_id,
        [
            {
                "event_type": "experiment.score",
                "experiment_id": exp_id,
                "experiment_group": group,
                "score": v,
                "occurred_at": WHEN,
            }
            for v in values
        ],
    )


def _age(tenant_id, exp_id, days):
    """Pretend the test started `days` ago."""
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        conn.execute(
            "UPDATE experiment SET created_at = now() - %s WHERE id = %s",
            (dt.timedelta(days=days), exp_id),
        )


def _evaluate(tenant_id, exp_id):
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        live_tests.evaluate(conn, exp_id)
    return experiments.get(tenant_id, exp_id)


def _tagged(tenant_id):
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        return conn.execute(
            "SELECT experiment_group, COUNT(*) FROM ai_span WHERE experiment_id IS NOT NULL "
            "GROUP BY experiment_group ORDER BY 1"
        ).fetchall()


def _rule(tenant_id, rule_id):
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        return conn.execute(
            "SELECT metric, enabled, status FROM alert_rule WHERE id = %s", (rule_id,)
        ).fetchone()


def _opp(tenant_id, feature_id):
    result = optimize_measured.opportunities(tenant_id, feature_id, PERIOD)
    return next(o for o in result["opportunities"] if o["lever"] == "model_rightsizing"), result


# ---------------------------------------------------------------------------
# Starting a test
# ---------------------------------------------------------------------------
def test_a_live_test_starts_running_with_its_own_guardrail_alert(tenant_id, triage):
    exp = _start(tenant_id, triage)
    assert (exp["mode"], exp["status"]) == ("live", "running")
    assert exp["setting_label"].startswith(
        "claude-haiku-4-5 in place of claude-sonnet-4-6 on 10% of live traffic"
    )
    metric, enabled, _status = _rule(tenant_id, exp["guardrail_alert_id"])
    assert (metric, enabled) == ("experiment_guardrail", True)
    rule = next(r for r in alerts.list_rules(tenant_id) if r["id"] == exp["guardrail_alert_id"])
    assert [c["channel"] for c in rule["channels"]] == ["in_app"]


@pytest.mark.parametrize(
    "over, message",
    [
        ({"min_calls": 50}, "between 100 and 100,000"),
        ({"min_days": 0}, "between 1 and 30"),
        ({"max_error_increase": 0.5}, "between 0 and 20 points"),
        ({"traffic_share": 80}, "between 1% and 50%"),
        ({"quality_margin": 0.9}, "between 0% and 50%"),
    ],
)
def test_a_dial_out_of_range_is_refused_in_words(tenant_id, triage, over, message):
    with pytest.raises(experiments.ExperimentError, match=message):
        _start(tenant_id, triage, **over)


def test_only_right_sizing_can_go_live_and_live_dials_need_a_live_test(tenant_id, triage):
    with pytest.raises(experiments.ExperimentError, match="Only model right-sizing"):
        experiments.create(
            tenant_id,
            triage,
            "duplicate_calls",
            ACTOR,
            mode="live",
            ttl_seconds=600,
            scoped_only=False,
        )
    with pytest.raises(experiments.ExperimentError, match="apply to live tests only"):
        experiments.create(
            tenant_id,
            triage,
            "model_rightsizing",
            ACTOR,
            control_model=SONNET,
            candidate_model=HAIKU,
            live={"traffic_share": 10},
        )


# ---------------------------------------------------------------------------
# What is accepted
# ---------------------------------------------------------------------------
def test_calls_are_tagged_only_for_a_running_test_of_this_organization(tenant_id, triage, app_env):
    exp = _start(tenant_id, triage)
    other = str(
        app_env.execute("INSERT INTO tenant (name) VALUES ('Other') RETURNING id").fetchone()[0]
    )
    app_env.commit()
    _calls(tenant_id, exp["id"], "control", 3)
    _calls(tenant_id, str(uuid.uuid4()), "candidate", 2)  # no such test
    _calls(other, exp["id"], "candidate", 2)  # another organization's test
    assert _tagged(tenant_id) == [("control", 3)]
    assert _tagged(other) == []


def test_a_malformed_tag_is_dropped_without_losing_the_call(tenant_id, triage):
    exp = _start(tenant_id, triage)
    traces.ingest(
        tenant_id,
        [
            {
                "event_type": "span.completed",
                "trace_id": "t-x",
                "span_id": "s-x",
                "span_kind": "llm",
                "provider": "anthropic",
                "model": SONNET,
                "tokens_in": 10,
                "tokens_out": 1,
                "occurred_at": WHEN,
                "experiment_id": "not-a-uuid",
                "experiment_group": "control",
            },
            {
                "event_type": "span.completed",
                "trace_id": "t-y",
                "span_id": "s-y",
                "span_kind": "llm",
                "provider": "anthropic",
                "model": SONNET,
                "tokens_in": 10,
                "tokens_out": 1,
                "occurred_at": WHEN,
                "experiment_id": exp["id"],
                "experiment_group": "treatment",
            },
        ],
    )
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        spans = conn.execute("SELECT experiment_id FROM ai_span").fetchall()
    assert len(spans) == 2 and all(s[0] is None for s in spans)


def test_a_score_is_a_number_for_a_group_and_nothing_else(tenant_id, triage):
    exp = _start(tenant_id, triage)
    _scores(tenant_id, exp["id"], "control", [1, 0, 1])
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        row = conn.execute("SELECT n, total, total_squares FROM experiment_score").fetchone()
    assert row == (3, 2.0, 2.0)
    for bad in (True, "good", float("nan")):
        with pytest.raises(traces.TraceError):
            traces.ingest(
                tenant_id,
                [
                    {
                        "event_type": "experiment.score",
                        "experiment_id": exp["id"],
                        "experiment_group": "control",
                        "score": bad,
                    }
                ],
            )
    with pytest.raises(traces.TraceError, match="needs experiment_id"):
        traces.ingest(tenant_id, [{"event_type": "experiment.score", "score": 1}])
    with pytest.raises(traces.TraceError, match="does not accept prompt"):
        traces.ingest(
            tenant_id,
            [
                {
                    "event_type": "experiment.score",
                    "experiment_id": exp["id"],
                    "experiment_group": "control",
                    "score": 1,
                    "response": "the answer",
                }
            ],
        )


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------
def test_no_verdict_before_the_minimum_however_good_it_looks(tenant_id, triage):
    exp = _start(tenant_id, triage, min_calls=200, min_days=7)
    _calls(tenant_id, exp["id"], "control", 150)
    _calls(tenant_id, exp["id"], "candidate", 150)
    _age(tenant_id, exp["id"], 2)
    done = _evaluate(tenant_id, exp["id"])
    assert (done["status"], done["outcome"]) == ("running", None)
    assert done["result"]["provisional"] is True
    assert "150 and 150 of the 200 calls" in done["outcome_reason"]
    assert "2 of 7 days" in done["outcome_reason"]
    opp, _ = _opp(tenant_id, triage)
    assert opp["validation"] == "untested" and opp["savings_type"] == "modeled_ceiling"


def test_a_cheaper_model_that_holds_on_live_traffic_passes_and_is_tested_with_high_confidence(
    tenant_id, triage
):
    exp = _start(tenant_id, triage)
    _calls(tenant_id, exp["id"], "control", 120, vary=True)
    _calls(tenant_id, exp["id"], "candidate", 120, vary=True)
    _age(tenant_id, exp["id"], 2)
    done = _evaluate(tenant_id, exp["id"])
    assert (done["status"], done["outcome"]) == ("completed", "passed"), done["outcome_reason"]
    assert "Quality was not measured" in done["outcome_reason"]
    assert _rule(tenant_id, exp["guardrail_alert_id"])[1] is False  # nothing left to guard

    opp, result = _opp(tenant_id, triage)
    assert (opp["savings_type"], opp["validation"], opp["confidence"]) == (
        "tested",
        "tested_live",
        "high",
    )
    assert opp["projected_monthly_savings"] == pytest.approx(
        300 * done["result"]["saving_fraction"], abs=0.01
    )
    assert opp["evidence"].startswith("Tested on live traffic")
    assert result["totals"]["tested"] == opp["projected_monthly_savings"]


def test_a_guardrail_stops_the_test_early_and_its_alert_fires(tenant_id, triage):
    exp = _start(tenant_id, triage, min_calls=1000, min_days=14)
    _calls(tenant_id, exp["id"], "control", 120, failed=1)
    _calls(tenant_id, exp["id"], "candidate", 120, failed=12)  # 10% against under 1%
    done = _evaluate(tenant_id, exp["id"])
    assert (done["status"], done["outcome"]) == ("completed", "failed")
    assert done["outcome_reason"].startswith("Stopped by a guardrail")
    assert "Send this traffic back to claude-sonnet-4-6" in done["outcome_reason"]
    # The breach really happened, so the alert stays on, and fires.
    assert _rule(tenant_id, exp["guardrail_alert_id"])[1] is True
    assert alerts_eval.evaluate_rule(tenant_id, exp["guardrail_alert_id"])["status"] == "triggered"
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        message = conn.execute(
            "SELECT message FROM alert_event WHERE alert_id = %s", (exp["guardrail_alert_id"],)
        ).fetchone()[0]
    assert "breached a guardrail" in message


def test_a_slower_candidate_trips_the_latency_guardrail(tenant_id, triage):
    exp = _start(tenant_id, triage)
    _calls(tenant_id, exp["id"], "control", 120, latency=800)
    _calls(tenant_id, exp["id"], "candidate", 120, latency=2000)
    done = _evaluate(tenant_id, exp["id"])
    assert done["outcome"] == "failed"
    assert "slowest 5%" in done["outcome_reason"]


def test_guardrails_wait_for_enough_calls_to_mean_anything(tenant_id, triage):
    exp = _start(tenant_id, triage)
    _calls(tenant_id, exp["id"], "control", 50)
    _calls(tenant_id, exp["id"], "candidate", 50, failed=10)
    assert _evaluate(tenant_id, exp["id"])["status"] == "running"
    assert alerts_eval.evaluate_rule(tenant_id, exp["guardrail_alert_id"])["status"] == (
        "insufficient_data"
    )


def test_failed_calls_do_not_make_a_model_look_cheaper(tenant_id, triage):
    """Cost per call is over successful calls only."""
    exp = _start(tenant_id, triage, max_error_increase=0.2)
    _calls(tenant_id, exp["id"], "control", 120, model=SONNET)
    # The same model in both groups, one failing more: same cost per call.
    _calls(tenant_id, exp["id"], "candidate", 120, model=SONNET, failed=20)
    done = _evaluate(tenant_id, exp["id"])
    groups = done["result"]["groups"]
    assert groups["candidate"]["cost_per_call"] == pytest.approx(groups["control"]["cost_per_call"])
    # Every call tagged candidate ran Sonnet, failed or not: the flag and the
    # model choice disagree, and the result says so.
    assert groups["candidate"]["other_model_calls"] == 120


def test_a_saving_the_noise_could_explain_is_inconclusive(tenant_id, triage):
    exp = _start(tenant_id, triage)
    # A tenth of a cent cheaper per call, in calls that vary by several cents.
    _calls(
        tenant_id,
        exp["id"],
        "control",
        110,
        model=SONNET,
        tokens=(1100, 200),
        vary=True,
        spread=2000,
    )
    _calls(
        tenant_id,
        exp["id"],
        "candidate",
        110,
        model=SONNET,
        tokens=(1000, 200),
        vary=True,
        spread=2000,
    )
    _age(tenant_id, exp["id"], 2)
    done = _evaluate(tenant_id, exp["id"])
    assert done["outcome"] == "inconclusive"
    assert "clear of zero" in done["outcome_reason"]


def test_quality_that_falls_beyond_the_margin_fails(tenant_id, triage):
    exp = _start(tenant_id, triage)
    _calls(tenant_id, exp["id"], "control", 120)
    _calls(tenant_id, exp["id"], "candidate", 120)
    _scores(tenant_id, exp["id"], "control", [0.9, 0.8] * 20)
    _scores(tenant_id, exp["id"], "candidate", [0.6, 0.5] * 20)
    _age(tenant_id, exp["id"], 2)
    done = _evaluate(tenant_id, exp["id"])
    assert done["outcome"] == "failed"
    assert done["outcome_reason"].startswith("Quality fell")


def test_quality_that_holds_is_part_of_the_verdict(tenant_id, triage):
    exp = _start(tenant_id, triage)
    _calls(tenant_id, exp["id"], "control", 120)
    _calls(tenant_id, exp["id"], "candidate", 120)
    _scores(tenant_id, exp["id"], "control", [4.0] * 40)
    _scores(tenant_id, exp["id"], "candidate", [4.0] * 40)  # any scale
    _age(tenant_id, exp["id"], 2)
    done = _evaluate(tenant_id, exp["id"])
    assert done["outcome"] == "passed"
    assert "Average quality 4 against 4" in done["outcome_reason"]


def test_a_loosened_rule_is_said_out_loud(tenant_id, triage):
    exp = _start(tenant_id, triage, max_error_increase=0.05)
    assert "under a loosened rule" in exp["setting_label"]
    _calls(tenant_id, exp["id"], "control", 120)
    _calls(tenant_id, exp["id"], "candidate", 120)
    _age(tenant_id, exp["id"], 2)
    done = _evaluate(tenant_id, exp["id"])
    assert "Decided under a loosened rule" in done["outcome_reason"]
    assert done["result"]["rule"]["relaxed"] is True


def test_ending_a_live_test_turns_its_guardrail_off(tenant_id, triage):
    exp = _start(tenant_id, triage)
    assert experiments.cancel(tenant_id, exp["id"])["status"] == "cancelled"
    assert _rule(tenant_id, exp["guardrail_alert_id"])[1] is False
    # And tags stop being accepted.
    _calls(tenant_id, exp["id"], "control", 2)
    assert _tagged(tenant_id) == []


def test_the_scheduled_job_brings_every_running_test_up_to_date(tenant_id, triage):
    exp = _start(tenant_id, triage)
    _calls(tenant_id, exp["id"], "control", 120)
    _calls(tenant_id, exp["id"], "candidate", 120)
    _age(tenant_id, exp["id"], 2)
    results = live_tests.run_scheduled()
    assert {"experiment_id": exp["id"], "status": "evaluated"} in results
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        status = conn.execute(
            "SELECT status FROM experiment WHERE id = %s", (exp["id"],)
        ).fetchone()[0]
    assert status == "completed"


def test_the_guardrail_alert_cannot_be_edited_from_the_alert_form(tenant_id, triage):
    exp = _start(tenant_id, triage)
    with pytest.raises(alerts.AlertError, match="belongs to a live test"):
        alerts.update_rule(tenant_id, exp["guardrail_alert_id"], {"name": "x"})
    # ...and nobody can make one by hand.
    with pytest.raises(alerts.AlertError, match="Unknown metric"):
        alerts.create_rule(
            tenant_id,
            {
                "name": "x",
                "metric": "experiment_guardrail",
                "condition_type": "exceeds",
                "threshold": 0,
                "window": "daily",
                "channels": [{"channel": "in_app"}],
            },
        )


def test_enough_calls_is_not_enough_without_the_days(tenant_id, triage):
    """A busy first day is still one day: weekday and weekend traffic differ."""
    exp = _start(tenant_id, triage, min_days=7)
    _calls(tenant_id, exp["id"], "control", 120)
    _calls(tenant_id, exp["id"], "candidate", 120)
    _age(tenant_id, exp["id"], 2)
    done = _evaluate(tenant_id, exp["id"])
    assert (done["status"], done["outcome"]) == ("running", None)
    assert "2 of 7 days" in done["outcome_reason"]


def test_the_quality_margin_is_relative_so_any_score_scale_works(tenant_id, triage):
    """Scores on a 1-5 scale, 3% lower: inside a 5% margin. An absolute margin
    of 0.05 would call a 0.12 drop a failure on this scale and a pass on 0-1."""
    exp = _start(tenant_id, triage)
    _calls(tenant_id, exp["id"], "control", 120)
    _calls(tenant_id, exp["id"], "candidate", 120)
    _scores(tenant_id, exp["id"], "control", [4.0] * 40)
    _scores(tenant_id, exp["id"], "candidate", [3.88] * 40)
    _age(tenant_id, exp["id"], 2)
    assert _evaluate(tenant_id, exp["id"])["outcome"] == "passed"
