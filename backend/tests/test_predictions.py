"""Prediction vs outcome (EX-5): what Meter claimed when a change was applied,
frozen, and how much of it the bill shows arriving.

Model right-sizing is the price lever used throughout: it makes each call
cheaper, which is exactly what the bill's cost per call can check.
"""

from __future__ import annotations

import datetime as dt

import pytest
from meter import features, optimize_measured
from meter.db import app_dsn, connect, tenant_tx

APRIL, MAY, JUNE = dt.date(2026, 4, 1), dt.date(2026, 5, 1), dt.date(2026, 6, 1)
SONNET = "claude-sonnet-4-6"


def _bill(tenant_id, feature_id, period, amount, calls, model=SONNET):
    """A month of the connector's own numbers for a feature."""
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        conn.execute(
            """
            INSERT INTO inference_cost (tenant_id, feature_id, provider, model, amount,
                                        period, tokens_in, tokens_out, request_count,
                                        source, confidence)
            VALUES (%s, %s, 'anthropic', %s, %s, %s, 100000000, 0, %s, 'cost_api', 'high')
            """,
            (tenant_id, feature_id, model, amount, period, calls),
        )


def _feature(tenant_id, name="AI threat triage"):
    return features.add_feature(tenant_id, name)["id"]


def _action(tenant_id, feature_id, lever="model_rightsizing", period=JUNE):
    result = optimize_measured.opportunities(tenant_id, feature_id, period)
    return next(a for a in result["actions"] if a["lever"] == lever)


def _rightsizing(tenant_id, feature_id, period):
    result = optimize_measured.opportunities(tenant_id, feature_id, period)
    return next(o for o in result["opportunities"] if o["lever"] == "model_rightsizing")


# ---------------------------------------------------------------------------
# Freezing the prediction
# ---------------------------------------------------------------------------
def test_applying_freezes_meters_own_figure_and_what_it_rested_on(app_env, tenant_id):
    feature = _feature(tenant_id)
    _bill(tenant_id, feature, APRIL, 300, 100)  # $3 a call
    card = _rightsizing(tenant_id, feature, APRIL)

    # The browser's figure is not what is frozen: Meter's own is.
    applied = optimize_measured.mark_applied(
        tenant_id, feature, "model_rightsizing", 999_999.0, APRIL
    )
    p = applied["prediction"]
    assert p["monthly"] == card["projected_monthly_savings"] < 300
    assert (p["savings_type"], p["confidence"]) == (card["savings_type"], card["confidence"])
    assert p["spend"] == 300.0
    assert p["reduction"] == pytest.approx(p["monthly"] / 300, abs=1e-6)
    assert p["experiment_id"] is None  # untested
    assert _action(tenant_id, feature, period=APRIL)["projected_monthly"] == p["monthly"]


def test_a_change_meter_no_longer_finds_keeps_the_figure_it_was_applied_with(
    app_env, tenant_id
):
    feature = _feature(tenant_id)
    _bill(tenant_id, feature, APRIL, 300, 100)
    applied = optimize_measured.mark_applied(tenant_id, feature, "prompt_caching", 40.0, APRIL)
    p = applied["prediction"]
    assert (p["monthly"], p["savings_type"]) == (40.0, None)
    assert p["reduction"] == pytest.approx(40 / 300, abs=1e-6)


def test_a_prediction_never_promises_more_than_the_whole_bill(app_env, tenant_id):
    feature = _feature(tenant_id)
    _bill(tenant_id, feature, APRIL, 300, 100)
    p = optimize_measured.mark_applied(tenant_id, feature, "prompt_caching", 5_000.0, APRIL)[
        "prediction"
    ]
    # The frozen dollar figure is what was applied; the predicted fall in cost
    # per unit cannot exceed all of it.
    assert (p["monthly"], p["reduction"]) == (5_000.0, 1.0)


def test_with_no_billed_spend_nothing_is_predicted(app_env, tenant_id):
    feature = _feature(tenant_id)
    applied = optimize_measured.mark_applied(tenant_id, feature, "prompt_caching", 40.0, APRIL)
    assert applied["prediction"]["reduction"] is None
    _bill(tenant_id, feature, JUNE, 100, 50)
    a = _action(tenant_id, feature, "prompt_caching")
    assert a["prediction"] is None and a["outcome"] is None
    assert "no billed spend when this was applied" in a["outcome_note"]


# ---------------------------------------------------------------------------
# The bill's answer
# ---------------------------------------------------------------------------
def test_the_bill_says_how_much_of_the_predicted_fall_arrived(app_env, tenant_id):
    feature = _feature(tenant_id)
    _bill(tenant_id, feature, APRIL, 300, 100)  # $3.00 a call
    p = optimize_measured.mark_applied(tenant_id, feature, "model_rightsizing", 0, APRIL)[
        "prediction"
    ]
    _bill(tenant_id, feature, JUNE, 100, 50)  # $2.00 a call: a third cheaper

    a = _action(tenant_id, feature)
    assert a["prediction"]["reduction"] == pytest.approx(p["reduction"])
    assert a["prediction"]["unit_cost"] == pytest.approx(3.0 * (1 - p["reduction"]), abs=1e-5)
    assert a["outcome"]["reduction"] == pytest.approx(1 / 3, abs=1e-5)
    assert a["outcome"]["delivered"] == pytest.approx((1 / 3) / p["reduction"], abs=1e-3)
    assert a["outcome_note"] is None


def test_a_cost_per_call_that_rose_is_an_answer_not_a_gap(app_env, tenant_id):
    feature = _feature(tenant_id)
    _bill(tenant_id, feature, APRIL, 300, 100)  # $3 a call
    optimize_measured.mark_applied(tenant_id, feature, "model_rightsizing", 0, APRIL)
    _bill(tenant_id, feature, JUNE, 360, 100)  # $3.60 a call
    a = _action(tenant_id, feature)
    assert a["outcome"]["reduction"] == pytest.approx(-0.2, abs=1e-6)
    assert a["outcome"]["delivered"] < 0


def test_nothing_is_checked_in_the_month_it_was_applied(app_env, tenant_id):
    feature = _feature(tenant_id)
    _bill(tenant_id, feature, JUNE, 300, 100)
    optimize_measured.mark_applied(tenant_id, feature, "model_rightsizing", 0, JUNE)
    a = _action(tenant_id, feature)
    assert a["prediction"] is not None and a["outcome"] is None
    assert "from the month after" in a["outcome_note"]


def test_without_a_bill_to_compare_it_says_which_half_is_missing(app_env, tenant_id):
    feature = _feature(tenant_id)
    _bill(tenant_id, feature, APRIL, 300, 100)
    optimize_measured.mark_applied(tenant_id, feature, "model_rightsizing", 0, APRIL)
    a = _action(tenant_id, feature)  # nothing billed in June
    assert a["outcome"] is None
    assert "Not checked against the bill" in a["outcome_note"]


def test_removing_repeated_calls_is_not_scored_by_cost_per_call(app_env, tenant_id):
    feature = _feature(tenant_id)
    _bill(tenant_id, feature, APRIL, 300, 100)
    optimize_measured.mark_applied(tenant_id, feature, "duplicate_calls", 50.0, APRIL)
    _bill(tenant_id, feature, JUNE, 100, 50)
    a = _action(tenant_id, feature, "duplicate_calls")
    assert a["prediction"]["reduction"] == pytest.approx(50 / 300, abs=1e-6)
    assert a["outcome"] is None
    assert "fewer calls, not cheaper ones" in a["outcome_note"]


def test_a_change_applied_before_predictions_were_kept_says_so(app_env, tenant_id):
    feature = _feature(tenant_id)
    _bill(tenant_id, feature, APRIL, 300, 100)
    _bill(tenant_id, feature, JUNE, 100, 50)
    app_env.execute(
        "INSERT INTO optimization_action (tenant_id, feature_id, lever, applied_on, "
        "projected_monthly) VALUES (%s, %s, 'model_rightsizing', %s, 100)",
        (tenant_id, feature, APRIL),
    )
    app_env.commit()
    a = _action(tenant_id, feature)
    assert a["prediction"] is None and a["outcome"] is None
    assert "before Meter recorded its predictions" in a["outcome_note"]


# ---------------------------------------------------------------------------
# A tested figure
# ---------------------------------------------------------------------------
def test_a_tested_figure_is_frozen_with_the_test_behind_it(app_env, tenant_id):
    from tests.test_offline_tests import _cases, _start, _submit

    feature = _feature(tenant_id)
    _bill(tenant_id, feature, JUNE, 300, 100)
    exp = _start(tenant_id, feature)
    _submit(tenant_id, exp, _cases(better=5, same=20))
    p = optimize_measured.mark_applied(tenant_id, feature, "model_rightsizing", 0, JUNE)[
        "prediction"
    ]
    assert (p["savings_type"], p["validation"], p["experiment_id"]) == (
        "tested",
        "tested_offline",
        exp["id"],
    )
    assert _action(tenant_id, feature)["prediction"]["experiment_id"] == exp["id"]


# ---------------------------------------------------------------------------
# Calibration across changes
# ---------------------------------------------------------------------------
def test_calibration_is_reported_by_kind_of_figure_never_pooled(app_env, tenant_id):
    from tests.test_offline_tests import _cases, _start, _submit

    # One tested change, applied in May, that delivered its full prediction...
    tested = _feature(tenant_id, "Tested feature")
    _bill(tenant_id, tested, MAY, 300, 100)
    exp = _start(tenant_id, tested)
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        conn.execute("UPDATE experiment SET period = %s WHERE id = %s", (MAY, exp["id"]))
    _submit(tenant_id, exp, _cases(better=5, same=20))
    pt = optimize_measured.mark_applied(tenant_id, tested, "model_rightsizing", 0, MAY)[
        "prediction"
    ]
    _bill(tenant_id, tested, JUNE, 3.0 * (1 - pt["reduction"]) * 100, 100)

    # ...one untested ceiling that delivered a third of its...
    ceiling = _feature(tenant_id, "Ceiling feature")
    _bill(tenant_id, ceiling, MAY, 300, 100)
    pc = optimize_measured.mark_applied(tenant_id, ceiling, "model_rightsizing", 0, MAY)[
        "prediction"
    ]
    _bill(tenant_id, ceiling, JUNE, 3.0 * (1 - pc["reduction"] / 3) * 100, 100)

    # ...and one that cannot be scored.
    dedup = _feature(tenant_id, "Dedup feature")
    _bill(tenant_id, dedup, MAY, 300, 100)
    optimize_measured.mark_applied(tenant_id, dedup, "duplicate_calls", 30.0, MAY)
    _bill(tenant_id, dedup, JUNE, 270, 90)

    cal = optimize_measured.copilot_overview(tenant_id, JUNE)["calibration"]
    assert pt["savings_type"] == "tested" and pc["savings_type"] == "modeled_ceiling"
    kinds = {k["savings_type"]: k for k in cal["by_savings_type"]}
    assert kinds["tested"]["count"] == 1
    assert kinds["tested"]["median_delivered"] == pytest.approx(1.0, abs=0.01)
    assert kinds["modeled_ceiling"]["median_delivered"] == pytest.approx(1 / 3, abs=0.01)
    assert "measured" not in kinds
    assert cal["count"] == 2
    assert (cal["waiting"], cal["unpredicted"]) == (1, 0)


# ---------------------------------------------------------------------------
# Over HTTP, and in the demo
# ---------------------------------------------------------------------------
def test_the_browser_cannot_choose_the_figure_that_is_frozen(
    admin_conn, admin_conninfo, app_conninfo, monkeypatch
):
    from fastapi.testclient import TestClient
    from meter.api import create_app

    monkeypatch.setenv("APP_SECRET_KEY", "unit-test-secret-key")
    monkeypatch.setenv("DATABASE_URL", admin_conninfo)
    monkeypatch.setenv("DATABASE_APP_URL", app_conninfo)
    client = TestClient(create_app())
    client.post("/api/auth/signup", json={"email": "cto@acme.com", "password": "correct horse"})
    tenant = client.get("/api/auth/me").json()["tenant_id"]
    feature = _feature(tenant)
    _bill(tenant, feature, APRIL, 300, 100)

    resp = client.post(
        f"/api/features/{feature}/opportunities/apply?period=2026-04",
        json={"lever": "model_rightsizing", "projected_monthly": 1_000_000},
    )
    assert resp.status_code == 200, resp.text
    p = resp.json()["prediction"]
    assert p["monthly"] < 300 and p["reduction"] < 1


def test_the_demo_shows_a_prediction_held_against_the_bill(tenant_id, app_env):
    from meter.sampledata import DEFAULT_PERIOD, insert_sample_data

    insert_sample_data(app_env, tenant_id, extended=True)
    app_env.commit()
    overview = optimize_measured.copilot_overview(tenant_id, DEFAULT_PERIOD)
    phishing = next(a for a in overview["applied"] if a["feature_name"] == "Phishing detection")
    assert phishing["prediction"]["reduction"] == pytest.approx(0.18)
    assert 0.8 < phishing["outcome"]["delivered"] < 0.9
    cal = overview["calibration"]
    assert (cal["count"], cal["unpredicted"]) == (1, 1)  # triage's older change kept no prediction
