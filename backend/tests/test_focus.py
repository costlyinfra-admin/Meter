"""FOCUS billing exports.

FOCUS is a published schema, so unlike a vendor's own CSV its columns can be
matched exactly — and read for what the specification says they mean. These
tests are mostly about that meaning: which of the four costs was summed, which
rows are tax rather than usage, which restate a month somebody already closed,
and where the tag that attributes a charge actually lives.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from meter import focus, infra_csv

HEADERS = (
    "BillingPeriodStart,ChargePeriodStart,ChargePeriodEnd,ChargeCategory,ChargeClass,"
    "ChargeDescription,BilledCost,EffectiveCost,ListCost,BillingCurrency,ServiceName,"
    "ServiceCategory,ServiceProvider,RegionId,SubAccountId,ResourceId,SkuId,Tags"
)


def row(
    *,
    category="Usage",
    charge_class="",
    billed="10.00",
    effective="8.00",
    listed="12.00",
    service="Amazon Elastic Compute Cloud",
    start="2026-09-01",
    tags="",
):
    quoted = f'"{tags}"' if tags else ""
    return (
        f"2026-09-01,{start},2026-09-02,{category},{charge_class},Compute,"
        f"{billed},{effective},{listed},USD,{service},Compute,AWS,us-east-1,1234,i-1,sku-1,{quoted}"
    )


def csv_of(*rows: str) -> str:
    return "\n".join((HEADERS, *rows)) + "\n"


def parse(*rows: str, **kwargs):
    return infra_csv.parse(csv_of(*rows), default_service="Cloud", **kwargs)


# ---------------------------------------------------------------------------
# Recognising one
# ---------------------------------------------------------------------------
def test_a_focus_export_is_recognised_as_one():
    report = parse(row())
    assert report.focus is not None
    assert report.mapping["date"] == "ChargePeriodStart"
    assert report.mapping["service"] == "ServiceName"


def test_an_ordinary_vendor_csv_is_not_mistaken_for_focus():
    # The by-meaning parser still handles everything that is not FOCUS.
    report = infra_csv.parse("Usage Date,Cost (USD),Service\n2026-09-01,10.00,Redis\n")
    assert report.focus is None
    assert report.mapping["amount"] == "Cost (USD)"


def test_columns_are_matched_however_the_exporter_cased_them():
    # A Parquet-to-CSV conversion may not preserve the spec's capitalisation,
    # and the file is still FOCUS.
    lowered = csv_of(row()).replace(HEADERS, HEADERS.lower())
    report = infra_csv.parse(lowered, default_service="Cloud")
    assert report.focus is not None
    assert report.focus["cost_column"] == "BilledCost"


def test_a_file_with_the_shape_but_no_cost_column_is_not_focus():
    # BilledCost is mandatory in every released version; without any cost
    # column this is something else that happens to share two names.
    text = "ChargeCategory,ChargePeriodStart,Amount\nUsage,2026-09-01,5.00\n"
    assert focus.detect(text.splitlines()[0].split(",")) is None


# ---------------------------------------------------------------------------
# Which dollars
# ---------------------------------------------------------------------------
def test_it_reads_billed_cost_by_default():
    # Invariant 5: the provider's own invoice is authoritative on dollars.
    report = parse(row(billed="10.00", effective="8.00", listed="12.00"))
    assert report.total == Decimal("10.00")
    assert report.focus["cost_column"] == "BilledCost"


def test_a_customer_can_ask_for_amortized_cost_instead():
    report = parse(row(billed="10.00", effective="8.00"), cost_column="EffectiveCost")
    assert report.total == Decimal("8.00")
    assert report.focus["cost_column"] == "EffectiveCost"


def test_it_says_which_other_costs_the_file_carries():
    report = parse(row())
    assert report.focus["cost_columns_available"] == ["BilledCost", "EffectiveCost", "ListCost"]
    assert any("also carries EffectiveCost, ListCost" in w for w in report.warnings)


def test_asking_for_a_cost_the_file_does_not_have_falls_back_rather_than_failing():
    text = csv_of(row()).replace(",EffectiveCost,ListCost", ",EffectiveCostX,ListCostX")
    report = infra_csv.parse(text, default_service="Cloud", cost_column="EffectiveCost")
    assert report.focus["cost_column"] == "BilledCost"


# ---------------------------------------------------------------------------
# What kind of charge
# ---------------------------------------------------------------------------
def test_tax_and_credits_are_imported_but_called_out():
    report = parse(
        row(category="Usage", billed="100.00"),
        row(category="Tax", billed="14.80"),
        row(category="Credit", billed="-25.00"),
    )
    # Imported, because dropping them would make Meter's total disagree with
    # the invoice it is supposed to reconcile against.
    assert report.total == Decimal("89.80")
    assert report.focus["by_category"] == {"Credit": -25.0, "Tax": 14.8, "Usage": 100.0}
    # ...and named, because attributing sales tax to a service is a lie.
    assert any("not service usage" in w for w in report.warnings)


def test_a_bill_of_only_usage_says_nothing_about_other_charges():
    report = parse(row(category="Usage"), row(category="Purchase"))
    assert not any("not service usage" in w for w in report.warnings)


def test_every_row_keeps_its_charge_category_for_later():
    report = parse(row(category="Tax"))
    assert report.items[0].dimensions["ChargeCategory"] == "Tax"


def test_a_correction_is_imported_and_flagged():
    # It is real money, and a month that moves after someone wrote its total
    # down should say so.
    report = parse(
        row(category="Usage", start="2026-09-01"),
        row(category="Usage", charge_class="Correction", start="2026-08-15"),
    )
    assert report.focus["corrections"] == 1
    assert len(report.items) == 2
    assert any("restates a closed billing period" in w for w in report.warnings)


def test_corrections_are_counted_not_just_noticed():
    report = parse(*[row(charge_class="Correction") for _ in range(3)])
    assert report.focus["corrections"] == 3
    assert any("3 rows restate" in w for w in report.warnings)


# ---------------------------------------------------------------------------
# Tags — where the attribution actually lives
# ---------------------------------------------------------------------------
def test_the_tag_is_read_out_of_the_json_map():
    report = parse(row(tags='{""feature"":""threat-triage"",""env"":""prod""}'))
    assert report.items[0].tag_value == "threat-triage"


def test_a_provider_prefixed_tag_key_still_matches():
    # AWS writes "aws:feature", and a FOCUS scheme prefix uses a slash. The
    # customer typed "feature" and means the same thing in both.
    assert focus.tag_value({"aws:feature": "billing"}, "feature") == "billing"
    assert focus.tag_value({"someScheme/feature": "billing"}, "feature") == "billing"
    assert focus.tag_value({"Feature": "billing"}, "feature") == "billing"


def test_an_unrelated_tag_is_not_mistaken_for_the_one_asked_for():
    assert focus.tag_value({"features": "x", "team": "y"}, "feature") is None


def test_a_valueless_tag_does_not_become_the_string_true():
    # FOCUS gives a tag key with no value the boolean true. Attributing spend
    # to a feature called "True" would be worse than attributing none.
    assert focus.read_tags('{"feature": true}') == {"feature": ""}
    assert focus.tag_value(focus.read_tags('{"feature": true}'), "feature") is None


def test_unreadable_tags_do_not_lose_the_charge():
    report = parse(row(tags="not json at all"))
    assert len(report.items) == 1
    assert report.items[0].tag_value is None


def test_no_tags_column_is_not_an_error():
    text = csv_of(row()).replace(",Tags", "").replace(",sku-1,", ",sku-1")
    report = infra_csv.parse(text, default_service="Cloud")
    assert report.focus is not None
    assert report.items[0].tag_value is None


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def test_a_clean_focus_file_reports_no_unused_columns():
    # Every column in the file is either read, kept as a dimension, or a spec
    # column Meter has no use for. Listing those as "not used" would bury the
    # warnings that matter.
    report = parse(row())
    assert report.unmapped == []
    assert not any("Columns not used" in w for w in report.warnings)


def test_vendor_extensions_are_recognised_rather_than_reported_as_junk():
    # FOCUS reserves the x_ prefix for a vendor's own columns, so one is
    # expected rather than unrecognised.
    report = infra_csv.parse(
        csv_of(row() + ",ops").replace(HEADERS, HEADERS + ",x_CostCenter"),
        default_service="Cloud",
    )
    assert report.focus["extensions"] == ["x_CostCenter"]
    assert report.unmapped == []


def test_a_genuinely_foreign_column_is_still_reported():
    report = infra_csv.parse(
        csv_of(row() + ",whatever").replace(HEADERS, HEADERS + ",SomeoneElsesColumn"),
        default_service="Cloud",
    )
    assert report.unmapped == ["SomeoneElsesColumn"]


def test_the_dimensions_keep_what_a_later_rule_might_group_by():
    report = parse(row())
    dims = report.items[0].dimensions
    assert dims["ServiceProvider"] == "AWS"
    assert dims["ResourceId"] == "i-1"
    assert dims["ServiceCategory"] == "Compute"
    assert dims["imported_from"] == "focus"


def test_the_date_is_the_charge_period_not_the_billing_period():
    # A September invoice can carry an August charge; the charge is when the
    # money was spent, which is the month it belongs in.
    report = parse(row(start="2026-08-15"))
    assert report.items[0].period == dt.date(2026, 8, 15)


@pytest.mark.parametrize("category", focus.CATEGORIES)
def test_every_category_the_spec_allows_can_be_imported(category):
    report = parse(row(category=category))
    assert report.focus["by_category"] == {category: 10.0}


# ---------------------------------------------------------------------------
# End to end, through the API a customer actually uses
# ---------------------------------------------------------------------------
@pytest.fixture
def client(admin_conn, admin_conninfo, app_conninfo, monkeypatch):
    from fastapi.testclient import TestClient
    from meter.api import create_app

    monkeypatch.setenv("APP_SECRET_KEY", "unit-test-secret-key")
    monkeypatch.setenv("DATABASE_URL", admin_conninfo)
    monkeypatch.setenv("DATABASE_APP_URL", app_conninfo)
    c = TestClient(create_app())
    c.post("/api/auth/signup", json={"email": "cto@acme.com", "password": "correct horse battery"})
    return c


def test_focus_is_offered_as_something_to_import_from(client):
    cards = client.get("/api/infrastructure/providers").json()
    card = next(p for p in cards if p["type"] == "focus")
    assert card["ingest"] == "csv"
    assert "FOCUS" in card["name"]


def test_a_preview_shows_the_split_before_anything_is_written(client):
    body = client.post(
        "/api/infrastructure/import",
        json={
            "provider": "focus",
            "csv": csv_of(
                row(category="Usage", billed="100.00"), row(category="Tax", billed="10.00")
            ),
            "dry_run": True,
        },
    ).json()

    assert body["focus"]["cost_column"] == "BilledCost"
    assert body["focus"]["by_category"] == {"Tax": 10.0, "Usage": 100.0}
    stored = client.get("/api/infrastructure/summary?provider=focus&period=2026-09").json()
    assert stored["rows"] == 0


def test_an_import_records_which_cost_column_the_dollars_came_from(client, admin_conn):
    client.post(
        "/api/infrastructure/import",
        json={"provider": "focus", "csv": csv_of(row()), "cost_column": "EffectiveCost"},
    )
    metrics = [m for (m,) in admin_conn.execute("SELECT DISTINCT metric FROM infra_cost")]
    # Not "invoice": a row must stay honest about which of FOCUS's four costs it
    # measured, because a tenant may import the other one next month.
    assert metrics == ["EffectiveCost"]


def test_a_vendor_csv_import_still_records_itself_as_an_invoice(client, admin_conn):
    client.post(
        "/api/infrastructure/import",
        json={
            "provider": "redis_cloud",
            "csv": "Usage Date,Cost (USD),Database\n2026-05-01,142.50,prod-cache\n",
        },
    )
    assert admin_conn.execute("SELECT DISTINCT metric FROM infra_cost").fetchone()[0] == "invoice"


def test_the_stored_rows_carry_the_focus_dimensions(client, admin_conn):
    client.post(
        "/api/infrastructure/import",
        json={
            "provider": "focus",
            "csv": csv_of(row(category="Tax", tags='{""feature"":""threat-triage""}')),
        },
    )
    dims = admin_conn.execute("SELECT dimensions FROM infra_cost").fetchone()[0]
    assert dims["ChargeCategory"] == "Tax"
    assert dims["ServiceProvider"] == "AWS"
    assert dims["imported_from"] == "focus"


def test_re_importing_a_corrected_file_converges_rather_than_doubling(client):
    first = client.post(
        "/api/infrastructure/import",
        json={"provider": "focus", "csv": csv_of(row(billed="100.00"))},
    ).json()
    assert first["items"] == 1

    client.post(
        "/api/infrastructure/import",
        json={"provider": "focus", "csv": csv_of(row(billed="120.00"))},
    )
    summary = client.get("/api/infrastructure/summary?provider=focus&period=2026-09").json()
    assert summary["total"] == 120.0


def test_the_preview_says_the_tag_came_from_the_tags_column(client):
    # Not "not used": the attribution is read out of the JSON map, and a
    # preview that showed nothing there would say the opposite of the truth.
    body = client.post(
        "/api/infrastructure/import",
        json={
            "provider": "focus",
            "csv": csv_of(row(tags='{""feature"":""threat-triage""}')),
            "dry_run": True,
        },
    ).json()
    assert body["mapping"]["tag"] == "Tags"


def test_pointing_the_tag_at_a_different_column_uses_that_column(client):
    # A customer whose attribution lives somewhere other than Tags says so, and
    # is not overruled by the spec.
    body = client.post(
        "/api/infrastructure/import",
        json={
            "provider": "focus",
            "csv": csv_of(row(tags='{""feature"":""ignored""}')),
            "mapping": {"tag": "ChargeDescription"},
            "dry_run": True,
        },
    ).json()
    assert body["mapping"]["tag"] == "ChargeDescription"
