"""The organization budget: storage, proration, forecasting, and the demo as-of date.

Proration is tested against hand-computed day counts rather than against the
implementation's own arithmetic — the point of these is to catch a leap year or
an effective date being silently wrong, which a self-consistent test would miss.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from meter import budgets
from meter.api import create_app
from meter.db import admin_dsn, app_dsn, connect, tenant_tx

GOOD_PASSWORD = "correct horse battery"


@pytest.fixture
def client(admin_conn, admin_conninfo, app_conninfo, monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "unit-test-secret-key")
    monkeypatch.setenv("DATABASE_URL", admin_conninfo)
    monkeypatch.setenv("DATABASE_APP_URL", app_conninfo)
    return TestClient(create_app())


def _signup(client, email="cto@acme.com"):
    resp = client.post("/api/auth/signup", json={"email": email, "password": GOOD_PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _set_as_of(tenant_id: str, day: dt.date | None) -> None:
    """Pin the tenant's "today" the way the demo seed does."""
    with connect(admin_dsn()) as conn, conn.transaction():
        conn.execute("UPDATE tenant SET demo_as_of = %s WHERE id = %s", (day, tenant_id))


def _add_daily(tenant_id: str, day: dt.date, amount: float) -> None:
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        conn.execute(
            """
            INSERT INTO inference_cost_daily (tenant_id, feature_id, provider, model, amount,
                                              day, environment, source, confidence)
            VALUES (%s, NULL, 'anthropic', 'claude-sonnet-4-6', %s, %s, 'production',
                    'cost_api', 'high')
            """,
            (tenant_id, amount, day),
        )


def _add_monthly(tenant_id: str, period: dt.date, inference: float, build: float = 0.0) -> None:
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        if inference:
            conn.execute(
                """
                INSERT INTO inference_cost (tenant_id, feature_id, provider, period, amount,
                                            source, confidence)
                VALUES (%s, NULL, 'anthropic', %s, %s, 'cost_api', 'high')
                """,
                (tenant_id, period, inference),
            )
        if build:
            conn.execute(
                """
                INSERT INTO build_cost (tenant_id, feature_id, tool, period, amount,
                                        source, confidence)
                VALUES (%s, NULL, 'cursor', %s, %s, 'seat_allocation', 'high')
                """,
                (tenant_id, period, build),
            )


# ---------------------------------------------------------------------------
# Proration — pure arithmetic, no database
# ---------------------------------------------------------------------------
def _budget(amount, cadence, effective_from="2020-01-01"):
    return {"amount": amount, "cadence": cadence, "effective_from": effective_from}


def test_a_monthly_budget_over_whole_months_is_just_the_months():
    got = budgets.prorate(_budget(10_000, "monthly"), dt.date(2026, 3, 1), dt.date(2026, 5, 1))
    assert got["amount"] == 30_000.0
    assert got["covered_days"] == 31 + 30 + 31
    assert got["fully_covered"] is True
    assert got["method"] == "monthly"


def test_a_monthly_budget_prorates_a_partial_month_by_that_months_own_length():
    # February 2026 has 28 days, so 14 covered days is exactly half a month --
    # dividing by 30 or 31 here would quietly shrink or inflate the budget.
    got = budgets.prorate(
        _budget(2800, "monthly", effective_from="2026-02-15"),
        dt.date(2026, 2, 1),
        dt.date(2026, 2, 1),
    )
    assert got["covered_days"] == 14
    assert got["amount"] == 1400.0
    assert got["fully_covered"] is False


def test_an_annual_budget_divides_by_the_years_own_length():
    got = budgets.prorate(_budget(36_500, "annual"), dt.date(2026, 1, 1), dt.date(2026, 1, 1))
    assert got["covered_days"] == 31
    assert got["amount"] == 3100.0  # 36,500 / 365 * 31


def test_a_leap_year_divides_by_366_not_365():
    # 2028 is a leap year. Same budget, same month, one more day in the divisor.
    leap = budgets.prorate(_budget(36_600, "annual"), dt.date(2028, 2, 1), dt.date(2028, 2, 1))
    assert leap["covered_days"] == 29  # February 2028 has 29 days
    assert leap["amount"] == 2900.0  # 36,600 / 366 * 29

    common = budgets.prorate(_budget(36_600, "annual"), dt.date(2027, 2, 1), dt.date(2027, 2, 1))
    assert common["covered_days"] == 28
    assert common["amount"] == pytest.approx(36_600 / 365 * 28, abs=0.01)


def test_an_annual_budget_spanning_two_years_uses_each_years_own_length():
    # Dec 2027 (365-day year) + Jan 2028 (366-day year).
    got = budgets.prorate(_budget(36_600, "annual"), dt.date(2027, 12, 1), dt.date(2028, 1, 1))
    expected = 36_600 / 365 * 31 + 36_600 / 366 * 31
    assert got["amount"] == pytest.approx(expected, abs=0.01)
    assert got["covered_days"] == 62


def test_days_before_the_effective_date_are_not_budgeted():
    # A budget set in April does not retroactively cover February and March.
    got = budgets.prorate(
        _budget(10_000, "monthly", effective_from="2026-04-01"),
        dt.date(2026, 2, 1),
        dt.date(2026, 4, 1),
    )
    assert got["amount"] == 10_000.0  # April only
    assert got["covered_days"] == 30
    assert got["covered_start"] == "2026-04-01"
    assert got["fully_covered"] is False


def test_a_window_entirely_before_the_effective_date_budgets_nothing():
    got = budgets.prorate(
        _budget(10_000, "monthly", effective_from="2027-01-01"),
        dt.date(2026, 1, 1),
        dt.date(2026, 3, 1),
    )
    assert got["amount"] == 0.0
    assert got["covered_days"] == 0
    assert got["covered_start"] is None


# ---------------------------------------------------------------------------
# CRUD and permissions
# ---------------------------------------------------------------------------
def test_an_organization_has_no_budget_until_someone_sets_one(client):
    _signup(client)
    assert client.get("/api/budget").json()["budget"] is None


def test_setting_reading_and_removing_a_budget(client):
    _signup(client)
    resp = client.put(
        "/api/budget",
        json={"amount": 50_000, "cadence": "monthly", "effective_from": "2026-01-01"},
    )
    assert resp.status_code == 200, resp.text
    saved = resp.json()["budget"]
    assert saved["amount"] == 50_000.0
    assert saved["cadence"] == "monthly"
    assert saved["currency"] == "USD"
    assert saved["updated_by"] == "cto@acme.com"

    # Setting again replaces rather than accumulating -- one budget per org.
    client.put(
        "/api/budget",
        json={"amount": 60_000, "cadence": "annual", "effective_from": "2026-02-01"},
    )
    again = client.get("/api/budget").json()["budget"]
    assert (again["amount"], again["cadence"]) == (60_000.0, "annual")

    assert client.delete("/api/budget").json()["budget"] is None
    assert client.get("/api/budget").json()["budget"] is None


def test_budget_input_is_validated_on_the_server(client):
    _signup(client)
    for bad, why in [
        ({"amount": 0, "cadence": "monthly", "effective_from": "2026-01-01"}, "zero"),
        ({"amount": 100, "cadence": "weekly", "effective_from": "2026-01-01"}, "cadence"),
        ({"amount": 100, "cadence": "monthly", "effective_from": "not-a-date"}, "date"),
    ]:
        resp = client.put("/api/budget", json=bad)
        assert resp.status_code in (400, 422), f"{why}: {resp.status_code} {resp.text}"
    assert client.get("/api/budget").json()["budget"] is None  # nothing was written


def test_a_budget_must_be_in_the_organizations_reporting_currency(client):
    _signup(client)
    resp = client.put(
        "/api/budget",
        json={
            "amount": 100,
            "cadence": "monthly",
            "effective_from": "2026-01-01",
            "currency": "EUR",
        },
    )
    assert resp.status_code == 400
    assert "currency" in resp.json()["detail"].lower()


def test_the_budget_endpoints_require_a_session(client):
    for call in (
        lambda: client.get("/api/budget"),
        lambda: client.put(
            "/api/budget",
            json={"amount": 1, "cadence": "monthly", "effective_from": "2026-01-01"},
        ),
        lambda: client.delete("/api/budget"),
    ):
        assert call().status_code == 401


def test_one_organizations_budget_is_invisible_to_another(client, admin_conninfo, app_conninfo):
    _signup(client, "a@acme.com")
    client.put(
        "/api/budget",
        json={"amount": 12_345, "cadence": "monthly", "effective_from": "2026-01-01"},
    )
    client.post("/api/auth/logout")

    _signup(client, "b@other.com")
    assert client.get("/api/budget").json()["budget"] is None


# ---------------------------------------------------------------------------
# "Today", timezones, and the demo as-of date
# ---------------------------------------------------------------------------
def test_as_of_follows_the_organizations_timezone(client, monkeypatch):
    tenant = _signup(client)["tenant_id"]

    # Late evening UTC is already the next day in Auckland and still the previous
    # day in Los Angeles -- which decides whether a month is open or closed.
    class _FixedDatetime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 5, 31, 23, 30, tzinfo=dt.timezone.utc).astimezone(tz)

    monkeypatch.setattr(budgets.dt, "datetime", _FixedDatetime)

    client.patch("/api/settings", json={"timezone": "Pacific/Auckland"})
    assert budgets.as_of_date(tenant) == (dt.date(2026, 6, 1), False)

    client.patch("/api/settings", json={"timezone": "America/Los_Angeles"})
    assert budgets.as_of_date(tenant) == (dt.date(2026, 5, 31), False)


def test_a_demo_as_of_date_overrides_the_clock_and_says_that_it_did(client):
    tenant = _signup(client)["tenant_id"]
    _set_as_of(tenant, dt.date(2026, 5, 21))
    assert budgets.as_of_date(tenant) == (dt.date(2026, 5, 21), True)


def test_production_never_picks_up_a_demo_date(client):
    # The column is NULL for every tenant nobody has seeded, and that is the only
    # thing standing between production and a fixed date. Assert it explicitly.
    tenant = _signup(client)["tenant_id"]
    with connect(admin_dsn()) as conn:
        row = conn.execute("SELECT demo_as_of FROM tenant WHERE id = %s", (tenant,)).fetchone()
    assert row[0] is None
    day, fixed = budgets.as_of_date(tenant)
    assert fixed is False
    assert day == dt.datetime.now(dt.timezone.utc).date()


# ---------------------------------------------------------------------------
# Forecast
# ---------------------------------------------------------------------------
def test_a_completed_period_reports_final_spend_and_does_not_forecast(client):
    tenant = _signup(client)["tenant_id"]
    _set_as_of(tenant, dt.date(2026, 8, 10))  # well after the window
    _add_monthly(tenant, dt.date(2026, 5, 1), inference=4000, build=1000)

    got = budgets.period_forecast(tenant, dt.date(2026, 5, 1), dt.date(2026, 5, 1))
    assert got["status"] == "closed"
    assert got["actual"] == 5000.0
    assert got["forecast"] == 5000.0  # final, not projected
    assert got["method"] == "closed"
    assert got["confidence"] == "final"


def test_an_open_month_is_projected_from_observed_daily_spend(client):
    tenant = _signup(client)["tenant_id"]
    _set_as_of(tenant, dt.date(2026, 5, 10))  # 10 of May's 31 days elapsed
    for day in range(1, 11):
        _add_daily(tenant, dt.date(2026, 5, day), 100.0)
    _add_monthly(tenant, dt.date(2026, 5, 1), inference=1000)

    got = budgets.period_forecast(tenant, dt.date(2026, 5, 1), dt.date(2026, 5, 1))
    assert got["status"] == "open"
    assert got["observed_days"] == 10
    # Flat $100/day either way, so both run-rate methods agree: 21 days left.
    assert got["method"] == "recent_weighted"
    assert got["forecast"] == pytest.approx(1000 + 100 * 21, abs=0.01)
    assert got["actual"] == 1000.0  # actual is never replaced by the projection


def test_a_short_month_to_date_uses_the_flat_average_rather_than_a_weighted_one(client):
    tenant = _signup(client)["tenant_id"]
    _set_as_of(tenant, dt.date(2026, 5, 3))
    for day in range(1, 4):
        _add_daily(tenant, dt.date(2026, 5, day), 50.0)
    _add_monthly(tenant, dt.date(2026, 5, 1), inference=150)

    got = budgets.period_forecast(tenant, dt.date(2026, 5, 1), dt.date(2026, 5, 1))
    assert got["method"] == "month_to_date_average"
    assert got["confidence"] == "medium"
    assert got["forecast"] == pytest.approx(150 + 50 * 28, abs=0.01)


def test_the_weighted_rate_leans_on_the_recent_days(client):
    tenant = _signup(client)["tenant_id"]
    _set_as_of(tenant, dt.date(2026, 5, 14))
    # A quiet first week, then a much busier second one.
    for day in range(1, 8):
        _add_daily(tenant, dt.date(2026, 5, day), 10.0)
    for day in range(8, 15):
        _add_daily(tenant, dt.date(2026, 5, day), 100.0)
    _add_monthly(tenant, dt.date(2026, 5, 1), inference=770)

    got = budgets.period_forecast(tenant, dt.date(2026, 5, 1), dt.date(2026, 5, 1))
    flat_average = 770 / 14
    assert got["method"] == "recent_weighted"
    # The projection must sit above what the flat average would give, or the
    # weighting is not doing anything.
    assert got["forecast"] > 770 + flat_average * 17


def test_an_open_month_with_no_daily_spend_says_so_rather_than_forecasting_zero(client):
    tenant = _signup(client)["tenant_id"]
    _set_as_of(tenant, dt.date(2026, 5, 10))
    _add_monthly(tenant, dt.date(2026, 4, 1), inference=900)

    got = budgets.period_forecast(tenant, dt.date(2026, 4, 1), dt.date(2026, 5, 1))
    assert got["status"] == "insufficient"
    assert got["forecast"] is None
    assert got["confidence"] == "none"
    assert got["actual"] == 900.0  # what is known is still reported


def test_without_a_budget_the_forecast_reports_no_budget_rather_than_a_default(client):
    tenant = _signup(client)["tenant_id"]
    _set_as_of(tenant, dt.date(2026, 5, 10))
    for day in range(1, 11):
        _add_daily(tenant, dt.date(2026, 5, day), 100.0)
    _add_monthly(tenant, dt.date(2026, 5, 1), inference=1000)

    got = budgets.period_forecast(tenant, dt.date(2026, 5, 1), dt.date(2026, 5, 1))
    assert got["budget"] is None
    assert got["budget_detail"] is None
    assert got["variance"] is None and got["variance_pct"] is None
    assert got["forecast"] is not None  # a forecast still stands on its own


def test_variance_and_optimized_forecast_against_a_real_budget(client):
    tenant = _signup(client)["tenant_id"]
    _set_as_of(tenant, dt.date(2026, 5, 10))
    client.put(
        "/api/budget",
        json={"amount": 3000, "cadence": "monthly", "effective_from": "2026-01-01"},
    )
    for day in range(1, 11):
        _add_daily(tenant, dt.date(2026, 5, day), 100.0)
    _add_monthly(tenant, dt.date(2026, 5, 1), inference=1000)

    got = budgets.period_forecast(
        tenant, dt.date(2026, 5, 1), dt.date(2026, 5, 1), identified_savings=500
    )
    assert got["budget"] == 3000.0
    assert got["forecast"] == pytest.approx(3100.0, abs=0.01)
    assert got["variance"] == pytest.approx(100.0, abs=0.01)
    assert got["variance_pct"] == pytest.approx(3.33, abs=0.01)
    assert got["forecast_optimized"] == pytest.approx(2600.0, abs=0.01)


def test_identified_savings_never_push_a_forecast_below_zero(client):
    tenant = _signup(client)["tenant_id"]
    _set_as_of(tenant, dt.date(2026, 5, 10))
    for day in range(1, 11):
        _add_daily(tenant, dt.date(2026, 5, day), 1.0)
    _add_monthly(tenant, dt.date(2026, 5, 1), inference=10)

    got = budgets.period_forecast(
        tenant, dt.date(2026, 5, 1), dt.date(2026, 5, 1), identified_savings=1_000_000
    )
    assert got["forecast_optimized"] == 0.0


def test_the_forecast_endpoint_follows_the_window_the_overview_is_showing(client):
    tenant = _signup(client)["tenant_id"]
    _set_as_of(tenant, dt.date(2026, 5, 10))
    _add_monthly(tenant, dt.date(2026, 3, 1), inference=100)
    _add_monthly(tenant, dt.date(2026, 4, 1), inference=200)
    _add_monthly(tenant, dt.date(2026, 5, 1), inference=400)

    one = client.get("/api/budget/forecast", params={"range": "this_month"}).json()
    three = client.get("/api/budget/forecast", params={"range": "last_3_months"}).json()
    assert one["actual"] == 400.0
    assert three["actual"] == 700.0
    assert three["window_start"] == "2026-03-01"
    assert three["window_end"] == "2026-05-31"
    assert three["as_of"] == "2026-05-10"
    assert three["as_of_is_fixed"] is True
