"""The bill against what Meter metered.

Two observations of the same spend: a provider's billing API, and the metering
SDK. These tests are mostly about the difference between "these disagree" and
"only one of them exists" — a distinction a variance column cannot express, and
the one a customer without the SDK installed would otherwise see as a 100%
discrepancy on every provider they have.
"""

from __future__ import annotations

import datetime as dt

import pytest
from meter import bill_compare
from meter.db import app_dsn, connect, tenant_tx

PERIOD = dt.date(2026, 5, 1)


def cost(app_env, tenant_id, *, provider="anthropic", source="cost_api", amount=100.0,
         model="claude-sonnet-4-6", workspace=None, day=None):
    app_env.execute(
        """
        INSERT INTO inference_cost
            (tenant_id, provider, model, amount, period, source, confidence, workspace_name)
        VALUES (%s, %s, %s, %s, %s, %s, 'high', %s)
        """,
        (tenant_id, provider, model, amount, PERIOD, source, workspace),
    )
    if day is not None:
        app_env.execute(
            """
            INSERT INTO inference_cost_daily
                (tenant_id, provider, model, amount, day, source, confidence)
            VALUES (%s, %s, %s, %s, %s, %s, 'high')
            """,
            (tenant_id, provider, model, amount, day, source),
        )
    app_env.commit()


def row(result, provider="anthropic"):
    return next(r for r in result["providers"] if r["provider"] == provider)


# ---------------------------------------------------------------------------
# The four states that are not a variance
# ---------------------------------------------------------------------------
def test_agreement_within_tolerance_is_matched(app_env, tenant_id):
    cost(app_env, tenant_id, source="cost_api", amount=100.00)
    cost(app_env, tenant_id, source="hook", amount=99.80)
    assert row(bill_compare.compare(tenant_id, PERIOD))["status"] == bill_compare.MATCHED


def test_a_real_gap_is_a_variance(app_env, tenant_id):
    cost(app_env, tenant_id, source="cost_api", amount=1000.00)
    cost(app_env, tenant_id, source="hook", amount=600.00)
    found = row(bill_compare.compare(tenant_id, PERIOD))
    assert found["status"] == bill_compare.VARIANCE
    assert found["variance"] == 400.0
    assert found["variance_pct"] == 40.0


def test_a_bill_with_nothing_metered_is_not_called_a_variance(app_env, tenant_id):
    # The overwhelmingly common state before the SDK is installed. Reporting it
    # as "100% variance" would tell every new customer their bill is wrong.
    cost(app_env, tenant_id, source="cost_api", amount=800.00)
    found = row(bill_compare.compare(tenant_id, PERIOD))
    assert found["status"] == bill_compare.NO_METERED_DATA
    assert found["tracked"] == 0.0


def test_a_connected_provider_with_no_billing_data_says_so(app_env, tenant_id, monkeypatch):
    monkeypatch.setattr(
        bill_compare.credentials,
        "connector_statuses",
        lambda _t: [{"type": "openai", "name": "OpenAI", "category": "inference",
                     "connected": True, "credential_set_at": None, "credential_count": 1}],
    )
    found = row(bill_compare.compare(tenant_id, PERIOD), "openai")
    assert found["status"] == bill_compare.BILLING_ACCESS_REQUIRED
    assert found["connected"] is True


def test_metered_spend_with_no_billing_api_behind_it_is_not_supported(app_env, tenant_id):
    # A self-hosted model, or a provider outside the connector list. There is
    # no bill to check it against, and saying "variance" would imply there is.
    cost(app_env, tenant_id, provider="ollama-local", source="hook", amount=40.00)
    found = row(bill_compare.compare(tenant_id, PERIOD), "ollama-local")
    assert found["status"] == bill_compare.NOT_SUPPORTED
    assert found["supported"] is False


# ---------------------------------------------------------------------------
# Tolerance
# ---------------------------------------------------------------------------
def test_a_large_bill_is_not_in_trouble_over_eighty_cents(app_env, tenant_id):
    cost(app_env, tenant_id, source="cost_api", amount=40_000.00)
    cost(app_env, tenant_id, source="hook", amount=39_999.20)
    assert row(bill_compare.compare(tenant_id, PERIOD))["status"] == bill_compare.MATCHED


def test_a_small_bill_is_judged_in_dollars_not_percent(app_env, tenant_id):
    # 10 cents out of $2 is 5% — inside no percentage tolerance, and plainly
    # not a problem. The absolute floor is what saves it.
    cost(app_env, tenant_id, source="cost_api", amount=2.00)
    cost(app_env, tenant_id, source="hook", amount=1.90)
    assert row(bill_compare.compare(tenant_id, PERIOD))["status"] == bill_compare.MATCHED


def test_the_tolerance_matches_the_one_the_nightly_snapshot_uses(app_env):
    # hook.reconcile stores a status on every ingest; this screen must not
    # disagree with it about the same numbers.
    from meter import hook

    assert bill_compare.TOLERANCE == hook._DEFAULT_TOLERANCE


# ---------------------------------------------------------------------------
# Honesty about what the numbers are
# ---------------------------------------------------------------------------
def test_an_open_month_is_marked_estimated(app_env, tenant_id):
    # Anthropic's not-yet-billed figure. A variance against it is not yet a
    # discrepancy, because the bill can still move.
    cost(app_env, tenant_id, source="cost_api_est", amount=500.00)
    cost(app_env, tenant_id, source="hook", amount=400.00)
    assert row(bill_compare.compare(tenant_id, PERIOD))["estimated"] is True


def test_billing_freshness_comes_from_the_rows_not_the_clock(app_env, tenant_id):
    cost(app_env, tenant_id, source="cost_api", amount=100.00)
    found = row(bill_compare.compare(tenant_id, PERIOD))
    assert found["billing_updated_at"] is not None


def test_mixed_currencies_are_flagged_rather_than_summed(app_env, tenant_id):
    cost(app_env, tenant_id, source="cost_api", amount=100.00)
    app_env.execute(
        "INSERT INTO inference_cost (tenant_id, provider, model, amount, period, source,"
        " confidence, currency) VALUES (%s,'anthropic','m',50,%s,'cost_api','high','EUR')",
        (tenant_id, PERIOD),
    )
    app_env.commit()
    assert row(bill_compare.compare(tenant_id, PERIOD))["mixed_currency"] is True


def test_a_percentage_of_nothing_is_not_reported_as_zero(app_env, tenant_id):
    cost(app_env, tenant_id, source="hook", amount=25.00)
    assert row(bill_compare.compare(tenant_id, PERIOD))["variance_pct"] is None


# ---------------------------------------------------------------------------
# The breakdown, and what it refuses to pretend
# ---------------------------------------------------------------------------
def test_the_model_breakdown_compares_both_sides(app_env, tenant_id):
    cost(app_env, tenant_id, source="cost_api", amount=600.00, model="claude-opus-5")
    cost(app_env, tenant_id, source="hook", amount=400.00, model="claude-opus-5")
    cost(app_env, tenant_id, source="cost_api", amount=100.00, model="claude-haiku-4-5")
    cost(app_env, tenant_id, source="hook", amount=100.00, model="claude-haiku-4-5")

    detail = bill_compare.breakdown(tenant_id, "anthropic", PERIOD)
    opus = next(m for m in detail["by_model"] if m["model"] == "claude-opus-5")
    assert (opus["provider_reported"], opus["tracked"], opus["variance"]) == (600.0, 400.0, 200.0)
    haiku = next(m for m in detail["by_model"] if m["model"] == "claude-haiku-4-5")
    assert haiku["variance"] == 0.0


def test_day_and_account_are_labelled_provider_only(app_env, tenant_id):
    # inference_cost_daily holds connector rows and the hook writes none, and
    # hook rows carry no workspace. A variance column on either would be half a
    # comparison presented as a whole one.
    cost(app_env, tenant_id, source="cost_api", amount=100.00, workspace="Production",
         day=dt.date(2026, 5, 3))
    detail = bill_compare.breakdown(tenant_id, "anthropic", PERIOD)

    assert "by_day" not in detail and "by_account" not in detail
    assert detail["provider_only"]["by_day"][0]["day"] == "2026-05-03"
    assert detail["provider_only"]["by_account"][0]["account"] == "Production"
    for slice_ in (detail["provider_only"]["by_day"], detail["provider_only"]["by_account"]):
        for entry in slice_:
            assert "tracked" not in entry and "variance" not in entry


# ---------------------------------------------------------------------------
# It must not touch what it reads
# ---------------------------------------------------------------------------
def test_comparing_changes_no_tracked_cost(app_env, tenant_id):
    cost(app_env, tenant_id, source="cost_api", amount=1000.00)
    cost(app_env, tenant_id, source="hook", amount=600.00)

    def snapshot():
        with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
            return conn.execute(
                "SELECT id, amount, source, updated_at FROM inference_cost ORDER BY id"
            ).fetchall()

    before = snapshot()
    bill_compare.compare(tenant_id, PERIOD)
    bill_compare.breakdown(tenant_id, "anthropic", PERIOD)
    assert snapshot() == before


def test_it_writes_no_snapshot_row_of_its_own(app_env, tenant_id):
    cost(app_env, tenant_id, source="cost_api", amount=100.00)
    bill_compare.compare(tenant_id, PERIOD)
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        assert conn.execute("SELECT count(*) FROM bill_reconciliation").fetchone()[0] == 0


def test_one_tenant_cannot_see_another_bill(app_env, tenant_id):
    other = str(
        app_env.execute("INSERT INTO tenant (name) VALUES ('Other') RETURNING id").fetchone()[0]
    )
    cost(app_env, other, source="cost_api", amount=9999.00)
    app_env.commit()
    assert bill_compare.compare(tenant_id, PERIOD)["providers"] == []


@pytest.mark.parametrize("period", [None, PERIOD])
def test_the_period_defaults_to_the_latest_month_with_cost(app_env, tenant_id, period):
    cost(app_env, tenant_id, source="cost_api", amount=100.00)
    assert bill_compare.compare(tenant_id, period)["period"] == PERIOD.isoformat()
