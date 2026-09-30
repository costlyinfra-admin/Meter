"""Forecasting: the open month, the next three, and what is driving them.

The numbers here are the kind a CFO plans a quarter on, so every test pins an
exact figure worked out by hand rather than a shape, and the reasoning behind
each figure is in the test.
"""

from __future__ import annotations

import datetime as dt
import math

import pytest
from fastapi.testclient import TestClient
from meter import features, forecast
from meter.api import create_app
from meter.db import admin_dsn, app_dsn, connect, tenant_tx

GOOD_PASSWORD = "correct horse battery"
AS_OF = dt.date(2026, 5, 10)  # 10 of May's 31 days observed
MAY = dt.date(2026, 5, 1)
# The six full months before May, oldest first.
HISTORY = [dt.date(2025, 11, 1), dt.date(2025, 12, 1), dt.date(2026, 1, 1),
           dt.date(2026, 2, 1), dt.date(2026, 3, 1), dt.date(2026, 4, 1)]


@pytest.fixture
def client(admin_conn, admin_conninfo, app_conninfo, monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "unit-test-secret-key")
    monkeypatch.setenv("DATABASE_URL", admin_conninfo)
    monkeypatch.setenv("DATABASE_APP_URL", app_conninfo)
    return TestClient(create_app())


def _tenant(client, as_of=AS_OF) -> str:
    resp = client.post(
        "/api/auth/signup", json={"email": "cfo@acme.com", "password": GOOD_PASSWORD}
    )
    tenant = resp.json()["tenant_id"]
    with connect(admin_dsn()) as conn, conn.transaction():
        conn.execute("UPDATE tenant SET demo_as_of = %s WHERE id = %s", (as_of, tenant))
    return tenant


def _cost(tenant, period, amount, *, feature=None, source="cost_api", environment=None,
          provider="anthropic"):
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant):
        conn.execute(
            """
            INSERT INTO inference_cost (tenant_id, feature_id, provider, period, amount,
                                        source, confidence, environment)
            VALUES (%s, %s, %s, %s, %s, %s, 'high', %s)
            """,
            (tenant, feature, provider, period, amount, source, environment),
        )


def _daily(tenant, day, amount, *, feature=None, environment="production"):
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant):
        conn.execute(
            """
            INSERT INTO inference_cost_daily (tenant_id, feature_id, provider, model, amount,
                                              day, environment, source, confidence)
            VALUES (%s, %s, 'anthropic', 'claude-sonnet-4-6', %s, %s, %s, 'cost_api', 'high')
            """,
            (tenant, feature, amount, day, environment),
        )


def _build(tenant, period, amount):
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant):
        conn.execute(
            """
            INSERT INTO build_cost (tenant_id, feature_id, tool, period, amount, source,
                                    confidence)
            VALUES (%s, NULL, 'cursor', %s, %s, 'seat_allocation', 'high')
            """,
            (tenant, period, amount),
        )


def _budget(client, amount=10_000):
    assert client.put(
        "/api/budget",
        json={"amount": amount, "cadence": "monthly", "effective_from": "2020-01-01"},
    ).status_code == 200


# ---------------------------------------------------------------------------
# The open month
# ---------------------------------------------------------------------------
def test_the_open_month_is_projected_with_a_range_from_daily_variation(client):
    tenant = _tenant(client)
    # Alternating $80 / $120: an average of $100 a day with real spread.
    days = [80.0 if d % 2 else 120.0 for d in range(1, 11)]
    for d, amount in enumerate(days, start=1):
        _daily(tenant, dt.date(2026, 5, d), amount)
    _cost(tenant, MAY, sum(days))

    got = forecast.forecast(tenant)["open_month"]["inference"]
    # Recent-weighted: last 7 days average 100-ish, month average 100. 21 days
    # remain. The exact rate is budgets._run_rate's; here it is $100 either way.
    last7 = days[-7:]
    rate = (sum(last7) / 7) * 0.7 + (sum(days) / 10) * 0.3
    assert got["projected"] == pytest.approx(1000 + rate * 21, abs=0.02)
    # 80% range: 1.2816 x sample stdev of the daily figures x sqrt(21 days).
    mean = sum(days) / len(days)
    sigma = math.sqrt(sum((x - mean) ** 2 for x in days) / (len(days) - 1))
    band = 1.2816 * sigma * math.sqrt(21)
    assert got["high"] - got["projected"] == pytest.approx(band, abs=0.05)
    assert got["low"] < got["projected"] < got["high"]
    assert got["confidence"] == "medium"  # 10 of 31 days: past a week, under half


def test_an_sdk_only_month_is_projected_pro_rata_rather_than_called_insufficient(client):
    """The SDK writes monthly rows only, never daily ones.

    The old forecast looked only at daily rows, so a tenant metered by the SDK
    alone got "insufficient" every month however much it had spent.
    """
    tenant = _tenant(client)
    _cost(tenant, MAY, 1000, source="hook")  # $1,000 across 10 days, no daily rows

    got = forecast.forecast(tenant)["open_month"]["inference"]
    assert got["projected"] == pytest.approx(1000 * 31 / 10, abs=0.01)  # $3,100
    assert got["method"] == "month_to_date_prorata"
    assert got["confidence"] == "low"
    # No daily spread to draw a range from, and none is invented.
    assert got["low"] is None and got["high"] is None


def test_sdk_spend_beside_a_connector_is_carried_forward_rather_than_dropped(client):
    """A tenant with both: the daily rows explain the connector's spend and
    nothing else. Projecting only them left the SDK's share out of the rest of
    the month entirely."""
    tenant = _tenant(client)
    for d in range(1, 11):
        _daily(tenant, dt.date(2026, 5, d), 100.0)
    _cost(tenant, MAY, 1000, source="cost_api", provider="anthropic")
    _cost(tenant, MAY, 500, source="hook", provider="openai")  # SDK only, no daily

    got = forecast.forecast(tenant)["open_month"]["inference"]
    # $100/day from the daily rows plus $50/day of SDK spend, for 21 more days.
    assert got["projected"] == pytest.approx(1500 + (100 + 50) * 21, abs=0.01)
    assert "prorata" in got["method"]


def test_a_self_hosted_allocation_is_already_the_whole_month(client):
    """compute.allocate books a pool's WHOLE monthly cost, split by usage.

    It does not accrue by the day. Spreading $6,500 found on day 10 over the
    rest of the month reports $20,150 for a pool that costs $6,500 — and the
    demo showed exactly this, inflating a real $6,500 pool by half as much again.
    """
    tenant = _tenant(client)
    _cost(tenant, MAY, 6500, source="self_host", provider="self_hosted")

    got = forecast.forecast(tenant)["open_month"]["inference"]
    assert got["projected"] == pytest.approx(6500, abs=0.01)
    assert got["method"] == "monthly_allocation"


def test_one_providers_surplus_does_not_cancel_anothers_missing_days(client):
    """Projected per provider, because summing first hides errors inside the sum.

    Here the connector's daily rows run AHEAD of its monthly figure, and a
    second provider reports no daily rows at all. An org-wide "monthly minus
    daily" left nothing to carry forward, so the second provider's spend for
    the rest of the month vanished — the same shape the demo data has.
    """
    tenant = _tenant(client)
    for d in range(1, 11):
        _daily(tenant, dt.date(2026, 5, d), 150.0)  # $1,500 in daily rows...
    _cost(tenant, MAY, 1000, provider="anthropic")  # ...against a $1,000 month
    _cost(tenant, MAY, 500, provider="openai")  # no daily rows at all

    got = forecast.forecast(tenant)["open_month"]["inference"]
    # anthropic: $1,000 actual + $150/day x 21. openai: $500 x 31/10 = $1,550.
    assert got["projected"] == pytest.approx(1000 + 150 * 21 + 1550, abs=0.01)


def test_mostly_carried_forward_spend_is_low_confidence(client):
    """A month mostly projected from monthly totals has no daily shape behind it,
    however many daily rows a small connector reported."""
    tenant = _tenant(client, as_of=dt.date(2026, 5, 20))
    for d in range(1, 21):
        _daily(tenant, dt.date(2026, 5, d), 10.0)
    _cost(tenant, MAY, 200, provider="anthropic")  # daily, but small
    _cost(tenant, MAY, 5000, source="hook", provider="openai")  # the bulk, monthly only

    got = forecast.forecast(tenant)["open_month"]["inference"]
    assert got["confidence"] == "low"


def test_the_open_month_counts_one_dollar_once_and_ignores_ignored_spend(client):
    tenant = _tenant(client)
    for d in range(1, 11):
        _daily(tenant, dt.date(2026, 5, d), 100.0)
        _daily(tenant, dt.date(2026, 5, d), 900.0, environment="ignore")
    _cost(tenant, MAY, 1000, source="cost_api")
    _cost(tenant, MAY, 1000, source="hook")  # the same calls, as the SDK saw them
    _cost(tenant, MAY, 9000, environment="ignore")

    got = forecast.forecast(tenant)["open_month"]["inference"]
    assert got["actual"] == 1000.0
    assert got["projected"] == pytest.approx(1000 + 100 * 21, abs=0.01)


def test_build_cost_is_reported_not_projected_and_never_folded_into_inference(client):
    """Build is billed per month with no day resolution (invariant 2 keeps it
    apart anyway). The open month shows its actual, and only a field that says
    it is a total adds the two."""
    tenant = _tenant(client)
    for d in range(1, 11):
        _daily(tenant, dt.date(2026, 5, d), 100.0)
    _cost(tenant, MAY, 1000)
    _build(tenant, MAY, 700)

    om = forecast.forecast(tenant)["open_month"]
    assert om["build"] == {"actual": 700.0}
    assert om["inference"]["projected"] == pytest.approx(3100, abs=0.01)  # untouched
    assert om["total_projected"] == pytest.approx(3800, abs=0.01)


# ---------------------------------------------------------------------------
# The next three months
# ---------------------------------------------------------------------------
def test_a_straight_line_of_history_projects_the_next_three_months_along_it(client):
    tenant = _tenant(client)
    for i, month in enumerate(HISTORY):
        _cost(tenant, month, 100 * (i + 1))  # $100, $200, ... $600

    got = forecast.forecast(tenant)
    assert got["horizon"]["status"] == "ok"
    assert got["horizon"]["history_months"] == 6
    # Nov=0 ... Apr=5 is y = 100 + 100x; May is x=6, so Jun/Jul/Aug are 7, 8, 9.
    # Exactly on the line, so the residual spread is zero and so is the range.
    proj = [m["inference"]["projected"] for m in got["months"]]
    assert proj == pytest.approx([800, 900, 1000], abs=0.01)
    assert [m["month"] for m in got["months"]] == ["2026-06-01", "2026-07-01", "2026-08-01"]
    assert all(m["inference"]["low"] == m["inference"]["high"] for m in got["months"])


def test_the_range_widens_the_further_ahead_it_looks(client):
    tenant = _tenant(client)
    for month, amount in zip(HISTORY, [900, 1100, 950, 1050, 1000, 1000]):
        _cost(tenant, month, amount)

    months = forecast.forecast(tenant)["months"]
    width = [m["inference"]["high"] - m["inference"]["low"] for m in months]
    assert width[0] < width[1] < width[2]
    # With the square root of the distance: three months out is sqrt(3) as wide.
    assert width[2] / width[0] == pytest.approx(math.sqrt(3), rel=0.01)


def test_fewer_than_three_months_of_history_is_insufficient_not_a_guess(client):
    tenant = _tenant(client)
    _cost(tenant, HISTORY[-2], 900)
    _cost(tenant, HISTORY[-1], 1000)

    got = forecast.forecast(tenant)
    assert got["horizon"]["status"] == "insufficient"
    assert all(m["inference"]["projected"] is None for m in got["months"])
    assert all(m["total_projected"] is None for m in got["months"])


def test_months_before_the_first_spend_are_not_zeros_in_the_trend(client):
    """A tenant that joined three months ago did not spend nothing for the three
    before that — it did not exist. Fitting through those zeros invents a steep
    growth rate out of an account being opened."""
    tenant = _tenant(client)
    for month in HISTORY[-3:]:
        _cost(tenant, month, 1000)  # flat from the first month it had any spend

    got = forecast.forecast(tenant)
    assert got["horizon"]["history_months"] == 3
    proj = [m["inference"]["projected"] for m in got["months"]]
    assert proj == pytest.approx([1000, 1000, 1000], abs=0.01)


def test_a_month_running_hot_carries_into_the_next_ones(client):
    """History is flat at $1,000; this month is on course for three times that.

    Fitting the full months alone would say next month is $1,000 again, which is
    the one thing the open month says it will not be.
    """
    tenant = _tenant(client, as_of=dt.date(2026, 5, 20))  # 20 of 31: high confidence
    for month in HISTORY:
        _cost(tenant, month, 1000)
    for d in range(1, 21):
        _daily(tenant, dt.date(2026, 5, d), 150.0)
    _cost(tenant, MAY, 3000)

    got = forecast.forecast(tenant)
    assert got["open_month"]["inference"]["confidence"] == "high"
    assert got["horizon"]["carried_open_month"] is True
    assert got["months"][0]["inference"]["projected"] > 1000


def test_a_shaky_open_month_does_not_steer_the_trend(client):
    """Two days is not enough to tell a quarter where it is going.

    budgets._confidence calls anything under three days `low`, and a low-
    confidence month stays out of the fit.
    """
    tenant = _tenant(client, as_of=dt.date(2026, 5, 2))
    for month in HISTORY:
        _cost(tenant, month, 1000)
    for d in range(1, 3):
        _daily(tenant, dt.date(2026, 5, d), 500.0)
    _cost(tenant, MAY, 1000)

    got = forecast.forecast(tenant)
    assert got["open_month"]["inference"]["confidence"] == "low"
    assert got["horizon"]["carried_open_month"] is False
    assert got["months"][0]["inference"]["projected"] == pytest.approx(1000, abs=0.01)


def test_build_and_inference_are_projected_separately(client):
    tenant = _tenant(client)
    for i, month in enumerate(HISTORY):
        _cost(tenant, month, 1000)
        _build(tenant, month, 100 * (i + 1))

    m = forecast.forecast(tenant)["months"][0]
    assert m["inference"]["projected"] == pytest.approx(1000, abs=0.01)
    assert m["build"]["projected"] == pytest.approx(800, abs=0.01)
    assert m["total_projected"] == pytest.approx(1800, abs=0.01)


# ---------------------------------------------------------------------------
# Drivers
# ---------------------------------------------------------------------------
def test_drivers_rank_the_biggest_projected_increase_first_and_keep_unattributed(client):
    tenant = _tenant(client)
    # Named so the ranking by change is NOT also alphabetical — otherwise a
    # sort by name would pass this test and prove nothing.
    triage = features.add_feature(tenant, "Threat triage")["id"]
    summary = features.add_feature(tenant, "Alert summarizer")["id"]
    # April: triage $1,000, summarizer $1,000, unattributed $500.
    _cost(tenant, HISTORY[-1], 1000, feature=triage)
    _cost(tenant, HISTORY[-1], 1000, feature=summary)
    _cost(tenant, HISTORY[-1], 500)
    # May so far: triage running at $100/day, summarizer at $20/day,
    # unattributed at $50/day.
    for d in range(1, 11):
        _daily(tenant, dt.date(2026, 5, d), 100.0, feature=triage)
        _daily(tenant, dt.date(2026, 5, d), 20.0, feature=summary)
        _daily(tenant, dt.date(2026, 5, d), 50.0)
    _cost(tenant, MAY, 1000, feature=triage)
    _cost(tenant, MAY, 200, feature=summary)
    _cost(tenant, MAY, 500)

    drivers = forecast.forecast(tenant)["drivers"]
    names = [d["name"] for d in drivers]
    # triage -> $3,100 (+2,100); unattributed -> $1,550 (+1,050);
    # summarizer -> $620 (-380). Biggest increase first.
    assert names == ["Threat triage", "Unattributed", "Alert summarizer"]
    assert drivers[0]["change"] == pytest.approx(2100, abs=0.01)
    assert drivers[0]["change_pct"] == pytest.approx(210.0, abs=0.1)
    # Unattributed is a row, never dropped or folded into "other" (invariant 4).
    assert drivers[1]["feature_id"] is None


def test_a_driver_carries_the_savings_optimize_has_already_found_on_it(client, monkeypatch):
    tenant = _tenant(client)
    triage = features.add_feature(tenant, "AI threat triage")["id"]
    for d in range(1, 11):
        _daily(tenant, dt.date(2026, 5, d), 100.0, feature=triage)
    _cost(tenant, MAY, 1000, feature=triage)

    from meter import optimize_measured

    monkeypatch.setattr(
        optimize_measured,
        "copilot_overview",
        lambda *_a, **_k: {
            "by_feature": [{"feature_id": triage, "measured": 120.0, "modeled_ceiling": 80.0}]
        },
    )
    driver = forecast.forecast(tenant)["drivers"][0]
    # Measured plus modeled ceiling — the same figure the Optimize screen shows.
    assert driver["identified_savings"] == 200.0


def test_a_failing_optimizer_does_not_take_the_forecast_down(client, monkeypatch):
    tenant = _tenant(client)
    _cost(tenant, MAY, 1000)
    from meter import optimize_measured

    def boom(*_a, **_k):
        raise RuntimeError("optimizer down")

    monkeypatch.setattr(optimize_measured, "copilot_overview", boom)
    got = forecast.forecast(tenant)
    assert got["open_month"]["inference"]["projected"] is not None


# ---------------------------------------------------------------------------
# Budget and the endpoint
# ---------------------------------------------------------------------------
def test_projected_budget_share_is_computed_against_the_total(client):
    tenant = _tenant(client)
    _budget(client, 5000)
    for d in range(1, 11):
        _daily(tenant, dt.date(2026, 5, d), 100.0)
    _cost(tenant, MAY, 1000)
    _build(tenant, MAY, 900)

    om = forecast.forecast(tenant)["open_month"]
    # $3,100 inference + $900 build = $4,000 of a $5,000 month.
    assert om["budget"] == 5000.0
    assert om["projected_budget_pct"] == pytest.approx(80.0, abs=0.1)


def test_no_budget_is_reported_as_none_rather_than_a_default(client):
    tenant = _tenant(client)
    _cost(tenant, MAY, 1000)
    got = forecast.forecast(tenant)
    assert got["has_budget"] is False
    assert got["open_month"]["budget"] is None
    assert got["open_month"]["projected_budget_pct"] is None


def test_the_endpoint_needs_a_session_and_serves_the_caller_only(
    client, admin_conninfo, app_conninfo, monkeypatch
):
    tenant = _tenant(client)
    _cost(tenant, MAY, 1000)
    body = client.get("/api/forecast").json()
    assert body["open_month"]["inference"]["actual"] == 1000.0

    anonymous = TestClient(create_app())
    assert anonymous.get("/api/forecast").status_code == 401


def test_the_history_behind_the_trend_is_returned_split_and_reconciled(client):
    """A chart draws what happened before the line it projects, so the months
    come back too — and on the same basis as everything else."""
    tenant = _tenant(client)
    _cost(tenant, HISTORY[-1], 1000, source="cost_api")
    _cost(tenant, HISTORY[-1], 1000, source="hook")  # the same April, twice
    _build(tenant, HISTORY[-1], 300)

    history = forecast.forecast(tenant)["history"]
    assert [h["month"] for h in history] == [m.isoformat() for m in HISTORY]
    assert history[-1] == {"month": "2026-04-01", "inference": 1000.0, "build": 300.0}


def test_a_known_allocation_does_not_make_the_rest_of_the_month_look_unsure(client):
    """A self-hosted pool is the whole month, known exactly.

    Counting it against the daily-observed share meant a tenant with a large
    pool and a well-observed connector was told its forecast was low
    confidence — about a figure that is mostly certain.
    """
    tenant = _tenant(client, as_of=dt.date(2026, 5, 20))
    for d in range(1, 21):
        _daily(tenant, dt.date(2026, 5, d), 100.0)
    _cost(tenant, MAY, 2000, provider="anthropic")  # observed daily, all of it
    _cost(tenant, MAY, 9000, source="self_host", provider="self_hosted")  # a big pool

    got = forecast.forecast(tenant)["open_month"]["inference"]
    assert got["confidence"] == "high"
