"""A live test, end to end: the real SDK, the real routes, only the provider faked.

The customer's application in miniature: two wrapped clients, one per group,
a feature flag choosing between them, and a quality score per answer. What
comes out the far end is the recommendation the customer sees.
"""

from __future__ import annotations

import json
import threading

import costlyinfra_meter
import pytest
from fastapi.testclient import TestClient
from meter import features
from meter.api import create_app
from meter.db import app_dsn, connect, tenant_tx

PASSWORD = "correct horse battery"
SECRET = "the quick brown fox jumped over the lazy dog"
SONNET, HAIKU = "claude-sonnet-4-6", "claude-haiku-4-5"


@pytest.fixture
def client(admin_conn, admin_conninfo, app_conninfo, monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "unit-test-secret-key")
    monkeypatch.setenv("DATABASE_URL", admin_conninfo)
    monkeypatch.setenv("DATABASE_APP_URL", app_conninfo)
    c = TestClient(create_app())
    c.post("/api/auth/signup", json={"email": "cto@acme.com", "password": PASSWORD})
    return c


class Anthropic:
    """An Anthropic-shaped client answering with whichever model was asked for."""

    def __init__(self):
        class messages:
            @staticmethod
            def create(**kwargs):
                return type(
                    "Response",
                    (),
                    {
                        "model": kwargs["model"],
                        "usage": type(
                            "U",
                            (),
                            {
                                "input_tokens": 1200,
                                "output_tokens": 300,
                                "cache_read_input_tokens": 0,
                                "cache_creation_input_tokens": 0,
                            },
                        )(),
                    },
                )()

        self.messages = messages


def test_a_live_test_runs_through_the_real_sdk_and_comes_out_tested(client):
    tenant = client.get("/api/auth/me").json()["tenant_id"]
    token = client.post("/api/hook/token").json()["token"]
    feature = features.add_feature(tenant, "AI threat triage")["id"]
    lock = threading.Lock()
    sent = []

    def transport(url, headers, body):
        path = url[url.index("/api/") :]
        with lock:
            if body is None:
                resp = client.get(path, headers=headers)
            else:
                sent.append(body.decode())
                resp = client.post(path, headers=headers, content=body)
        return resp.status_code, resp.content

    meter = costlyinfra_meter.Meter(
        application="support-agent",
        ingest_url="https://app.test/api/hook/events",
        token=token,
        feature_id=feature,
        transport=transport,
        flush_interval=0.01,
    )
    # A month of Sonnet on this feature, so there is a recommendation to test.
    for _ in range(3):
        meter.wrap(Anthropic(), feature_id=feature).messages.create(model=SONNET)
    assert meter.flush(timeout=5.0)
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant):
        conn.execute("UPDATE inference_cost SET amount = 300, period = date_trunc('month', now())")

    exp = client.post(
        f"/api/features/{feature}/experiments",
        json={
            "lever": "model_rightsizing",
            "mode": "live",
            "control_model": SONNET,
            "candidate_model": HAIKU,
            "traffic_share": 50,
            "min_calls": 100,
            "min_days": 1,
        },
    ).json()
    assert exp["status"] == "running", exp

    control = meter.wrap(Anthropic(), feature_id=feature, experiment=exp["id"], group="control")
    candidate = meter.wrap(Anthropic(), feature_id=feature, experiment=exp["id"], group="candidate")
    for i in range(240):
        on_candidate = i % 2 == 1  # the customer's own flag
        wrapped, model = (candidate, HAIKU) if on_candidate else (control, SONNET)
        wrapped.messages.create(model=model, messages=[{"role": "user", "content": SECRET}])
        meter.score(exp["id"], "candidate" if on_candidate else "control", 1.0)
    assert meter.flush(timeout=10.0)
    assert SECRET not in "".join(sent)

    with connect(app_dsn()) as conn, tenant_tx(conn, tenant):
        conn.execute(
            "UPDATE experiment SET created_at = now() - interval '2 days', "
            "last_evaluated_at = NULL WHERE id = %s",
            (exp["id"],),
        )
    seen = client.get(f"/api/experiments/{exp['id']}").json()
    assert (seen["status"], seen["outcome"]) == ("completed", "passed"), seen["outcome_reason"]
    groups = seen["result"]["groups"]
    assert (groups["control"]["calls"], groups["candidate"]["calls"]) == (120, 120)
    assert groups["candidate"]["scores"] == 120
    assert "Average quality 1 against 1" in seen["outcome_reason"]

    opps = client.get(f"/api/features/{feature}/opportunities").json()
    rs = next(o for o in opps["opportunities"] if o["lever"] == "model_rightsizing")
    assert (rs["savings_type"], rs["validation"], rs["confidence"]) == (
        "tested",
        "tested_live",
        "high",
    )
    assert SECRET not in json.dumps(rs)
