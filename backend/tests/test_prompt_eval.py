"""Proving a rewrite is not worse: the rule, the money, and the fairness of it."""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
from meter import prompt_capture as pc
from meter import prompt_eval as pe
from meter import prompt_optimize as po

TEMPLATE = "You classify {alert}. ACME-SECRET-TEMPLATE. Answer in one word."
REWRITE = "Classify {alert}. One word."
BEFORE_ANSWER = "phishing, because the domain is a lookalike"
AFTER_ANSWER = "phishing"


@pytest.fixture(autouse=True)
def meters_model(monkeypatch):
    monkeypatch.setenv("METER_DISCOVERY_BASE_URL", "https://api.groq.com/openai/v1")
    monkeypatch.setenv("METER_DISCOVERY_MODEL", "openai/gpt-oss-120b")
    monkeypatch.setenv("METER_DISCOVERY_API_KEY", "sk-judge-key")


def endpoints(*, winner="shorter", after=AFTER_ANSWER, provider_status=200, rewrite=REWRITE):
    """Meter's provider and judge, faked. One client answers all three URLs."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "api.anthropic.com" in url:
            if provider_status != 200:
                return httpx.Response(provider_status, text='{"error":"bad key sk-replay-key"}')
            body = json.loads(request.content)
            # The rewrite is the shorter system prompt; answer accordingly.
            is_rewrite = len(body.get("system", "")) < len(TEMPLATE)
            text = after if is_rewrite else BEFORE_ANSWER
            return httpx.Response(
                200,
                json={
                    "content": [{"type": "text", "text": text}],
                    "usage": {
                        "input_tokens": 300 if is_rewrite else 1000,
                        "output_tokens": 5 if is_rewrite else 40,
                    },
                },
            )
        # The judge, and the optimizer that wrote the candidate.
        sent = request.content.decode()
        if "You compare two answers" in sent:
            if winner == "shorter":
                # Prefers the rewrite's answer whichever side it is read on.
                body = json.loads(request.content)
                shown = body["messages"][1]["content"]
                first, second = shown.split("ANSWER B:")
                pick = "A" if len(first) < len(second) else "B"
            else:
                pick = winner  # a judge with a position habit
            reply = json.dumps({"winner": pick, "reason": "Same verdict, fewer words."})
        else:
            reply = json.dumps(
                {
                    "template": rewrite,
                    "changes": [
                        {"category": "output_format", "reason": "The caller reads only the label."}
                    ],
                }
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})

    return httpx.Client(transport=httpx.MockTransport(handler))


def ready(app_env, tenant_id, monkeypatch, *, cases=4, with_key=True):
    """A consented feature with samples, a rewrite to test, and a replay key."""
    monkeypatch.setattr(po, "MIN_SAMPLES", 2)
    monkeypatch.setattr(pe, "MIN_CASES_FOR_DECISION", 3)
    feature = str(
        app_env.execute(
            "INSERT INTO feature (tenant_id, name) VALUES (%s, 'Ticket triage') RETURNING id",
            (tenant_id,),
        ).fetchone()[0]
    )
    app_env.commit()
    pc.grant_consent(
        tenant_id,
        "cto@acme.com",
        accepted_version=pc.CONSENT_VERSION,
        accepted_disclosure=pc.disclosure(tenant_id),
    )
    pc.set_feature(tenant_id, feature, True, "cto@acme.com")
    for n in range(cases):
        pc.store_sample(
            tenant_id,
            {
                "feature_id": feature,
                "prompt_id": "triage",
                "prompt_version": "v7",
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "template": TEMPLATE,
                "input": [{"role": "user", "text": f"Alert {n}"}],
                "output": "phishing",
                "parameters": {"temperature": 0, "max_tokens": 100},
                "tokens_in": 1000,
                "tokens_out": 40,
            },
        )
    template_id = po.list_prompts(tenant_id)["prompts"][0]["template_id"]
    po.generate_candidate(tenant_id, template_id, "cto@acme.com", client=endpoints())
    if with_key:
        pe.set_eval_key(tenant_id, "anthropic", "sk-replay-key", "cto@acme.com")
    return feature, template_id


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "better, same, worse, checks, done, decision",
    [
        (6, 18, 0, 0, 24, "recommended"),
        (2, 20, 2, 0, 24, "recommended"),  # a loss inside the margin is noise
        (0, 20, 4, 0, 24, "not_recommended"),  # a pattern of losses is not
        (10, 13, 0, 1, 24, "not_recommended"),  # anything broken ends it
        (5, 5, 0, 0, 10, "not_recommended"),  # too few cases to mean anything
    ],
)
def test_the_decision_rule(better, same, worse, checks, done, decision):
    verdict, reason = pe.decide(better, same, worse, checks, done)
    assert verdict == decision
    assert reason


def test_a_biased_judge_cannot_decide_a_rewrite(app_env, tenant_id, monkeypatch):
    # Asked both ways round, a judge that always prefers the first answer
    # disagrees with itself — which is a tie, not a win.
    _, template_id = ready(app_env, tenant_id, monkeypatch)
    run = pe.start_evaluation(
        tenant_id, template_id, "cto@acme.com", client=endpoints(winner="A"), background=False
    )
    done = pe.status(tenant_id, run["evaluation_id"])
    # The same judge, asked both ways round, contradicts itself: neither side wins.
    assert done["better"] == 0 and done["worse"] == 0
    assert done["same"] == done["cases_done"]
    assert done["decision"] == "recommended"  # a tie is not a regression


# ---------------------------------------------------------------------------
# What a run costs, and whether it may happen
# ---------------------------------------------------------------------------
def test_a_replay_needs_a_key_that_can_make_model_calls(app_env, tenant_id, monkeypatch):
    _, template_id = ready(app_env, tenant_id, monkeypatch, with_key=False)
    plan = pe.estimate(tenant_id, template_id)
    assert (plan["can_run"], plan["reason"]) == (False, "no_key")
    with pytest.raises(pe.EvalError, match="Add a key"):
        pe.start_evaluation(tenant_id, template_id, "cto@acme.com", background=False)


def test_the_estimate_is_priced_from_the_tokens_the_samples_used(app_env, tenant_id, monkeypatch):
    _, template_id = ready(app_env, tenant_id, monkeypatch, cases=4)
    plan = pe.estimate(tenant_id, template_id)
    # 4 cases x 2 calls x (1000 in, 40 out) on claude-sonnet-4-6.
    assert plan["can_run"] is True
    assert plan["cases"] == 4
    assert plan["cost_estimate"] == pytest.approx(0.0288, abs=0.0005)


def test_a_run_that_would_pass_the_monthly_cap_is_refused(app_env, tenant_id, monkeypatch):
    _, template_id = ready(app_env, tenant_id, monkeypatch)
    monkeypatch.setattr(pe, "MONTHLY_CAP", Decimal("0.001"))
    assert pe.estimate(tenant_id, template_id)["reason"] == "over_cap"
    with pytest.raises(pe.EvalError, match="monthly evaluation cap"):
        pe.start_evaluation(tenant_id, template_id, "cto@acme.com", background=False)


def test_evaluation_needs_consent_that_still_stands(app_env, tenant_id, monkeypatch):
    _, template_id = ready(app_env, tenant_id, monkeypatch)
    monkeypatch.setenv("METER_DISCOVERY_MODEL", "a-different-model")  # disclosure changed
    with pytest.raises(pe.EvalError, match="not turned on, or is paused"):
        pe.start_evaluation(tenant_id, template_id, "cto@acme.com", background=False)


# ---------------------------------------------------------------------------
# A complete run
# ---------------------------------------------------------------------------
def test_a_passing_run_measures_the_saving_and_recommends(app_env, tenant_id, monkeypatch):
    _, template_id = ready(app_env, tenant_id, monkeypatch, cases=4)
    started = pe.start_evaluation(
        tenant_id, template_id, "cto@acme.com", client=endpoints(), background=False
    )
    done = pe.status(tenant_id, started["evaluation_id"])

    assert (done["status"], done["decision"]) == ("completed", "recommended")
    assert done["cases_done"] == 4
    assert done["better"] == 4 and done["worse"] == 0
    # Measured per call from the replay, priced from the price book.
    assert done["cost_after"] < done["cost_before"]
    assert (done["tokens_in_before"], done["tokens_in_after"]) == (1000, 300)
    assert done["spend"] > 0
    assert po.prompt_detail(tenant_id, template_id)["candidate"]["status"] == "recommended"


def test_a_broken_answer_stops_a_recommendation(app_env, tenant_id, monkeypatch):
    _, template_id = ready(app_env, tenant_id, monkeypatch)
    # The rewrite answers with nothing: a deterministic failure, not a matter of taste.
    started = pe.start_evaluation(
        tenant_id, template_id, "cto@acme.com", client=endpoints(after="  "), background=False
    )
    done = pe.status(tenant_id, started["evaluation_id"])
    assert done["decision"] == "not_recommended"
    assert done["check_failures"] == done["cases_done"]
    assert "broken" in done["decision_reason"]
    assert po.prompt_detail(tenant_id, template_id)["candidate"]["status"] == "not_recommended"


def test_a_provider_failure_leaves_the_run_failed_and_says_nothing_about_the_key(
    app_env, tenant_id, monkeypatch
):
    _, template_id = ready(app_env, tenant_id, monkeypatch)
    started = pe.start_evaluation(
        tenant_id,
        template_id,
        "cto@acme.com",
        client=endpoints(provider_status=401),
        background=False,
    )
    done = pe.status(tenant_id, started["evaluation_id"])
    assert done["status"] == "failed"
    assert "sk-replay-key" not in done["error"] and "***" in done["error"]
    # The rewrite is back where it was: untested, not wrongly cleared.
    assert po.prompt_detail(tenant_id, template_id)["candidate"]["status"] == "not_evaluated"


def test_replayed_answers_are_encrypted_and_reading_them_is_logged(app_env, tenant_id, monkeypatch):
    _, template_id = ready(app_env, tenant_id, monkeypatch)
    started = pe.start_evaluation(
        tenant_id, template_id, "cto@acme.com", client=endpoints(), background=False
    )
    stored = app_env.execute(
        "SELECT before_cipher, after_cipher FROM prompt_evaluation_case WHERE tenant_id = %s",
        (tenant_id,),
    ).fetchone()
    raw = bytes(stored[0]) + bytes(stored[1])
    assert b"lookalike" not in raw and b"phishing" not in raw

    cases = pe.case_content(tenant_id, started["evaluation_id"], "analyst@acme.com")
    assert cases[0]["before"] == BEFORE_ANSWER and cases[0]["after"] == AFTER_ANSWER
    events = {e["event"] for e in pc.audit_log(tenant_id)}
    assert {"evaluation_started", "evaluation_finished", "evaluation_viewed"} <= events
    assert "ACME-SECRET-TEMPLATE" not in json.dumps(pc.audit_log(tenant_id))


def test_the_key_is_write_only_and_removable(app_env, tenant_id):
    keys = pe.set_eval_key(tenant_id, "anthropic", "sk-replay-key", "cto@acme.com")
    assert [k for k in keys if k["provider"] == "anthropic"][0]["has_key"] is True
    assert "sk-replay-key" not in json.dumps(keys)
    assert any(e["event"] == "eval_key_added" for e in pc.audit_log(tenant_id))

    after = pe.remove_eval_key(tenant_id, "anthropic", "cto@acme.com")
    assert [k for k in after if k["provider"] == "anthropic"][0]["has_key"] is False


def test_one_tenant_never_sees_anothers_evaluation(app_env, tenant_id, monkeypatch):
    _, template_id = ready(app_env, tenant_id, monkeypatch)
    started = pe.start_evaluation(
        tenant_id, template_id, "cto@acme.com", client=endpoints(), background=False
    )
    other = str(
        app_env.execute("INSERT INTO tenant (name) VALUES ('Other Co') RETURNING id").fetchone()[0]
    )
    app_env.commit()
    assert pe.status(other, started["evaluation_id"]) is None
    assert pe.case_content(other, started["evaluation_id"], "intruder@other.com") is None
    assert pe.eval_keys(other) == [
        {"provider": p, "has_key": False, "added_by": None, "added_at": None} for p in pe.PROVIDERS
    ]
