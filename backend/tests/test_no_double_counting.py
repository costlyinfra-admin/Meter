"""One dollar, counted once, wherever it is shown.

A provider with both a connector and the metering SDK has described the same
spend twice: the connector reports what it billed, the SDK reports the calls
behind it. `_inference_rollup` has always reconciled that for the feature
totals — but the rule was written inline rather than shared, so every query
added since summed both and doubled the money.

This fixture is the case that exposes it: $1,000 billed by the connector, $600
of it metered by the SDK. Anything that reports $1,600 is counting the same
dollar twice. A second provider is metered only, with no connector at all — its
hook rows are the only record there is and must survive every filter.
"""

from __future__ import annotations

import datetime as dt

import pytest
from meter import dashboard, features

PERIOD = dt.date(2026, 6, 1)
BILLED = 1000.0
METERED = 600.0
#: Self-hosted, or metered before its connector was added. No second observation.
HOOK_ONLY = 250.0


def _cost(app_env, tenant_id, *, provider, source, amount, model="m1", feature_id=None,
          tokens_in=0, tokens_out=0):
    app_env.execute(
        """
        INSERT INTO inference_cost
            (tenant_id, feature_id, provider, model, amount, period, source, confidence,
             tokens_in, tokens_out)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'high', %s, %s)
        """,
        (tenant_id, feature_id, provider, model, amount, PERIOD, source, tokens_in, tokens_out),
    )


@pytest.fixture
def both_sources(tenant_id, app_env):
    """A connector and the SDK on one provider, plus a hook-only provider."""
    feature = features.add_feature(tenant_id, "AI threat triage")["id"]
    _cost(app_env, tenant_id, provider="anthropic", source="cost_api", amount=BILLED,
          tokens_in=10_000_000, tokens_out=1_000_000, feature_id=feature)
    _cost(app_env, tenant_id, provider="anthropic", source="hook", amount=METERED,
          tokens_in=6_000_000, tokens_out=600_000, feature_id=feature)
    _cost(app_env, tenant_id, provider="ollama", source="hook", amount=HOOK_ONLY,
          tokens_in=500_000, tokens_out=50_000, feature_id=feature)
    app_env.commit()
    return tenant_id, feature


TOTAL = BILLED + HOOK_ONLY


def test_the_overview_total_is_the_bill(both_sources):
    tenant_id, _ = both_sources
    assert dashboard.dashboard(tenant_id, PERIOD)["totals"]["inference_cost"] == TOTAL


def test_the_previous_period_total_uses_the_same_basis(tenant_id, app_env):
    # Current and previous were computed differently, so the month-over-month
    # delta on the Overview was wrong for anyone running the SDK.
    _cost(app_env, tenant_id, provider="anthropic", source="cost_api", amount=BILLED)
    _cost(app_env, tenant_id, provider="anthropic", source="hook", amount=METERED)
    app_env.commit()
    later = dt.date(2026, 7, 1)
    data = dashboard.dashboard(tenant_id, later)
    assert data["totals"]["prev_inference_cost"] == BILLED


def test_the_token_totals_are_not_doubled(both_sources):
    tenant_id, _ = both_sources
    totals = dashboard.dashboard(tenant_id, PERIOD)["totals"]
    # Connector tokens for the reconciled provider, hook tokens for the one
    # that has no connector.
    assert totals["tokens_in"] == 10_000_000 + 500_000
    assert totals["tokens_out"] == 1_000_000 + 50_000


def test_the_provider_list_is_not_doubled(both_sources):
    tenant_id, _ = both_sources
    data = dashboard.spend_by_provider(tenant_id, PERIOD)
    by_provider = {p["provider"]: p for p in data["by_provider"]}
    assert by_provider["anthropic"]["amount"] == BILLED
    # The hook-only provider keeps its rows: they are the only record of it.
    assert by_provider["ollama"]["amount"] == HOOK_ONLY


def test_the_model_breakdown_is_not_doubled(both_sources):
    # The failure that started this: $1,250 of gpt-4o reading as $2,500.
    tenant_id, _ = both_sources
    data = dashboard.spend_by_provider(tenant_id, PERIOD)
    anthropic = next(p for p in data["by_provider"] if p["provider"] == "anthropic")
    assert sum(m["amount"] for m in anthropic["by_model"]) == BILLED


def test_the_provider_total_and_its_models_agree(both_sources):
    tenant_id, _ = both_sources
    data = dashboard.spend_by_provider(tenant_id, PERIOD)
    assert data["total"] == TOTAL
    for provider in data["by_provider"]:
        assert sum(m["amount"] for m in provider["by_model"]) == pytest.approx(provider["amount"])


def test_the_token_type_split_sums_to_the_bill(both_sources):
    tenant_id, _ = both_sources
    data = dashboard.spend_by_provider(tenant_id, PERIOD)
    assert sum(t["amount"] for t in data["by_token_type"]) == pytest.approx(TOTAL)


def test_the_environment_trend_sums_to_the_months_total(both_sources):
    # The four classification buckets are documented as summing to the month's
    # active total, which they cannot do on a different basis from it.
    tenant_id, _ = both_sources
    data = dashboard.spend_by_provider(tenant_id, PERIOD)
    point = next(p for p in data["trend"] if p["period"] == PERIOD.isoformat())
    assert point["total"] == pytest.approx(TOTAL)


def test_the_overview_trend_is_not_doubled(both_sources):
    tenant_id, _ = both_sources
    trend = dashboard.dashboard(tenant_id, PERIOD)["trend"]
    point = next(p for p in trend if p["period"] == PERIOD.isoformat())
    assert point["inference_cost"] == pytest.approx(TOTAL)


def test_the_feature_drill_down_is_not_doubled(both_sources):
    tenant_id, feature = both_sources
    detail = dashboard.feature_inference(tenant_id, feature, PERIOD)
    assert detail["total"] == pytest.approx(TOTAL)
    assert sum(m["amount"] for m in detail["by_model"]) == pytest.approx(TOTAL)


def test_the_optimizer_prices_the_bill_not_twice_the_bill(both_sources, app_env):
    # Estimated savings are a share of the month's usage, so a doubled month
    # proposes a doubled saving. Asserted as the property rather than against a
    # figure: the hook row must make no difference, because the connector row
    # beside it already reports that spend.
    from meter.db import app_dsn, connect, tenant_tx

    tenant_id, feature = both_sources
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        with_hook = dashboard.heuristic_optimization(conn, feature, PERIOD)

    app_env.execute(
        "DELETE FROM inference_cost WHERE tenant_id = %s AND source = 'hook' "
        "AND provider = 'anthropic'",
        (tenant_id,),
    )
    app_env.commit()
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        without_hook = dashboard.heuristic_optimization(conn, feature, PERIOD)

    assert with_hook == without_hook
    assert with_hook["monthly_savings"] > 0, "the fixture must produce an estimate to compare"


def test_a_provider_metered_before_its_connector_keeps_its_rows(tenant_id, app_env):
    # The rule drops hook rows only where a connector reports the same spend.
    # Dropping them everywhere would erase self-hosted and pre-connector spend.
    _cost(app_env, tenant_id, provider="ollama", source="hook", amount=HOOK_ONLY)
    app_env.commit()
    assert dashboard.dashboard(tenant_id, PERIOD)["totals"]["inference_cost"] == HOOK_ONLY
