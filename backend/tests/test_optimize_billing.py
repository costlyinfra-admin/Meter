"""Billing-only optimization rules — evidence in, recommendation out; nothing invented.

Each rule is exercised for: a qualifying case, missing data, zero spend, an
already-resolved condition, and (where relevant) an incomplete comparison period.
The final block asserts the module NEVER emits SDK-only conclusions.
"""

from __future__ import annotations

import datetime as dt

import pytest
from meter import optimize_billing as ob

MAY = dt.date(2026, 5, 1)
APR = dt.date(2026, 4, 1)
JUN = dt.date(2026, 6, 1)  # "today" lives here, so May/Apr are the complete months


def _inf(
    app_env,
    tenant_id,
    amount,
    *,
    period=MAY,
    env=None,
    feature_id=None,
    key="k1",
    key_name="prod-key",
    ws="ws1",
    provider="anthropic",
):
    app_env.execute(
        """
        INSERT INTO inference_cost
            (tenant_id, feature_id, provider, model, amount, period, environment,
             workspace_id, workspace_name, api_key_id, api_key_name, source, confidence)
        VALUES (%s, %s, %s, 'm', %s, %s, %s, %s, 'Prod WS', %s, %s, 'cost_api', 'high')
        """,
        (tenant_id, feature_id, provider, amount, period, env, ws, key, key_name),
    )
    app_env.commit()


def _build(app_env, tenant_id, amount, *, period=MAY, feature_id=None):
    app_env.execute(
        """
        INSERT INTO build_cost (tenant_id, feature_id, developer_id, tool, amount,
                                period, confidence, source)
        VALUES (%s, %s, 'dev', 'cursor', %s, %s, 'low', 'coding_tool+github')
        """,
        (tenant_id, feature_id, amount, period),
    )
    app_env.commit()


def _alert(app_env, tenant_id, *, metric="inference_cost", enabled=True):
    app_env.execute(
        """
        INSERT INTO alert_rule (tenant_id, name, metric, condition_type, threshold,
                                "window", enabled)
        VALUES (%s, 'Budget', %s, 'exceeds', 100, 'monthly', %s)
        """,
        (tenant_id, metric, enabled),
    )
    app_env.commit()


def _run(tenant_id, start=MAY, end=MAY, today=JUN):
    return ob.billing_opportunities(tenant_id, start, end, today=today)


def _types(opps):
    return {o["type"] for o in opps}


# ---- Rule 1: unclassified spend -------------------------------------------
def test_unclassified_spend_is_reported_as_spend_to_review(tenant_id, app_env):
    _inf(app_env, tenant_id, 500, env=None)
    (o,) = [x for x in _run(tenant_id) if x["type"] == "unclassified_spend"]
    assert o["evidence"]["observed_cost"] == 500.0
    assert o["evidence"]["resource_id"] == "k1"
    assert o["confidence"] == "high"
    # Spend under review is NEVER savings.
    assert o["impact"] == {"kind": "spend_to_review", "amount": 500.0}
    assert o["savings"] == {
        "kind": "not_quantified",
        "amount": None,
        "explanation": o["savings"]["explanation"],
    }
    assert o["action"]["href"] == "/cost-sources"


def test_unclassified_disappears_once_the_resource_is_classified(tenant_id, app_env):
    _inf(app_env, tenant_id, 500, env="production")
    assert "unclassified_spend" not in _types(_run(tenant_id))


def test_unclassified_ignores_dust(tenant_id, app_env):
    _inf(app_env, tenant_id, 0.10, env=None)
    assert "unclassified_spend" not in _types(_run(tenant_id))


def test_unclassified_needs_a_resource_identifier(tenant_id, app_env):
    # No workspace/api-key identity -> nothing to classify, so nothing is claimed.
    app_env.execute(
        """
        INSERT INTO inference_cost (tenant_id, provider, model, amount, period, source, confidence)
        VALUES (%s, 'together', 'm', 900, %s, 'cost_api', 'low')
        """,
        (tenant_id, MAY),
    )
    app_env.commit()
    assert "unclassified_spend" not in _types(_run(tenant_id))


# ---- Rule 2: unattributed spend -------------------------------------------
def test_unattributed_inference_and_build_are_visibility_not_savings(tenant_id, app_env):
    _inf(app_env, tenant_id, 300, env="production", feature_id=None)
    _build(app_env, tenant_id, 200, feature_id=None)
    opps = {o["type"]: o for o in _run(tenant_id)}
    ids = {o["id"] for o in _run(tenant_id)}
    assert {"unattributed:inference", "unattributed:build"} <= ids
    o = opps["unattributed_spend"]
    assert o["impact"]["kind"] == "visibility"
    assert o["savings"]["kind"] == "not_quantified"


def test_unattributed_gone_when_everything_maps_to_a_feature(tenant_id, app_env, seeded_feature):
    _inf(app_env, tenant_id, 300, env="production", feature_id=seeded_feature)
    assert "unattributed_spend" not in _types(_run(tenant_id))


# ---- Rule 3: development/test + internal ----------------------------------
def test_non_production_spend_is_surfaced_for_review_only(tenant_id, app_env):
    _inf(app_env, tenant_id, 400, env="development")
    (o,) = [x for x in _run(tenant_id) if x["type"] == "non_production_spend"]
    assert o["evidence"]["observed_cost"] == 400.0
    assert o["impact"]["kind"] == "spend_to_review"
    assert o["savings"]["kind"] == "not_quantified"
    # Never asserts the workload is waste.
    assert "waste" not in (o["title"] + o["description"]).lower()


def test_production_spend_is_not_flagged_as_non_production(tenant_id, app_env):
    _inf(app_env, tenant_id, 400, env="production")
    assert "non_production_spend" not in _types(_run(tenant_id))


# ---- Rule 4: concentration -------------------------------------------------
def test_concentration_uses_an_exact_computed_share(tenant_id, app_env):
    _inf(app_env, tenant_id, 900, env="production", key="k1", key_name="big-key")
    _inf(app_env, tenant_id, 100, env="production", key="k2", key_name="small-key")
    (o,) = [x for x in _run(tenant_id) if x["type"] == "cost_concentration"]
    assert o["evidence"]["observed_cost"] == 900.0
    assert "90.0%" in o["evidence"]["calculation"]
    assert o["savings"]["kind"] == "not_quantified"


def test_evenly_spread_spend_is_not_concentrated(tenant_id, app_env):
    for i in range(4):
        _inf(app_env, tenant_id, 250, env="production", key=f"k{i}", key_name=f"key{i}")
    assert "cost_concentration" not in _types(_run(tenant_id))


# ---- Rule 5: growth (complete periods only) -------------------------------
def test_growth_compares_two_complete_months(tenant_id, app_env):
    _inf(app_env, tenant_id, 100, period=APR, env="production")
    _inf(app_env, tenant_id, 400, period=MAY, env="production")
    (o,) = [x for x in _run(tenant_id) if x["type"] == "cost_growth"]
    assert o["confidence"] == "medium"  # depends on period completeness
    assert "300.00 (+300.0%)" in o["evidence"]["calculation"]
    assert o["evidence"]["period_start"] == APR.isoformat()
    assert o["evidence"]["period_end"] == MAY.isoformat()
    assert o["savings"]["kind"] == "not_quantified"


def test_growth_needs_history_the_single_month_case(tenant_id, app_env):
    _inf(app_env, tenant_id, 400, period=MAY, env="production")
    assert "cost_growth" not in _types(_run(tenant_id))


def test_growth_excludes_the_incomplete_current_month(tenant_id, app_env):
    # Spend lands in May and June while "today" is inside June: June is
    # month-to-date, so it must never be compared.
    _inf(app_env, tenant_id, 100, period=MAY, env="production")
    _inf(app_env, tenant_id, 5000, period=JUN, env="production")
    today_in_june = dt.date(2026, 6, 15)
    assert "cost_growth" not in _types(
        ob.billing_opportunities(tenant_id, JUN, JUN, today=today_in_june)
    )


def test_small_growth_is_not_surfaced(tenant_id, app_env):
    _inf(app_env, tenant_id, 100, period=APR, env="production")
    _inf(app_env, tenant_id, 110, period=MAY, env="production")  # +10%, +$10
    assert "cost_growth" not in _types(_run(tenant_id))


# ---- Rule 6: missing cost control -----------------------------------------
def test_missing_cost_control_when_spend_has_no_alert(tenant_id, app_env):
    _inf(app_env, tenant_id, 700, env="production")
    (o,) = [x for x in _run(tenant_id) if x["type"] == "missing_cost_control"]
    assert o["impact"] == {"kind": "risk_reduction", "amount": None}
    assert o["savings"]["kind"] == "not_quantified"
    assert o["action"]["href"] == "/alerts/new"


def test_missing_cost_control_resolved_by_an_enabled_alert(tenant_id, app_env):
    _inf(app_env, tenant_id, 700, env="production")
    _alert(app_env, tenant_id)
    assert "missing_cost_control" not in _types(_run(tenant_id))


def test_a_disabled_alert_does_not_count_as_a_control(tenant_id, app_env):
    _inf(app_env, tenant_id, 700, env="production")
    _alert(app_env, tenant_id, enabled=False)
    assert "missing_cost_control" in _types(_run(tenant_id))


# ---- Global guarantees -----------------------------------------------------
def test_no_data_yields_no_recommendations(tenant_id):
    assert _run(tenant_id) == []
    assert ob.has_billing_data(tenant_id, MAY, MAY) is False
    assert ob.has_sdk_telemetry(tenant_id, MAY, MAY) is False


def test_never_emits_sdk_only_conclusions(tenant_id, app_env):
    """Billing data alone must never yield model/prompt/cache/user/feature claims."""
    _inf(app_env, tenant_id, 900, env=None, key="k1")
    _inf(app_env, tenant_id, 400, period=APR, env="development", key="k2")
    _build(app_env, tenant_id, 200)
    opps = _run(tenant_id)
    assert opps  # the billing rules DO fire...

    banned = (
        "downgrade",
        "cheaper model",
        "rightsiz",  # model conclusions
        "shorten",
        "compress",
        "prompt",  # prompt conclusions
        "cach",  # caching conclusions
        "duplicate",
        "redundant",  # duplicate-call conclusions
        "per user",
        "per-user",
        "quality",  # user/quality conclusions
    )
    for o in opps:
        blob = f"{o['type']} {o['title']} {o['description']}".lower()
        for word in banned:
            assert word not in blob, f"{o['id']} implies an SDK-only conclusion: {word!r}"
        # No invented percentages-of-spend masquerading as savings.
        assert o["savings"]["kind"] in {"measured", "deterministic", "not_quantified"}
        if o["savings"]["kind"] == "not_quantified":
            assert o["savings"]["amount"] is None
        # Every number is traceable: a source, a window and a calculation.
        ev = o["evidence"]
        assert ev["source"] and ev["calculation"]
        assert ev["period_start"] <= ev["period_end"]
        assert o["confidence"] in {"high", "medium"}  # 'low' is never displayed
        assert o["limitations"]


def test_ranking_is_deterministic_by_observed_spend(tenant_id, app_env):
    _inf(app_env, tenant_id, 50, env="development", key="k_small", key_name="small")
    _inf(app_env, tenant_id, 5000, env=None, key="k_big", key_name="big")
    costs = [o["evidence"]["observed_cost"] or 0 for o in _run(tenant_id)]
    assert costs == sorted(costs, reverse=True)


@pytest.fixture
def seeded_feature(tenant_id, app_env):
    row = app_env.execute(
        "INSERT INTO feature (tenant_id, name, status) VALUES (%s, 'F', 'confirmed') RETURNING id",
        (tenant_id,),
    ).fetchone()
    app_env.commit()
    return row[0]
