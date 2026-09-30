"""The alerts API over HTTP.

Every other alert test calls alerts.create_rule directly, which is below the
request model. That is how "forecast_budget_pct" came to be accepted by the
application, tested thoroughly, and still rejected with a 422 by every real
request: the model capped condition_type at 16 characters and the condition is
19. The form showed nothing, and nothing in the suite had ever crossed the wire.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from meter import alerts
from meter.api import AlertRequest, create_app

GOOD_PASSWORD = "correct horse battery"


@pytest.fixture
def client(admin_conn, admin_conninfo, app_conninfo, monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "unit-test-secret-key")
    monkeypatch.setenv("DATABASE_URL", admin_conninfo)
    monkeypatch.setenv("DATABASE_APP_URL", app_conninfo)
    c = TestClient(create_app())
    c.post("/api/auth/signup", json={"email": "cfo@acme.com", "password": GOOD_PASSWORD})
    return c


def _rule(**over):
    body = {
        "name": "Projected to exceed budget",
        "metric": "combined_cost",
        "scope_type": "organization",
        "condition_type": "forecast_budget_pct",
        "threshold": 100,
        "window": "daily",
        "cooldown": "day",
        "channels": [{"channel": "in_app"}],
    }
    body.update(over)
    return body


def test_a_projected_budget_alert_can_be_created_over_http(client):
    assert client.put(
        "/api/budget",
        json={"amount": 5000, "cadence": "monthly", "effective_from": "2020-01-01"},
    ).status_code == 200

    resp = client.post("/api/alerts", json=_rule())
    assert resp.status_code == 201, resp.text
    assert resp.json()["condition_type"] == "forecast_budget_pct"


def test_every_condition_the_application_accepts_fits_the_request_model():
    """The tripwire. A length cap on the request model is a guard, not the
    validation — but a guard tighter than the thing it guards rejects valid
    input before the validation ever sees it. Checked against the real list,
    so the next condition added cannot fall into the same gap."""
    for condition in alerts.CONDITIONS:
        AlertRequest(**_rule(condition_type=condition))  # raises if it does not fit
    for metric in alerts.METRICS:
        AlertRequest(**_rule(metric=metric))
    for scope in alerts.SCOPES:
        AlertRequest(**_rule(scope_type=scope))


def test_the_application_still_rejects_what_it_does_not_accept(client):
    """Widening the guard must not widen what is allowed: an unknown condition
    is still refused, now by the application rather than by a length check."""
    resp = client.post("/api/alerts", json=_rule(condition_type="not_a_condition"))
    assert resp.status_code == 400
