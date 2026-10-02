"""Offline tests over HTTP: the run's token, and the closed shape of results.

The run is the one place a customer's machine talks to Meter with a credential
that is not their session or their ingest token, and the one place it sends
test results. So these check, across the wire, that the token reaches exactly
one experiment and only its two endpoints, and that a result carrying anything
but numbers — a prompt, an answer, a judge's reasoning — is refused whole.
"""

from __future__ import annotations

import hashlib
import pathlib
import re

import pytest
from fastapi.testclient import TestClient
from meter import experiments, features, hook, offline_tests
from meter.api import ExperimentRequest, ExperimentResultsRequest, create_app

PASSWORD = "correct horse battery"
WHEN = "2026-06-15T10:00:00Z"


@pytest.fixture
def client(admin_conn, admin_conninfo, app_conninfo, monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "unit-test-secret-key")
    monkeypatch.setenv("DATABASE_URL", admin_conninfo)
    monkeypatch.setenv("DATABASE_APP_URL", app_conninfo)
    c = TestClient(create_app())
    c.post("/api/auth/signup", json={"email": "cto@acme.com", "password": PASSWORD})
    return c


@pytest.fixture
def feature(client):
    tenant = client.get("/api/auth/me").json()["tenant_id"]
    feature_id = features.add_feature(tenant, "AI threat triage")["id"]
    hook.ingest_events(
        tenant,
        [
            {
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "tokens_in": 100_000_000,
                "tokens_out": 0,
                "feature_id": feature_id,
                "occurred_at": WHEN,
            }
        ],
    )
    return feature_id


def _start(client, feature):
    resp = client.post(
        f"/api/features/{feature}/experiments",
        json={
            "lever": "model_rightsizing",
            "control_model": "claude-sonnet-4-6",
            "candidate_model": "claude-haiku-4-5",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _cases(n=25):
    call = {"tokens_in": 1000, "tokens_out": 200, "latency_ms": 500, "error": False}
    return [
        {
            "case": hashlib.sha256(str(i).encode()).hexdigest(),
            "control": call,
            "candidate": call,
            "check_failures": 0,
            "verdict": "same",
        }
        for i in range(n)
    ]


def _results(cases=None, **over):
    body = {
        "source": "runner",
        "runner_version": "2.5.0",
        "judge_model": "claude-sonnet-4-6",
        "cases": _cases() if cases is None else cases,
    }
    body.update(over)
    return body


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_a_whole_run_over_http(client, feature):
    options = client.get(f"/api/features/{feature}/experiments/options").json()
    assert options["controls"][0]["default_candidate"] == "claude-haiku-4-5"

    exp = _start(client, feature)
    run = TestClient(client.app)  # the customer's machine: no session at all
    spec = run.get("/api/experiment-runs/spec", headers=_bearer(exp["token"]))
    assert spec.status_code == 200 and spec.json()["candidate_model"] == "claude-haiku-4-5"

    posted = run.post(
        "/api/experiment-runs/results", headers=_bearer(exp["token"]), json=_results()
    )
    assert posted.status_code == 200, posted.text
    # The run learns how it went, and nothing about the business.
    assert set(posted.json()) == {"status", "outcome", "outcome_reason"}
    assert posted.json()["outcome"] == "passed"

    seen = client.get(f"/api/experiments/{exp['id']}").json()
    assert seen["status"] == "completed" and "token" not in seen


@pytest.mark.parametrize(
    "where, extra",
    [
        ("top", {"prompt": "what is our refund policy"}),
        ("case", {"output": "Refunds are issued within 30 days"}),
        ("call", {"text": "Refunds are issued within 30 days"}),
        ("top", {"judge_reason": "B answers the question"}),
    ],
)
def test_a_result_carrying_anything_but_numbers_is_refused_whole(client, feature, where, extra):
    exp = _start(client, feature)
    body = _results()
    if where == "top":
        body.update(extra)
    elif where == "case":
        body["cases"][0] = {**body["cases"][0], **extra}
    else:
        body["cases"][0] = {**body["cases"][0], "control": {**body["cases"][0]["control"], **extra}}
    resp = TestClient(client.app).post(
        "/api/experiment-runs/results", headers=_bearer(exp["token"]), json=body
    )
    assert resp.status_code == 422
    # Refused before anything was stored, so the run can fix it and post again.
    assert offline_tests.resolve_token(exp["token"]) is not None


def test_a_case_id_must_be_a_hash_not_the_customer_s_own_label(client, feature):
    exp = _start(client, feature)
    cases = _cases()
    cases[0]["case"] = "refund-question-1"
    resp = TestClient(client.app).post(
        "/api/experiment-runs/results", headers=_bearer(exp["token"]), json=_results(cases)
    )
    assert resp.status_code == 422


def test_the_token_reaches_one_experiment_and_two_endpoints(client, feature):
    exp = _start(client, feature)
    run = TestClient(client.app)
    # Not a session: it reads nothing else.
    assert (
        run.get(f"/api/experiments/{exp['id']}", headers=_bearer(exp["token"])).status_code == 401
    )
    assert run.get("/api/features", headers=_bearer(exp["token"])).status_code == 401
    # Not an ingest token either.
    assert (
        run.post("/api/hook/events", headers=_bearer(exp["token"]), json={"events": []}).status_code
        == 401
    )


def test_without_the_token_nothing_is_answered(client, feature):
    _start(client, feature)
    # A signed-in session is not enough: results come from a run, with its token.
    assert client.post("/api/experiment-runs/results", json=_results()).status_code == 401
    assert (
        client.get("/api/experiment-runs/spec", headers=_bearer("mtx_not-a-real-token")).status_code
        == 401
    )


def test_a_spent_token_is_refused(client, feature):
    exp = _start(client, feature)
    run = TestClient(client.app)
    assert (
        run.post(
            "/api/experiment-runs/results", headers=_bearer(exp["token"]), json=_results()
        ).status_code
        == 200
    )
    again = run.post("/api/experiment-runs/results", headers=_bearer(exp["token"]), json=_results())
    assert again.status_code == 401
    assert "expired, was used, or was cancelled" in again.json()["detail"]


def test_a_refusal_from_the_application_says_why(client, feature):
    resp = client.post(
        f"/api/features/{feature}/experiments",
        json={
            "lever": "model_rightsizing",
            "control_model": "claude-sonnet-4-6",
            "candidate_model": "gpt-4o-mini",
        },
    )
    assert resp.status_code == 400
    assert "same provider" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Tripwires: a guard must never be tighter than what it guards
# ---------------------------------------------------------------------------
def test_every_setting_the_application_offers_fits_the_request_model(client, feature):
    tenant = client.get("/api/auth/me").json()["tenant_id"]
    opts = offline_tests.options(tenant, feature)
    lo, hi = opts["rule"]["loss_margin_range"]
    cases_lo, cases_hi = opts["rule"]["min_cases_range"]
    for control in opts["controls"]:
        for candidate in control["candidates"]:
            for margin, cases in ((lo, cases_lo), (hi, cases_hi)):
                ExperimentRequest(
                    lever="model_rightsizing",
                    control_model=control["model"],
                    candidate_model=candidate["model"],
                    loss_margin=margin,
                    min_cases=cases,
                )
    assert "model_rightsizing" in experiments.OFFLINE


def test_the_results_model_accepts_everything_the_database_does():
    migration = (
        pathlib.Path(__file__).resolve().parents[1] / "migrations" / "0066_offline_experiments.sql"
    ).read_text()
    # The judge model's pattern is the same one the database checks.
    db_pattern = re.search(r"judge_model ~ '(.+?)'", migration).group(1)
    field = ExperimentResultsRequest.model_fields["judge_model"]
    assert any(getattr(m, "pattern", None) == db_pattern for m in field.metadata)
    # As many cases as a run may send, and every check count the table holds.
    ExperimentResultsRequest(**_results(_cases(offline_tests.MAX_CASES)))
    case = _cases(1)[0]
    ExperimentResultsRequest(**_results([{**case, "check_failures": 10}]))
    for verdict in ("better", "same", "worse", "unjudged"):
        ExperimentResultsRequest(**_results([{**case, "verdict": verdict}]))
    for source in ("runner", "promptfoo", "inspect"):
        ExperimentResultsRequest(**_results(source=source))


# ---------------------------------------------------------------------------
# End to end: the real meter-test command, through the real routes
# ---------------------------------------------------------------------------
def test_the_real_runner_completes_a_test_and_the_recommendation_becomes_tested(
    client, feature, tmp_path
):
    """The SDK's own command, talking to this app, with only the provider faked:
    the test the customer sees on their screen comes out the far end."""
    import io
    import json as _json

    from costlyinfra_meter import testing

    exp = _start(client, feature)
    run = TestClient(client.app)
    secret = "the quick brown fox jumped over the lazy dog"

    def http(url, headers, body):
        if url.startswith("https://meter.test"):
            path = url[len("https://meter.test") :]
            resp = (
                run.get(path, headers=headers)
                if body is None
                else run.post(path, headers=headers, content=body)
            )
            return resp.status_code, resp.content
        req = _json.loads(body)
        if "ANSWER A" in str(req["messages"]):
            text = _json.dumps({"winner": "tie"})
        else:
            text = f"An answer about {secret}"
        return 200, _json.dumps(
            {
                "content": [{"type": "text", "text": text}],
                "usage": {"input_tokens": 1000, "output_tokens": 150},
            }
        ).encode()

    cases = tmp_path / "cases.jsonl"
    cases.write_text("\n".join(_json.dumps({"input": f"{secret} {i}"}) for i in range(25)))
    out = io.StringIO()
    code = testing.main(
        ["run", "--cases", str(cases)],
        http=http,
        out=out,
        env={
            "METER_EXPERIMENT_TOKEN": exp["token"],
            "METER_URL": "https://meter.test",
            "ANTHROPIC_API_KEY": "sk-test",
        },
    )
    assert code == 0, out.getvalue()
    assert (
        out.getvalue().strip().endswith("claude-haiku-4-5 cost 67% less per call on these cases.")
    )

    seen = client.get(f"/api/experiments/{exp['id']}").json()
    assert (seen["status"], seen["outcome"], seen["results_source"]) == (
        "completed",
        "passed",
        "runner",
    )
    opps = client.get(f"/api/features/{feature}/opportunities").json()
    rs = next(o for o in opps["opportunities"] if o["lever"] == "model_rightsizing")
    assert rs["savings_type"] == "tested" and rs["validation"] == "tested_offline"
    assert opps["totals"]["tested"] == rs["projected_monthly_savings"] > 0
