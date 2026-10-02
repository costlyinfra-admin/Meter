"""Model tests Meter runs itself, and runs that survive a restart (EX-4).

The provider and the judge are faked; everything else is real: consent, the
captured calls, the evaluation key, the job table, the decision rule, and what
the result does to the recommendation.
"""

from __future__ import annotations

import hashlib
import json
import time
from decimal import Decimal

import httpx
import pytest
from meter import (
    experiments,
    features,
    hook,
    jobs,
    optimize_measured,
    prompt_eval,
)
from meter import prompt_capture as pc
from meter.db import app_dsn, connect, tenant_tx

SONNET, HAIKU = "claude-sonnet-4-6", "claude-haiku-4-5"
ACTOR = "cto@acme.com"
SYSTEM = "You triage alerts. ACME-SECRET-SYSTEM-PROMPT. Answer with one label."
INPUT = "ACME-SECRET-ALERT from 10.0.0.7"
CONTROL_ANSWER = "phishing"


@pytest.fixture(autouse=True)
def meters_model(monkeypatch):
    """The judge the consent names: Meter's own model, faked below."""
    monkeypatch.setenv("METER_DISCOVERY_BASE_URL", "https://api.groq.com/openai/v1")
    monkeypatch.setenv("METER_DISCOVERY_MODEL", "openai/gpt-oss-120b")
    monkeypatch.setenv("METER_DISCOVERY_API_KEY", "sk-judge-key")


class Provider:
    """Anthropic and the judge, faked, counting what each model was asked."""

    def __init__(self, candidate_answer=CONTROL_ANSWER, candidate_status=200, status=200):
        self.candidate_answer = candidate_answer
        self.candidate_status = candidate_status
        self.status = status
        self.calls = {SONNET: 0, HAIKU: 0}
        self.client = httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "api.anthropic.com" in str(request.url):
            model = body["model"]
            self.calls[model] += 1
            if self.status != 200:
                return httpx.Response(self.status, text='{"error":"bad key sk-replay-key"}')
            if model == HAIKU and self.candidate_status != 200:
                return httpx.Response(self.candidate_status, text='{"error":"overloaded"}')
            assert body["system"] == SYSTEM  # replayed with the prompt it ran with
            text = CONTROL_ANSWER if model == SONNET else self.candidate_answer
            return httpx.Response(
                200,
                json={
                    "content": [{"type": "text", "text": text}],
                    "usage": {"input_tokens": 1000, "output_tokens": 20},
                },
            )
        # The judge: a tie, both ways round.
        reply = json.dumps({"winner": "tie", "reason": "Both label it the same."})
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})


@pytest.fixture
def triage(app_env, tenant_id):
    return _triage(tenant_id)


def _triage(tenant_id):
    """$300 of Sonnet this month, prompt capture on, 25 captured calls, a key."""
    feature = features.add_feature(tenant_id, "AI threat triage")["id"]
    hook.ingest_events(
        tenant_id,
        [
            {
                "provider": "anthropic",
                "model": SONNET,
                "tokens_in": 100_000_000,
                "tokens_out": 0,
                "feature_id": feature,
            }
        ],
    )
    pc.grant_consent(
        tenant_id,
        ACTOR,
        accepted_version=pc.CONSENT_VERSION,
        accepted_disclosure=pc.disclosure(tenant_id),
    )
    pc.set_feature(tenant_id, feature, True, ACTOR)
    for n in range(25):
        pc.store_sample(
            tenant_id,
            {
                "feature_id": feature,
                "prompt_id": "triage",
                "prompt_version": f"v{n % 3}",  # several versions: per-version caps
                "provider": "anthropic",
                "model": SONNET,
                "template": SYSTEM,
                "input": [{"role": "user", "text": f"{INPUT} #{n}"}],
                "output": CONTROL_ANSWER,
                "parameters": {"temperature": 0, "max_tokens": 50},
                "tokens_in": 1000,
                "tokens_out": 20,
            },
        )
    prompt_eval.set_eval_key(tenant_id, "anthropic", "sk-replay-key", ACTOR)
    return feature


@pytest.fixture
def worker(monkeypatch):
    """Runs started by a request are worked inline, on the fake provider."""
    state = {"provider": Provider(), "started": True}
    real_run = jobs.run

    def start(tenant_id, job_id, *, background=True, client=None, judge_client=None):
        if state["started"]:
            real_run(tenant_id, job_id, client=client or state["provider"].client)

    monkeypatch.setattr(jobs, "start", start)
    return state


def _start(tenant_id, feature, **over):
    kw = {
        "control_model": SONNET,
        "candidate_model": HAIKU,
        "mode": "offline",
        "runs_at": "meter",
    }
    kw.update(over)
    return experiments.create(tenant_id, feature, "model_rightsizing", ACTOR, **kw)


def _job(app_env, experiment_id):
    return app_env.execute(
        "SELECT id, status, interruptions, cardinality(sample_ids) FROM eval_job "
        "WHERE experiment_id = %s",
        (experiment_id,),
    ).fetchone()


# ---------------------------------------------------------------------------
# A complete hosted test
# ---------------------------------------------------------------------------
def test_meter_replays_captured_calls_on_both_models_and_decides(
    app_env, tenant_id, triage, worker
):
    exp = _start(tenant_id, triage)
    assert exp["runs_at"] == "meter" and "token" not in exp

    done = experiments.get(tenant_id, exp["id"])
    assert (done["status"], done["outcome"]) == ("completed", "passed"), done["outcome_reason"]
    assert done["results_source"] == "meter"
    assert done["judge_model"] == "openai/gpt-oss-120b"
    r = done["result"]
    assert (r["cases"], r["compared"], r["same"], r["broken"]) == (25, 25, 25, 0)
    assert r["saving_fraction"] > 0.5  # Haiku against Sonnet, same tokens
    # Every captured call replayed once on each model, no more.
    assert worker["provider"].calls == {SONNET: 25, HAIKU: 25}
    # What the run cost, priced by Meter: both models on every case.
    expected = 25 * (pricing_of(SONNET, 1000, 20) + pricing_of(HAIKU, 1000, 20))
    assert done["test_cost"] == pytest.approx(float(expected), abs=1e-6)


def pricing_of(model, tin, tout):
    from meter import pricing

    return pricing.price(model, tin, tout, "anthropic")


def test_a_hosted_result_keeps_numbers_only_and_logs_who_spent_the_money(
    app_env, tenant_id, triage, worker
):
    exp = _start(tenant_id, triage)
    rows = app_env.execute(
        "SELECT * FROM experiment_case WHERE experiment_id = %s", (exp["id"],)
    ).fetchall()
    stored = json.dumps([list(map(str, r)) for r in rows])
    stored += json.dumps(experiments.get(tenant_id, exp["id"]), default=str)
    for secret in ("ACME-SECRET", CONTROL_ANSWER, "sk-replay-key"):
        assert secret not in stored
    # A case is its sample, hashed with the test: resumable, and unlinkable.
    sample = app_env.execute("SELECT id FROM prompt_sample LIMIT 1").fetchone()[0]
    want = hashlib.sha256(f"{exp['id']}:{sample}".encode()).hexdigest()
    assert want in {r[3] for r in rows}

    log = pc.audit_log(tenant_id)
    events = [e["event"] for e in log]
    assert "hosted_test_started" in events and "hosted_test_finished" in events
    started = next(e for e in log if e["event"] == "hosted_test_started")
    assert started["actor"] == ACTOR and started["detail"]["cases"] == 25
    assert "ACME-SECRET" not in json.dumps(log)


def test_a_passed_hosted_test_makes_the_recommendation_tested(app_env, tenant_id, triage, worker):
    _start(tenant_id, triage)
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        period = conn.execute("SELECT max(period) FROM inference_cost").fetchone()[0]
    found = optimize_measured.opportunities(tenant_id, triage, period)
    rs = next(o for o in found["opportunities"] if o["lever"] == "model_rightsizing")
    assert (rs["savings_type"], rs["validation"], rs["confidence"]) == (
        "tested",
        "tested_offline",
        "med",
    )
    assert "captured calls, replayed by Meter" in rs["evidence"]


def test_a_cheaper_model_that_errors_fails_the_test(app_env, tenant_id, triage, worker):
    worker["provider"] = Provider(candidate_status=529)
    exp = experiments.get(tenant_id, _start(tenant_id, triage)["id"])
    assert exp["outcome"] == "failed"
    assert exp["result"]["broken"] == 25


def test_broken_answers_fail_the_test_without_asking_the_judge(app_env, tenant_id, triage, worker):
    worker["provider"] = Provider(candidate_answer="   ")
    exp = experiments.get(tenant_id, _start(tenant_id, triage)["id"])
    assert exp["outcome"] == "failed" and "broken" in exp["outcome_reason"]


def test_a_refused_key_stops_the_run_instead_of_failing_the_model(
    app_env, tenant_id, triage, worker
):
    worker["provider"] = Provider(status=401)
    exp = experiments.get(tenant_id, _start(tenant_id, triage)["id"])
    assert (exp["status"], exp["outcome"]) == ("completed", "inconclusive")
    assert "stopped before it finished" in exp["outcome_reason"]
    assert "sk-replay-key" not in exp["outcome_reason"] and "***" in exp["outcome_reason"]
    assert worker["provider"].calls[HAIKU] == 0  # stopped at the first call
    assert _job(app_env, exp["id"])[1] == "failed"


# ---------------------------------------------------------------------------
# What it takes to start one
# ---------------------------------------------------------------------------
def test_without_consent_meter_will_not_run_it(app_env, tenant_id, triage, worker):
    pc.withdraw_consent(tenant_id, ACTOR)
    with pytest.raises(experiments.ExperimentError, match="allowed to capture"):
        _start(tenant_id, triage)


def test_without_capture_on_the_feature_there_is_nothing_to_replay(
    app_env, tenant_id, triage, worker
):
    pc.set_feature(tenant_id, triage, False, ACTOR)
    with pytest.raises(experiments.ExperimentError, match="not turned on for this feature"):
        _start(tenant_id, triage)


def test_without_a_key_it_says_where_to_add_one(app_env, tenant_id, triage, worker):
    prompt_eval.remove_eval_key(tenant_id, "anthropic", ACTOR)
    with pytest.raises(experiments.ExperimentError, match="evaluation key for Anthropic"):
        _start(tenant_id, triage)


def test_too_few_captured_calls_is_refused_before_any_money_is_spent(
    app_env, tenant_id, triage, worker
):
    with pytest.raises(experiments.ExperimentError, match="holds 25 captured calls"):
        _start(tenant_id, triage, min_cases=30)
    assert worker["provider"].calls == {SONNET: 0, HAIKU: 0}


def test_a_running_test_counts_what_it_may_spend_against_the_shared_cap(
    app_env, tenant_id, triage, worker
):
    worker["started"] = False  # written down, not yet worked
    exp = _start(tenant_id, triage)
    estimate = app_env.execute(
        "SELECT estimate FROM eval_job WHERE experiment_id = %s", (exp["id"],)
    ).fetchone()[0]
    assert estimate > 0
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        # The same figure a prompt-rewrite test is held to (prompt_eval.estimate).
        assert jobs.spent_this_month(conn) == estimate


def test_replacing_a_test_does_not_count_the_old_one_against_the_cap(
    app_env, tenant_id, triage, worker, monkeypatch
):
    worker["started"] = False
    first = _start(tenant_id, triage)
    estimate = app_env.execute(
        "SELECT estimate FROM eval_job WHERE experiment_id = %s", (first["id"],)
    ).fetchone()[0]
    monkeypatch.setattr(prompt_eval, "MONTHLY_CAP", estimate * Decimal("1.5"))
    second = _start(tenant_id, triage)  # room for one, and this replaces the first
    assert experiments.get(tenant_id, first["id"])["status"] == "cancelled"
    assert experiments.get(tenant_id, second["id"])["status"] == "running"


def test_what_finished_tests_spent_counts_against_the_cap(
    app_env, tenant_id, triage, worker, monkeypatch
):
    spent = experiments.get(tenant_id, _start(tenant_id, triage)["id"])["test_cost"]
    monkeypatch.setattr(prompt_eval, "MONTHLY_CAP", Decimal(str(spent)) * Decimal("1.5"))
    with pytest.raises(experiments.ExperimentError, match="monthly cap of \\$"):
        _start(tenant_id, triage)
    assert worker["provider"].calls == {SONNET: 25, HAIKU: 25}  # the second never ran


def test_only_a_model_test_on_cases_can_run_inside_meter(app_env, tenant_id, triage, worker):
    with pytest.raises(experiments.ExperimentError, match="can run inside Meter"):
        _start(tenant_id, triage, mode="live")
    with pytest.raises(experiments.ExperimentError, match="where the test runs"):
        _start(tenant_id, triage, runs_at="somewhere")


def test_options_say_what_a_hosted_test_would_replay_and_cost(app_env, tenant_id, triage):
    from meter import offline_tests

    opts = offline_tests.options(tenant_id, triage)["hosted"]
    assert opts["capturing"] and opts["feature_enabled"]
    mine = opts["by_control"][SONNET]
    assert (mine["reason"], mine["cases"], mine["has_key"]) == (None, 25, True)
    assert mine["estimates"][HAIKU] == pytest.approx(
        float(25 * (pricing_of(SONNET, 1000, 20) + pricing_of(HAIKU, 1000, 20))), abs=1e-4
    )
    prompt_eval.remove_eval_key(tenant_id, "anthropic", ACTOR)
    assert (
        offline_tests.options(tenant_id, triage)["hosted"]["by_control"][SONNET]["reason"]
        == "no_key"
    )


# ---------------------------------------------------------------------------
# Runs that survive an interruption
# ---------------------------------------------------------------------------
def test_an_interrupted_run_carries_on_from_where_it_stopped(app_env, tenant_id, triage, worker):
    worker["started"] = False
    exp = _start(tenant_id, triage)
    provider = worker["provider"]
    job_id = str(_job(app_env, exp["id"])[0])

    # A deadline already passed: one case, then the run is put down.
    assert jobs.run(tenant_id, job_id, client=provider.client, deadline=time.monotonic()) == (
        "paused"
    )
    assert _job(app_env, exp["id"])[1] == "queued"
    assert provider.calls == {SONNET: 1, HAIKU: 1}

    # The scheduled job picks it up and finishes it, without redoing a case.
    assert jobs.run_scheduled(client=provider.client) == [{"job_id": job_id, "status": "done"}]
    done = experiments.get(tenant_id, exp["id"])
    assert done["outcome"] == "passed" and done["result"]["cases"] == 25
    assert provider.calls == {SONNET: 25, HAIKU: 25}


def test_a_run_whose_holder_vanished_is_taken_over_when_someone_looks(
    app_env, tenant_id, triage, worker
):
    worker["started"] = False
    exp = _start(tenant_id, triage)
    # Its holder died mid-run: still marked running, lease long gone.
    app_env.execute(
        "UPDATE eval_job SET status = 'running', lease_token = gen_random_uuid(), "
        "leased_until = now() - interval '1 minute' WHERE experiment_id = %s",
        (exp["id"],),
    )
    app_env.commit()
    worker["started"] = True
    looked = experiments.get(tenant_id, exp["id"])  # looking at it carries it on
    assert looked["progress"]["planned"] == 25
    done = experiments.get(tenant_id, exp["id"])
    assert done["outcome"] == "passed"
    assert _job(app_env, exp["id"])[1:3] == ("done", 1)  # one interruption, counted


def test_a_run_that_keeps_dying_is_stopped_not_retried_for_ever(app_env, tenant_id, triage, worker):
    worker["started"] = False
    exp = _start(tenant_id, triage)
    app_env.execute(
        "UPDATE eval_job SET status = 'running', interruptions = %s, "
        "leased_until = now() - interval '1 minute' WHERE experiment_id = %s",
        (jobs.MAX_INTERRUPTIONS - 1, exp["id"]),
    )
    app_env.commit()
    jobs.run_scheduled(client=worker["provider"].client)
    done = experiments.get(tenant_id, exp["id"])
    assert done["outcome"] == "inconclusive"
    assert "interrupted 3 times" in done["outcome_reason"]
    assert worker["provider"].calls == {SONNET: 0, HAIKU: 0}


def test_a_held_run_is_not_taken_over(app_env, tenant_id, triage, worker):
    worker["started"] = False
    exp = _start(tenant_id, triage)
    job_id = str(_job(app_env, exp["id"])[0])
    app_env.execute(
        "UPDATE eval_job SET status = 'running', leased_until = now() + interval '5 minutes' "
        "WHERE id = %s",
        (job_id,),
    )
    app_env.commit()
    assert jobs.run(tenant_id, job_id, client=worker["provider"].client) == "busy"
    assert jobs.run_scheduled(client=worker["provider"].client) == []


def test_cancelling_stops_the_run_and_keeps_what_it_spent(app_env, tenant_id, triage, worker):
    worker["started"] = False
    exp = _start(tenant_id, triage)
    job_id = str(_job(app_env, exp["id"])[0])
    jobs.run(tenant_id, job_id, client=worker["provider"].client, deadline=time.monotonic())
    experiments.cancel(tenant_id, exp["id"])

    assert _job(app_env, exp["id"])[1] == "failed"
    assert jobs.run_scheduled(client=worker["provider"].client) == []
    assert worker["provider"].calls == {SONNET: 1, HAIKU: 1}
    gone = experiments.get(tenant_id, exp["id"])
    assert gone["status"] == "cancelled" and gone["test_cost"] > 0
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        period = conn.execute("SELECT period FROM experiment WHERE id = %s", (exp["id"],))
        spent = experiments.testing_spend(conn, triage, period.fetchone()[0])
    assert spent == round(gone["test_cost"], 2)


def test_withdrawing_consent_mid_run_stops_it(app_env, tenant_id, triage, worker):
    worker["started"] = False
    exp = _start(tenant_id, triage)
    job_id = str(_job(app_env, exp["id"])[0])
    jobs.run(tenant_id, job_id, client=worker["provider"].client, deadline=time.monotonic())
    pc.withdraw_consent(tenant_id, ACTOR)
    jobs.run_scheduled(client=worker["provider"].client)
    done = experiments.get(tenant_id, exp["id"])
    assert done["outcome"] == "inconclusive"
    assert "turned off or paused" in done["outcome_reason"]
    assert worker["provider"].calls == {SONNET: 1, HAIKU: 1}


def test_one_tenant_never_sees_or_runs_anothers_job(app_env, tenant_id, triage, worker):
    worker["started"] = False
    exp = _start(tenant_id, triage)
    job_id = str(_job(app_env, exp["id"])[0])
    other = str(
        app_env.execute("INSERT INTO tenant (name) VALUES ('Other Co') RETURNING id").fetchone()[0]
    )
    app_env.commit()
    assert jobs.run(other, job_id, client=worker["provider"].client) == "busy"
    assert jobs.resume_if_abandoned(other, "hosted_test", exp["id"]) is False
    assert worker["provider"].calls == {SONNET: 0, HAIKU: 0}
    with connect(app_dsn()) as conn, tenant_tx(conn, other):
        assert conn.execute("SELECT count(*) FROM eval_job").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Over HTTP
# ---------------------------------------------------------------------------
def test_a_hosted_test_over_http_says_how_far_it_has_got(
    admin_conn, admin_conninfo, app_conninfo, monkeypatch, worker
):
    from fastapi.testclient import TestClient
    from meter.api import create_app

    monkeypatch.setenv("APP_SECRET_KEY", "unit-test-secret-key")
    monkeypatch.setenv("DATABASE_URL", admin_conninfo)
    monkeypatch.setenv("DATABASE_APP_URL", app_conninfo)
    client = TestClient(create_app())
    client.post("/api/auth/signup", json={"email": ACTOR, "password": "correct horse battery"})
    feature = _triage(client.get("/api/auth/me").json()["tenant_id"])

    options = client.get(f"/api/features/{feature}/experiments/options").json()
    assert options["hosted"]["by_control"][SONNET]["cases"] == 25

    worker["started"] = False
    resp = client.post(
        f"/api/features/{feature}/experiments",
        json={
            "lever": "model_rightsizing",
            "mode": "offline",
            "runs_at": "meter",
            "control_model": SONNET,
            "candidate_model": HAIKU,
        },
    )
    assert resp.status_code == 201, resp.text
    exp = resp.json()
    assert (exp["status"], exp["runs_at"]) == ("running", "meter")
    assert "token" not in exp

    seen = client.get(f"/api/experiments/{exp['id']}").json()
    assert seen["progress"] == {"planned": 25, "done": 0, "status": "queued", "interruptions": 0}

    bad = client.post(
        f"/api/features/{feature}/experiments",
        json={
            "lever": "model_rightsizing",
            "mode": "offline",
            "runs_at": "elsewhere",
            "control_model": SONNET,
            "candidate_model": HAIKU,
        },
    )
    assert bad.status_code == 400 and "where the test runs" in bad.json()["detail"]


def test_the_daily_job_leaves_a_run_it_cannot_judge_for_the_web_service(
    app_env, tenant_id, triage, worker, monkeypatch
):
    worker["started"] = False
    exp = _start(tenant_id, triage)
    # The scheduled job has no judge configured: it cannot check the consent
    # that named one, so it must not decide the consent is gone.
    monkeypatch.delenv("METER_DISCOVERY_API_KEY")
    monkeypatch.delenv("METER_DISCOVERY_BASE_URL")
    monkeypatch.delenv("METER_DISCOVERY_MODEL")
    (skipped,) = jobs.run_scheduled(client=worker["provider"].client)
    assert skipped["status"] == "skipped"
    assert experiments.get(tenant_id, exp["id"])["status"] == "running"
    assert worker["provider"].calls == {SONNET: 0, HAIKU: 0}


def test_consent_to_earlier_terms_keeps_capture_but_not_model_tests(
    app_env, tenant_id, triage, worker
):
    # Agreed before the terms named model tests: capture carries on...
    app_env.execute(
        "UPDATE prompt_capture_consent SET consent_version = %s WHERE tenant_id = %s",
        (pc.CAPTURE_SINCE, tenant_id),
    )
    app_env.commit()
    status = pc.consent_status(tenant_id)
    assert (status["capturing"], status["terms_extended"], status["model_tests"]) == (
        True,
        True,
        False,
    )
    # ...but Meter will not replay captured calls through another model.
    with pytest.raises(experiments.ExperimentError, match="Agree to the updated terms"):
        _start(tenant_id, triage)
    from meter import offline_tests

    hosted = offline_tests.options(tenant_id, triage)["hosted"]
    assert hosted["by_control"][SONNET]["reason"] == "terms_not_agreed"
    # Agreeing to the current words is all it takes.
    pc.grant_consent(
        tenant_id,
        ACTOR,
        accepted_version=pc.CONSENT_VERSION,
        accepted_disclosure=pc.disclosure(tenant_id),
    )
    assert _start(tenant_id, triage)["status"] == "running"
