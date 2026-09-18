"""Human effort: parsing a timesheet, and what it may never do to the numbers.

Two things are being protected here. One is the file itself — a labour figure
that is wrong by a factor of ten is worse than no labour figure, so parsing is
strict and an import is all or nothing. The other is everything that was already
on the By Customer screen: metered inference, its coverage of the real bill, and
the reconciliation behind it must come out of this feature untouched.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from meter import dashboard, features, human_effort
from meter.db import app_dsn, connect, tenant_tx
from meter.sampledata import insert_sample_data

PERIOD = dt.date(2026, 5, 1)


@pytest.fixture
def seeded(tenant_id, app_env):
    insert_sample_data(app_env, tenant_id)
    app_env.commit()
    return tenant_id


def _customer(app_env, tenant_id, cid, period, amount, requests):
    app_env.execute(
        "INSERT INTO customer_cost (tenant_id, customer_id, period, amount, request_count) "
        "VALUES (%s, %s, %s, %s, %s)",
        (tenant_id, cid, period, amount, requests),
    )


def _rows(tenant_id):
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        return conn.execute(
            "SELECT work_date, customer_id, person_label, activity_type, hours, "
            "loaded_hourly_rate, source, note, feature_id FROM human_effort "
            "ORDER BY work_date, customer_id"
        ).fetchall()


def csv_of(*lines: str) -> str:
    header = "date,person,customer_id,feature_id,hours,activity_type,loaded_hourly_rate,note"
    return "\n".join((header, *lines)) + "\n"


ONE_ROW = csv_of("2026-05-15,Aman,northwind-financial,,3.5,development,110,Workflow changes")


# --- parsing ---------------------------------------------------------------
def test_a_well_formed_file_parses_into_priced_rows():
    rows = human_effort.parse_csv(
        csv_of(
            "2026-05-15,Aman,northwind-financial,,3.5,development,110,Workflow changes",
            "2026-05-16,Alessio,vertex-health,,2.0,support,125,Deployment support",
        )
    )
    assert [r["customer_id"] for r in rows] == ["northwind-financial", "vertex-health"]
    assert rows[0]["work_date"] == dt.date(2026, 5, 15)
    assert rows[0]["hours"] == Decimal("3.50")
    assert rows[0]["activity_type"] == "development"
    assert rows[0]["note"] == "Workflow changes"
    assert rows[1]["feature_id"] is None


def test_dates_are_read_exactly_and_never_guessed():
    """`infra_csv` infers a date format from the column because a bill's shape
    is not ours to choose. This file is written to a documented shape, so
    guessing between 03/04 readings would risk filing a quarter of somebody's
    labour in the wrong month to save them typing a hyphen."""
    for bad in ("15/05/2026", "05/15/2026", "May 15 2026", "2026-13-01"):
        with pytest.raises(human_effort.EffortImportError) as exc:
            human_effort.parse_csv(csv_of(f"{bad},Aman,acme,,1,support,100,"))
        assert "YYYY-MM-DD" in str(exc.value) or "outside the range" in str(exc.value)


@pytest.mark.parametrize(
    "row,message",
    [
        ("2026-05-15,Aman,acme,,0,support,100,", "greater than 0"),
        ("2026-05-15,Aman,acme,,-2,support,100,", "greater than 0"),
        ("2026-05-15,Aman,acme,,30,support,100,", "not possible"),
        ("2026-05-15,Aman,acme,,2,support,-5,", "cannot be negative"),
        ("2026-05-15,Aman,acme,,2,napping,100,", "activity_type must be one of"),
        ("2026-05-15,Aman,acme,,two,support,100,", "invalid hours"),
        ("2026-05-15,,acme,,2,support,100,", "missing person"),
        ("2026-05-15,Aman,,,2,support,100,", "missing customer_id"),
        ("2026-05-15,Aman,acme,not-a-uuid,2,support,100,", "not a valid id"),
    ],
)
def test_a_bad_row_is_named_by_row_and_reason(row, message):
    with pytest.raises(human_effort.EffortImportError) as exc:
        human_effort.parse_csv(csv_of(row))
    assert "Row 2" in str(exc.value)
    assert message in str(exc.value)


def test_a_zero_rate_is_allowed_because_unpaid_time_is_still_time():
    rows = human_effort.parse_csv(csv_of("2026-05-15,Intern,acme,,4,review,0,"))
    assert rows[0]["loaded_hourly_rate"] == Decimal("0.0000")


def test_a_file_missing_a_required_column_says_which_one():
    with pytest.raises(human_effort.EffortImportError) as exc:
        human_effort.parse_csv("date,person,hours\n2026-05-15,Aman,2\n")
    assert "customer_id" in str(exc.value) and "activity_type" in str(exc.value)


def test_an_empty_or_oversized_file_is_refused():
    with pytest.raises(human_effort.EffortImportError):
        human_effort.parse_csv(csv_of())
    with pytest.raises(human_effort.EffortImportError) as exc:
        human_effort.parse_csv("x" * (human_effort.MAX_CSV_BYTES + 1))
    assert "too large" in str(exc.value)


# --- importing -------------------------------------------------------------
def test_an_import_stores_the_rate_that_priced_each_row(tenant_id, app_env):
    result = human_effort.import_csv(tenant_id, ONE_ROW)
    assert result["imported"] == 1
    assert result["cost"] == 385.0  # 3.5 x 110, in Decimal

    row = _rows(tenant_id)[0]
    assert row[4] == Decimal("3.50") and row[5] == Decimal("110.0000")
    assert row[6] == "csv"


def test_an_unknown_customer_is_accepted(tenant_id, app_env):
    """There is no customer table, so there is nothing to be unknown to. A
    customer whose calls are not instrumented yet is exactly who this is for."""
    human_effort.import_csv(tenant_id, csv_of("2026-05-15,Aman,never-seen-before,,1,other,90,"))
    assert _rows(tenant_id)[0][1] == "never-seen-before"


def test_a_feature_id_must_belong_to_this_tenant(tenant_id, app_env):
    mine = features.add_feature(tenant_id, "AI threat triage")
    other = app_env.execute(
        "INSERT INTO tenant (name) VALUES ('Other Co') RETURNING id"
    ).fetchone()[0]
    theirs = app_env.execute(
        "INSERT INTO feature (tenant_id, name, status) VALUES (%s, 'Theirs', 'confirmed') "
        "RETURNING id",
        (other,),
    ).fetchone()[0]
    app_env.commit()

    human_effort.import_csv(
        tenant_id, csv_of(f"2026-05-15,Aman,acme,{mine['id']},2,development,100,")
    )
    assert _rows(tenant_id)[0][8] is not None

    with pytest.raises(human_effort.EffortImportError) as exc:
        human_effort.import_csv(
            tenant_id, csv_of(f"2026-05-16,Aman,acme,{theirs},2,development,100,")
        )
    assert "not a feature in this organization" in str(exc.value)


def test_deleting_a_feature_keeps_the_effort(tenant_id, app_env):
    feature = features.add_feature(tenant_id, "Doomed")
    human_effort.import_csv(
        tenant_id, csv_of(f"2026-05-15,Aman,acme,{feature['id']},2,development,100,")
    )
    features.delete_feature(tenant_id, feature["id"])

    row = _rows(tenant_id)[0]
    assert row[8] is None  # the link is gone...
    assert row[4] == Decimal("2.00")  # ...the cost is not


def test_the_same_file_cannot_be_imported_twice(tenant_id, app_env):
    human_effort.import_csv(tenant_id, ONE_ROW)
    with pytest.raises(human_effort.DuplicateImport) as exc:
        human_effort.import_csv(tenant_id, ONE_ROW)
    assert "already imported" in str(exc.value)
    assert len(_rows(tenant_id)) == 1  # and nothing was added the second time


def test_an_invalid_file_writes_nothing_at_all(tenant_id, app_env):
    """A half-loaded month is worse than a rejected one: the total looks
    plausible and nothing says which half is missing."""
    with pytest.raises(human_effort.EffortImportError):
        human_effort.import_csv(
            tenant_id,
            csv_of(
                "2026-05-15,Aman,acme,,3,development,100,fine",
                "2026-05-16,Alessio,acme,,2,support,110,also fine",
                "2026-05-17,Bo,acme,,-1,support,110,broken",
            ),
        )
    assert _rows(tenant_id) == []


def test_effort_is_tenant_isolated(tenant_id, app_env):
    other = str(
        app_env.execute("INSERT INTO tenant (name) VALUES ('Other Co') RETURNING id").fetchone()[0]
    )
    app_env.commit()
    human_effort.import_csv(tenant_id, ONE_ROW)

    assert len(_rows(tenant_id)) == 1
    assert _rows(other) == []
    assert human_effort.recent(other) == []


def test_two_tenants_can_import_the_same_file(tenant_id, app_env):
    """The duplicate guard is per tenant, not global."""
    other = str(
        app_env.execute("INSERT INTO tenant (name) VALUES ('Other Co') RETURNING id").fetchone()[0]
    )
    app_env.commit()
    human_effort.import_csv(tenant_id, ONE_ROW)
    human_effort.import_csv(other, ONE_ROW)  # must not raise
    assert len(_rows(other)) == 1


def test_money_is_decimal_all_the_way_through(tenant_id, app_env):
    """0.1 + 0.2 arithmetic has no place near a payroll number: three rows at
    a rate that is exact in decimal and not in binary must sum exactly."""
    human_effort.import_csv(
        tenant_id,
        csv_of(
            "2026-05-15,Aman,acme,,0.1,support,0.1,",
            "2026-05-15,Bo,acme,,0.2,support,0.1,",
            "2026-05-15,Cy,acme,,0.3,support,0.1,",
        ),
    )
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        total = conn.execute("SELECT SUM(hours * loaded_hourly_rate) FROM human_effort").fetchone()[
            0
        ]
    assert total == Decimal("0.0600")


# --- the aggregation it feeds ---------------------------------------------
def test_effort_joins_metered_spend_on_the_customer_screen(seeded, app_env):
    _customer(app_env, seeded, "acme", PERIOD, 600, 20_000)
    app_env.commit()
    human_effort.import_csv(seeded, csv_of("2026-05-15,Aman,acme,,4,development,150,"))

    data = dashboard.spend_by_customer(seeded, PERIOD)
    acme = next(c for c in data["customers"] if c["customer_id"] == "acme")
    assert acme["amount"] == 600.0
    assert acme["human_hours"] == 4.0
    assert acme["human_cost"] == 600.0
    assert acme["total_delivery_cost"] == 1200.0
    assert data["human_effort_present"] is True


def test_a_customer_with_effort_and_no_metered_calls_still_appears(seeded, app_env):
    """The customer this feature is most useful for: expensive in people,
    invisible in the model bill. Its AI cost is null — not zero, which would
    claim we measured it and found nothing."""
    _customer(app_env, seeded, "acme", PERIOD, 600, 20_000)
    app_env.commit()
    human_effort.import_csv(seeded, csv_of("2026-05-15,Aman,effort-only-co,,8,rework,140,"))

    data = dashboard.spend_by_customer(seeded, PERIOD)
    row = next(c for c in data["customers"] if c["customer_id"] == "effort-only-co")
    assert row["amount"] is None
    assert row["cost_per_request"] is None and row["requests"] is None
    assert row["human_cost"] == 1120.0
    assert row["total_delivery_cost"] == 1120.0
    # ...and it sorts after every customer that does have metered spend.
    assert data["customers"][-1]["customer_id"] == "effort-only-co"


def test_missing_effort_is_null_and_never_zero(seeded, app_env):
    _customer(app_env, seeded, "acme", PERIOD, 600, 20_000)
    _customer(app_env, seeded, "globex", PERIOD, 100, 5_000)
    app_env.commit()
    human_effort.import_csv(seeded, csv_of("2026-05-15,Aman,acme,,4,development,150,"))

    data = dashboard.spend_by_customer(seeded, PERIOD)
    globex = next(c for c in data["customers"] if c["customer_id"] == "globex")
    # Nobody logged hours against globex. That is not the same fact as nobody
    # having worked on it, and the screen must be able to tell them apart.
    assert globex["human_hours"] is None
    assert globex["human_cost"] is None
    # Delivery cost is still known for globex, because its AI half is.
    assert globex["total_delivery_cost"] == 100.0
    assert data["human_effort_customer_count"] == 1


def test_no_effort_anywhere_reports_null_totals_not_zero(seeded, app_env):
    _customer(app_env, seeded, "acme", PERIOD, 600, 20_000)
    app_env.commit()

    data = dashboard.spend_by_customer(seeded, PERIOD)
    assert data["human_effort_present"] is False
    assert data["human_hours"] is None and data["human_cost"] is None
    assert data["human_effort_ever"] is False
    assert data["customers"][0]["human_cost"] is None


def test_effort_outside_the_window_is_not_counted(seeded, app_env):
    _customer(app_env, seeded, "acme", PERIOD, 600, 20_000)
    app_env.commit()
    human_effort.import_csv(
        seeded,
        csv_of(
            "2026-04-30,Aman,acme,,5,development,100,the month before",
            "2026-05-01,Aman,acme,,1,development,100,first day in",
            "2026-05-31,Aman,acme,,2,development,100,last day in",
            "2026-06-01,Aman,acme,,9,development,100,the month after",
        ),
    )

    data = dashboard.spend_by_customer(seeded, PERIOD)
    acme = next(c for c in data["customers"] if c["customer_id"] == "acme")
    assert acme["human_hours"] == 3.0  # the two May days, and only those
    # The tenant HAS effort data, just not outside this window — a different
    # state from never having provided any.
    assert data["human_effort_ever"] is True


def test_the_effort_trend_uses_the_same_monthly_buckets(seeded, app_env):
    _customer(app_env, seeded, "acme", dt.date(2026, 3, 1), 10, 100)
    _customer(app_env, seeded, "acme", PERIOD, 30, 300)
    app_env.commit()
    human_effort.import_csv(
        seeded,
        csv_of(
            "2026-03-10,Aman,acme,,2,development,100,",
            "2026-03-20,Aman,acme,,3,development,100,",
            "2026-05-04,Aman,acme,,1,support,200,",
        ),
    )

    data = dashboard.spend_by_customer(seeded, dt.date(2026, 3, 1), PERIOD)
    trend = {t["period"]: t["amount"] for t in data["human_effort_trend"]}
    assert trend == {"2026-03-01": 500.0, "2026-05-01": 200.0}
    # April has no labour and simply isn't in the series — the same shape the
    # metered trend uses, so the two line up month for month.
    assert "2026-04-01" not in trend


# --- what must not have changed -------------------------------------------
def test_metered_totals_and_coverage_are_untouched_by_effort(seeded, app_env):
    _customer(app_env, seeded, "acme", PERIOD, 779, 1_000)
    app_env.commit()
    before = dashboard.spend_by_customer(seeded, PERIOD)

    human_effort.import_csv(
        seeded,
        csv_of(*(f"2026-05-{11 + d},Aman,acme,,8,support,200," for d in range(5))),
    )
    after = dashboard.spend_by_customer(seeded, PERIOD)

    # Labour is a third category. It may not move the AI numbers by a cent.
    for key in ("total", "inference_total", "coverage_pct"):
        assert after[key] == before[key], key
    assert after["customers"][0]["amount"] == before["customers"][0]["amount"]
    assert after["customers"][0]["cost_per_request"] == before["customers"][0]["cost_per_request"]
    # ...and the bill is still the Overview's own number.
    assert (
        after["inference_total"] == dashboard.dashboard(seeded, PERIOD)["totals"]["inference_cost"]
    )


def test_delivery_cost_is_not_presented_as_the_bill(seeded, app_env):
    """`total_delivery_cost` covers the customers on this screen. The real bill
    is `inference_total`, and it is bigger — most calls carry no customer tag."""
    _customer(app_env, seeded, "acme", PERIOD, 100, 1_000)
    app_env.commit()
    human_effort.import_csv(seeded, csv_of("2026-05-15,Aman,acme,,1,support,50,"))

    data = dashboard.spend_by_customer(seeded, PERIOD)
    assert data["total_delivery_cost"] == 150.0
    assert data["total"] == 100.0
    assert data["inference_total"] > data["total_delivery_cost"]


def test_prior_period_deltas_still_only_describe_metered_spend(seeded, app_env):
    _customer(app_env, seeded, "acme", dt.date(2026, 4, 1), 100, 500)
    _customer(app_env, seeded, "acme", PERIOD, 150, 700)
    app_env.commit()
    # A pile of labour in the prior window must not leak into the AI delta.
    human_effort.import_csv(
        seeded, csv_of(*(f"2026-04-{11 + d},Aman,acme,,10,rework,300," for d in range(2)))
    )

    acme = next(
        c
        for c in dashboard.spend_by_customer(seeded, PERIOD)["customers"]
        if c["customer_id"] == "acme"
    )
    assert acme["prev_amount"] == 100.0
    assert acme["delta_pct"] == 50.0


# --- the demo tenant -------------------------------------------------------
def test_the_demo_seed_shows_every_shape_the_screen_is_for(tenant_id, app_env):
    insert_sample_data(app_env, tenant_id, extended=True)
    app_env.commit()
    data = dashboard.spend_by_customer(tenant_id, dt.date(2026, 3, 1), PERIOD)
    by_id = {c["customer_id"]: c for c in data["customers"]}

    assert data["human_effort_present"] is True
    assert data["human_cost"] > 0 and data["human_hours"] > 0

    # High AI cost, almost no people.
    lean = by_id["globex-retail"]
    assert lean["amount"] > lean["human_cost"] * 4

    # The point of the whole feature, in one row: a customer that looks cheap on
    # the model bill and is the most expensive on the screen once the people are
    # counted. The RANK inverts — that is what nobody could see before.
    hidden = by_id["soylent-logistics"]
    assert hidden["human_cost"] > hidden["amount"] * 4
    assert hidden["amount"] < lean["amount"]  # cheaper than globex on AI...
    assert hidden["total_delivery_cost"] == max(
        c["total_delivery_cost"] for c in data["customers"]
    )  # ...and the costliest customer they have

    # Somebody has to be "Not provided", or the screen never demonstrates the
    # difference between no data and no effort.
    assert by_id["initech-legal"]["human_cost"] is None
    assert by_id["initech-legal"]["amount"] > 0

    # ...and somebody has to be effort-only.
    effort_only = by_id["meridian-shipping"]
    assert effort_only["amount"] is None and effort_only["human_cost"] > 0

    # The completeness line has a real fraction to report.
    assert 0 < data["human_effort_customer_count"] < len(data["customers"])


def test_the_demo_seed_does_not_move_the_metered_numbers(tenant_id, app_env):
    """The seeded customer-cost totals are load-bearing for other tests and for
    the demo script; adding labour must not have touched them."""
    insert_sample_data(app_env, tenant_id, extended=True)
    app_env.commit()
    data = dashboard.spend_by_customer(tenant_id, PERIOD)
    metered = {c["customer_id"]: c["amount"] for c in data["customers"] if c["amount"] is not None}
    assert metered["northwind-financial"] > 0
    assert data["total"] == pytest.approx(sum(metered.values()))
    assert data["total"] < data["inference_total"]  # still a subset of the bill
