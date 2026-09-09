"""Infrastructure ingest: classification, attribution, idempotency, and the
guarantee that Bedrock's dollars never reach an infrastructure total."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from meter import credentials, features, infrastructure
from meter.db import app_dsn, connect, tenant_tx
from meter.providers import CloudCostItem, ProviderError

DAY = dt.date(2026, 5, 4)
MONTH = dt.date(2026, 5, 1)
WINDOW = (dt.date(2026, 5, 1), dt.date(2026, 6, 1))


def item(service, amount, *, tag=None, usage_type=None, operation=None, region=None, account=None):
    """One AWS line item, shaped as the Cost Explorer client returns it."""
    return CloudCostItem(
        period=DAY,
        amount=Decimal(str(amount)),
        service=service,
        usage_type=usage_type,
        operation=operation,
        region=region,
        account_id=account,
        tag_key="feature" if tag is not None else None,
        tag_value=tag,
        dimensions={
            "SERVICE": service,
            "TAG:feature": tag,
            **({"USAGE_TYPE": usage_type} if usage_type else {}),
        },
    )


def store(tenant_id, items):
    return infrastructure.persist(
        tenant_id,
        "aws",
        items,
        start=WINDOW[0],
        end=WINDOW[1],
        metric="UnblendedCost",
        granularity="DAILY",
    )


def rows(tenant_id):
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        return conn.execute(
            """
            SELECT service, amount, category, category_rule, counted, dedupe_owner,
                   allocation_method, feature_id, month, usage_type, region, account_id
            FROM infra_cost ORDER BY service
            """
        ).fetchall()


# --- ordinary import -------------------------------------------------------
def test_an_ordinary_aws_service_imports_as_infrastructure(tenant_id):
    summary = store(tenant_id, [item("Amazon Simple Storage Service", "12.50")])

    assert summary["items"] == 1
    assert summary["infrastructure"] == 12.5
    (row,) = rows(tenant_id)
    service, amount, category, rule, counted, owner, method, feature_id, month = row[:9]
    assert (service, category, counted, owner) == (
        "Amazon Simple Storage Service",
        "infrastructure",
        True,
        None,
    )
    assert amount == Decimal("12.5000")
    assert rule == "default-infrastructure"  # the number is explainable
    assert (method, feature_id) == ("unattributed", None)
    assert month == MONTH  # rolled up from the line item's day


def test_an_unknown_service_is_imported_not_dropped(tenant_id):
    summary = store(tenant_id, [item("AWS Something Launched Last Tuesday", "9.00")])
    assert summary["items"] == 1
    assert summary["infrastructure"] == 9.0
    assert rows(tenant_id)[0][2] == "infrastructure"


def test_raw_dimensions_are_preserved_for_later_reclassification(tenant_id):
    store(
        tenant_id,
        [
            item(
                "Amazon Elastic Compute Cloud - Compute",
                "40.00",
                usage_type="USE1-BoxUsage:m5.large",
                region="us-east-1",
                account="123456789012",
            )
        ],
    )
    (row,) = rows(tenant_id)
    assert row[9] == "USE1-BoxUsage:m5.large"
    assert row[10] == "us-east-1"
    assert row[11] == "123456789012"
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        dims = conn.execute("SELECT dimensions FROM infra_cost").fetchone()[0]
    assert dims["SERVICE"] == "Amazon Elastic Compute Cloud - Compute"


# --- double counting -------------------------------------------------------
def test_bedrock_is_recorded_but_never_counted_as_infrastructure(tenant_id):
    summary = store(
        tenant_id,
        [item("Amazon Bedrock", "500.00", tag="triage"), item("Amazon S3", "10.00")],
    )

    # The infrastructure total is the S3 line only.
    assert summary["infrastructure"] == 10.0
    assert summary["excluded"] == 500.0

    by_service = {r[0]: r for r in rows(tenant_id)}
    bedrock = by_service["Amazon Bedrock"]
    # Kept, so Phase 2 can reconcile the two paths — but not counted here.
    assert bedrock[2] == "inference"
    assert bedrock[4] is False
    assert bedrock[5] == "bedrock"


def test_bedrock_never_enters_the_month_summary(tenant_id):
    store(tenant_id, [item("Amazon Bedrock", "500.00"), item("Amazon S3", "10.00")])

    result = infrastructure.summary(tenant_id, "aws", MONTH)
    assert result["total"] == 10.0
    assert result["excluded"] == 500.0
    assert [s["service"] for s in result["services"]] == ["Amazon S3"]
    # Bedrock is absent from every counted category bucket.
    assert "inference" not in {c["category"] for c in result["by_category"]}


def test_a_gpu_box_is_self_hosted_and_not_in_the_infrastructure_total(tenant_id):
    summary = store(
        tenant_id,
        [
            item(
                "Amazon Elastic Compute Cloud - Compute",
                "800.00",
                usage_type="USE1-BoxUsage:p4d.24xlarge",
            ),
            item("Amazon S3", "10.00"),
        ],
    )
    assert summary["infrastructure"] == 10.0
    assert summary["by_category"]["self_hosted"] == 800.0


# --- feature attribution ---------------------------------------------------
def test_an_activated_tag_attributes_directly_to_a_feature(tenant_id):
    triage = features.add_feature(tenant_id, "AI threat triage")
    features.add_signal(tenant_id, triage["id"], "usage_tag", "triage")

    summary = store(tenant_id, [item("Amazon S3", "30.00", tag="triage")])

    assert summary["attributed"] == 30.0
    assert summary["unattributed"] == 0.0
    (row,) = rows(tenant_id)
    assert row[6] == "direct"
    assert str(row[7]) == triage["id"]


def test_a_configured_service_rule_allocates_where_no_tag_exists(tenant_id):
    reports = features.add_feature(tenant_id, "Report generator")
    features.add_signal(tenant_id, reports["id"], "service", "Amazon Simple Queue Service")

    store(tenant_id, [item("Amazon Simple Queue Service", "20.00")])

    (row,) = rows(tenant_id)
    assert row[6] == "allocated"
    assert str(row[7]) == reports["id"]


def test_a_tag_beats_a_service_rule(tenant_id):
    triage = features.add_feature(tenant_id, "AI threat triage")
    features.add_signal(tenant_id, triage["id"], "usage_tag", "triage")
    reports = features.add_feature(tenant_id, "Report generator")
    features.add_signal(tenant_id, reports["id"], "service", "Amazon S3")

    store(tenant_id, [item("Amazon S3", "20.00", tag="triage")])

    (row,) = rows(tenant_id)
    # The tag is the customer saying what this resource is for.
    assert (row[6], str(row[7])) == ("direct", triage["id"])


def test_an_unmapped_tag_lands_in_unattributed_not_a_guess(tenant_id):
    features.add_feature(tenant_id, "AI threat triage")  # no signal configured

    summary = store(tenant_id, [item("Amazon S3", "20.00", tag="something-else")])

    assert summary["unattributed"] == 20.0
    (row,) = rows(tenant_id)
    assert (row[6], row[7]) == ("unattributed", None)


def test_attribution_is_reported_for_infrastructure_only(tenant_id):
    # A tagged GPU box is attributed, but it is self-hosted spend, not
    # infrastructure. The summary's attributed/unattributed must add up to the
    # infrastructure total it is printed beside, or the panel contradicts itself.
    triage = features.add_feature(tenant_id, "AI threat triage")
    features.add_signal(tenant_id, triage["id"], "usage_tag", "triage")

    summary = store(
        tenant_id,
        [
            item("Amazon S3", "30.00", tag="triage"),
            item("Amazon S3 Glacier", "20.00"),
            item(
                "Amazon Elastic Compute Cloud - Compute",
                "800.00",
                usage_type="USE1-BoxUsage:p4d.24xlarge",
                tag="triage",
            ),
            item("Amazon Bedrock", "500.00", tag="triage"),
        ],
    )

    assert summary["infrastructure"] == 50.0
    assert summary["attributed"] + summary["unattributed"] == summary["infrastructure"]
    assert (summary["attributed"], summary["unattributed"]) == (30.0, 20.0)

    month = infrastructure.summary(tenant_id, "aws", MONTH)
    assert (month["attributed"], month["unattributed"]) == (30.0, 20.0)


def test_a_service_name_alone_never_invents_a_feature(tenant_id):
    # A feature called "Report generator" exists, and the bill has a service
    # whose name looks related. Without a configured signal that is a guess.
    features.add_feature(tenant_id, "Report generator")
    store(tenant_id, [item("Amazon Report Generator Service", "15.00")])
    assert rows(tenant_id)[0][7] is None


# --- idempotency -----------------------------------------------------------
def test_syncing_the_same_window_twice_does_not_duplicate(tenant_id):
    items = [item("Amazon S3", "10.00", tag="triage"), item("Amazon Bedrock", "500.00")]

    first = store(tenant_id, items)
    second = store(tenant_id, items)

    assert first["items"] == second["items"] == 2
    assert len(rows(tenant_id)) == 2
    assert infrastructure.summary(tenant_id, "aws", MONTH)["total"] == 10.0


def test_a_line_item_that_leaves_the_bill_leaves_us_too(tenant_id):
    store(tenant_id, [item("Amazon S3", "10.00"), item("Amazon EC2", "40.00")])
    # AWS restates the window and the EC2 line is gone.
    store(tenant_id, [item("Amazon S3", "10.00")])
    assert [r[0] for r in rows(tenant_id)] == ["Amazon S3"]


def test_repeated_dimensions_in_one_batch_are_summed_not_rejected(tenant_id):
    # The unique index would reject a second row for the same key; folding keeps
    # the money right rather than losing the duplicate or blowing up.
    summary = store(tenant_id, [item("Amazon S3", "10.00"), item("Amazon S3", "5.00")])
    assert summary["items"] == 1
    assert rows(tenant_id)[0][1] == Decimal("15.0000")


def test_reclassification_does_not_move_a_row_s_identity():
    # item_key is built from the raw dimensions, not the category, so editing the
    # rule table and re-syncing updates rows instead of duplicating them.
    one = item("Amazon S3", "10.00", tag="triage")
    key = infrastructure.item_key(one, metric="UnblendedCost", granularity="DAILY")
    assert key == infrastructure.item_key(one, metric="UnblendedCost", granularity="DAILY")
    # But what was measured is part of the identity: different metric, different row.
    assert key != infrastructure.item_key(one, metric="AmortizedCost", granularity="DAILY")


# --- sync, runs, and errors -----------------------------------------------
class _FakeClient:
    """Stands in for AwsCostExplorerClient — no network, same surface."""

    metric = "UnblendedCost"
    granularity = "DAILY"

    def __init__(self, items=None, error=None):
        self._items, self._error = items or [], error

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def fetch_items(self, start, end):
        if self._error:
            raise self._error
        return self._items


def test_a_successful_sync_is_recorded_for_the_connector_card(tenant_id, monkeypatch):
    monkeypatch.setattr(
        infrastructure, "_make_client", lambda *_: _FakeClient([item("Amazon S3", "10.00")])
    )
    infrastructure.sync_window(
        tenant_id, "aws", "{}", start=WINDOW[0], end=WINDOW[1], trigger="manual"
    )

    status = {p["type"]: p for p in infrastructure.provider_status(tenant_id)}["aws"]
    assert status["last_sync"]["status"] == "success"
    assert status["last_sync"]["items"] == 1
    assert status["last_sync"]["error_message"] is None


def test_a_failed_sync_is_recorded_with_a_safe_message(tenant_id, monkeypatch):
    from meter.providers import ProviderError

    monkeypatch.setattr(
        infrastructure,
        "_make_client",
        lambda *_: _FakeClient(error=ProviderError("AWS rejected the credentials.", 403)),
    )
    with pytest.raises(ProviderError):
        infrastructure.sync_window(tenant_id, "aws", "{}", start=WINDOW[0], end=WINDOW[1])

    status = {p["type"]: p for p in infrastructure.provider_status(tenant_id)}["aws"]
    assert status["last_sync"]["status"] == "error"
    assert status["last_sync"]["error_message"] == "AWS rejected the credentials."


def test_an_unexpected_error_is_not_echoed_to_the_user(tenant_id, monkeypatch):
    # An arbitrary exception's text is not vetted for secrets, so it is logged
    # and replaced rather than stored on a row the customer reads.
    leak = RuntimeError("boom: secret_access_key=AKIAIOSFODNN7EXAMPLE")
    monkeypatch.setattr(infrastructure, "_make_client", lambda *_: _FakeClient(error=leak))
    with pytest.raises(RuntimeError):
        infrastructure.sync_window(tenant_id, "aws", "{}", start=WINDOW[0], end=WINDOW[1])

    status = {p["type"]: p for p in infrastructure.provider_status(tenant_id)}["aws"]
    message = status["last_sync"]["error_message"]
    assert "AKIA" not in message and "secret_access_key" not in message


# --- the provider registry -------------------------------------------------
def test_no_cloud_reports_connected_before_a_credential_exists(tenant_id):
    by_type = {p["type"]: p for p in infrastructure.provider_status(tenant_id)}
    assert all(p["connected"] is False for p in by_type.values())
    assert all(p["config"] is None for p in by_type.values())
    assert all(p["last_sync"] is None for p in by_type.values())


def test_the_azure_openai_connector_does_not_light_up_the_azure_cloud_card(tenant_id):
    # "azure" is the Azure OpenAI *inference* connector. It reads a narrow slice
    # of the same bill with a different credential, and connecting it must not
    # make the (unbuilt) Azure infrastructure card claim to be connected.
    credentials.save_credential(tenant_id, "azure", '{"tenant_id":"t","client_id":"c"}')

    by_type = {p["type"]: p for p in infrastructure.provider_status(tenant_id)}
    assert by_type["azure_cloud"]["connected"] is False
    assert by_type["azure_cloud"]["config"] is None


def test_an_unknown_cloud_refuses_to_sync():
    with pytest.raises(infrastructure.InfraError, match="Unknown infrastructure provider"):
        infrastructure.make_client("oracle_cloud", "{}")


def test_connection_state_and_config_come_back_without_the_secret(tenant_id):
    credentials.save_credential(
        tenant_id,
        "aws",
        '{"access_key_id":"AKIAIOSFODNN7EXAMPLE","secret_access_key":"wJalrXUtn",'
        '"tag":"feature","metric":"AmortizedCost","granularity":"MONTHLY"}',
    )
    status = {p["type"]: p for p in infrastructure.provider_status(tenant_id)}["aws"]

    assert status["connected"] is True
    assert status["config"] == {
        "tag": "feature",
        "metric": "AmortizedCost",
        "granularity": "MONTHLY",
        # `scope` is what these numbers cover, named in the provider's own terms.
        "scope": "us-east-1",
        "scope_label": "region",
        "group_by": ["SERVICE", "TAG"],
    }
    # The keys are nowhere in what the API hands back.
    blob = repr(status)
    assert "AKIAIOSFODNN7EXAMPLE" not in blob and "wJalrXUtn" not in blob


def test_backfill_start_is_month_aligned_and_crosses_a_year():
    assert infrastructure.backfill_start(12, today=dt.date(2026, 5, 20)) == dt.date(2025, 6, 1)
    assert infrastructure.backfill_start(1, today=dt.date(2026, 5, 20)) == dt.date(2026, 5, 1)


def test_one_tenant_never_sees_another_s_cloud_bill(tenant_id, app_env):
    other = str(
        app_env.execute("INSERT INTO tenant (name) VALUES ('Other Co') RETURNING id").fetchone()[0]
    )
    app_env.commit()

    store(tenant_id, [item("Amazon S3", "10.00")])
    store(other, [item("Amazon EC2", "999.00")])

    assert [r[0] for r in rows(tenant_id)] == ["Amazon S3"]
    assert [r[0] for r in rows(other)] == ["Amazon EC2"]
    assert infrastructure.summary(tenant_id, "aws", MONTH)["total"] == 10.0
    assert infrastructure.summary(other, "aws", MONTH)["total"] == 999.0


def test_the_nightly_run_syncs_every_tenant_that_connected_aws(tenant_id, app_env, monkeypatch):
    other = str(
        app_env.execute("INSERT INTO tenant (name) VALUES ('Other Co') RETURNING id").fetchone()[0]
    )
    app_env.commit()
    for tid in (tenant_id, other):
        credentials.save_credential(tid, "aws", '{"access_key_id":"A","secret_access_key":"B"}')
    monkeypatch.setattr(
        infrastructure, "_make_client", lambda *_: _FakeClient([item("Amazon S3", "10.00")])
    )

    results = infrastructure.run_scheduled_infra_sync(months=1)

    assert len(results) == 2
    assert all(r["items"] == 1 for r in results)


def test_one_tenant_failing_the_nightly_run_does_not_stop_the_others(
    tenant_id, app_env, monkeypatch
):
    other = str(
        app_env.execute("INSERT INTO tenant (name) VALUES ('Other Co') RETURNING id").fetchone()[0]
    )
    app_env.commit()
    for tid in (tenant_id, other):
        credentials.save_credential(tid, "aws", '{"access_key_id":"A","secret_access_key":"B"}')

    from meter.providers import ProviderError

    calls = {"n": 0}

    def flaky(*_args):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeClient(error=ProviderError("AWS rejected the credentials.", 403))
        return _FakeClient([item("Amazon S3", "10.00")])

    monkeypatch.setattr(infrastructure, "_make_client", flaky)
    results = infrastructure.run_scheduled_infra_sync(months=1)

    assert len(results) == 2
    assert sum("error" in r for r in results) == 1
    assert sum(r.get("items") == 1 for r in results) == 1


# ---------------------------------------------------------------------------
# Azure and GCP: the same guarantees as AWS, on their own vocabularies.
# ---------------------------------------------------------------------------
def store_for(tenant_id, provider_type, items, metric="cost", granularity="DAILY"):
    return infrastructure.persist(
        tenant_id,
        provider_type,
        items,
        start=WINDOW[0],
        end=WINDOW[1],
        metric=metric,
        granularity=granularity,
    )


def rows_for(tenant_id, provider_type):
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        return conn.execute(
            "SELECT service, amount, category, counted, dedupe_owner, allocation_method "
            "FROM infra_cost WHERE provider = %s ORDER BY service",
            (provider_type,),
        ).fetchall()


def test_azure_imports_the_subscription_and_keeps_azure_openai_out(tenant_id):
    summary = store_for(
        tenant_id,
        "azure_cloud",
        [
            item("Azure Blob Storage", "120.00"),
            item("Azure OpenAI", "800.00"),
            item("Virtual Machines", "500.00", usage_type="Standard_NC24ads_A100_v4"),
            item("Azure DevOps", "40.00"),
        ],
        metric="ActualCost",
    )

    assert summary["infrastructure"] == 120.0
    assert summary["excluded"] == 800.0
    by_service = {r[0]: r for r in rows_for(tenant_id, "azure_cloud")}
    assert by_service["Azure OpenAI"][2:5] == ("inference", False, "azure")
    assert by_service["Virtual Machines"][2] == "self_hosted"
    assert by_service["Azure DevOps"][2] == "build"


def test_gcp_imports_the_bill_and_keeps_vertex_out(tenant_id):
    summary = store_for(
        tenant_id,
        "gcp",
        [
            item("Cloud SQL", "214.50"),
            item("Vertex AI", "900.00"),
            item(
                "Compute Engine", "640.00", usage_type="Nvidia Tesla A100 GPU running in Americas"
            ),
            item("Cloud Build", "18.00"),
        ],
    )

    assert summary["infrastructure"] == 214.5
    assert summary["excluded"] == 900.0
    by_service = {r[0]: r for r in rows_for(tenant_id, "gcp")}
    assert by_service["Vertex AI"][2:5] == ("inference", False, "google")
    assert by_service["Compute Engine"][2] == "self_hosted"
    assert by_service["Cloud Build"][2] == "build"


def test_a_deduped_service_never_reaches_any_provider_s_month_summary(tenant_id):
    store_for(tenant_id, "aws", [item("Amazon Bedrock", "500"), item("Amazon S3", "10")])
    store_for(tenant_id, "azure_cloud", [item("Azure OpenAI", "800"), item("Azure Blob", "20")])
    store_for(tenant_id, "gcp", [item("Vertex AI", "900"), item("Cloud SQL", "30")])

    for provider_type, total, excluded in (
        ("aws", 10.0, 500.0),
        ("azure_cloud", 20.0, 800.0),
        ("gcp", 30.0, 900.0),
    ):
        s = infrastructure.summary(tenant_id, provider_type, MONTH)
        assert s["total"] == total, provider_type
        assert s["excluded"] == excluded, provider_type
        assert "inference" not in {c["category"] for c in s["by_category"]}


def test_each_provider_s_rows_are_independent(tenant_id):
    # Three clouds in one tenant. A sync of one must not disturb another's rows.
    store_for(tenant_id, "aws", [item("Amazon S3", "10")])
    store_for(tenant_id, "azure_cloud", [item("Azure Blob", "20")])
    store_for(tenant_id, "gcp", [item("Cloud SQL", "30")])
    store_for(tenant_id, "aws", [item("Amazon S3", "11")])  # re-sync AWS only

    assert [r[1] for r in rows_for(tenant_id, "aws")] == [Decimal("11.0000")]
    assert [r[1] for r in rows_for(tenant_id, "azure_cloud")] == [Decimal("20.0000")]
    assert [r[1] for r in rows_for(tenant_id, "gcp")] == [Decimal("30.0000")]


def test_the_same_tag_attributes_across_every_cloud(tenant_id):
    triage = features.add_feature(tenant_id, "AI threat triage")
    features.add_signal(tenant_id, triage["id"], "usage_tag", "triage")

    for provider_type, service in (
        ("aws", "Amazon S3"),
        ("azure_cloud", "Azure Blob"),
        ("gcp", "Cloud SQL"),
    ):
        s = store_for(tenant_id, provider_type, [item(service, "10.00", tag="triage")])
        assert s["attributed"] == 10.0, provider_type
        assert rows_for(tenant_id, provider_type)[0][5] == "direct"


def test_each_cloud_s_config_is_described_in_its_own_vocabulary(tenant_id):
    credentials.save_credential(
        tenant_id,
        "azure_cloud",
        '{"tenant_id":"t","client_id":"c","client_secret":"VERY-SECRET-VALUE",'
        '"subscription_id":"sub-1","tag":"feature"}',
    )
    credentials.save_credential(
        tenant_id,
        "gcp",
        '{"client_email":"a@b.iam.gserviceaccount.com","private_key":"-----BEGIN PRIVATE KEY-----",'
        '"project_id":"my-proj","dataset":"billing_export","table":"gcp_billing_export_v1_X"}',
    )
    by_type = {p["type"]: p for p in infrastructure.provider_status(tenant_id)}

    azure = by_type["azure_cloud"]["config"]
    assert (azure["metric"], azure["scope_label"], azure["scope"]) == (
        "ActualCost",
        "subscription id",
        "sub-1",
    )
    gcp = by_type["gcp"]["config"]
    # No borrowed vocabulary: the export reports one figure, the billed cost.
    assert (gcp["metric"], gcp["scope_label"], gcp["scope"]) == (
        "cost",
        "dataset",
        "billing_export",
    )

    # Neither secret appears anywhere in what the API hands back.
    blob = repr(by_type)
    assert "VERY-SECRET-VALUE" not in blob
    assert "BEGIN PRIVATE KEY" not in blob


def test_the_nightly_run_covers_every_cloud_a_tenant_connected(tenant_id, monkeypatch):
    for connector in ("aws", "azure_cloud", "gcp"):
        credentials.save_credential(
            tenant_id, connector, '{"access_key_id":"A","secret_access_key":"B"}'
        )
    monkeypatch.setattr(
        infrastructure, "_make_client", lambda *_: _FakeClient([item("Some Service", "10.00")])
    )

    results = infrastructure.run_scheduled_infra_sync(months=1)

    assert {r["provider"] for r in results} == {"aws", "azure_cloud", "gcp"}


# ---------------------------------------------------------------------------
# Managed platforms: DigitalOcean, MongoDB Atlas, Cloudflare, Snowflake.
# ---------------------------------------------------------------------------
PLATFORMS = ["digitalocean", "mongodb_atlas", "cloudflare", "snowflake", "vercel_cloud"]


def test_a_platform_bill_imports_as_infrastructure(tenant_id):
    summary = store_for(
        tenant_id,
        "digitalocean",
        [item("Droplets", "480.00"), item("Spaces", "22.50"), item("Managed Databases", "310.00")],
        metric="invoice",
    )
    assert summary["infrastructure"] == 812.5
    assert summary["excluded"] == 0.0
    assert {r[0] for r in rows_for(tenant_id, "digitalocean")} == {
        "Droplets",
        "Spaces",
        "Managed Databases",
    }


def test_a_platform_s_own_ai_product_is_counted_as_inference_not_excluded(tenant_id):
    # Cloudflare Workers AI and Snowflake Cortex are real inference spend that no
    # other connector ingests. Excluding them would delete money from the books;
    # calling them infrastructure would overstate infrastructure. Neither.
    cf = store_for(
        tenant_id, "cloudflare", [item("Workers AI", "120.00"), item("Pro Plan", "20.00")]
    )
    assert cf["infrastructure"] == 20.0
    assert cf["excluded"] == 0.0
    assert cf["by_category"]["inference"] == 120.0

    sf = store_for(
        tenant_id, "snowflake", [item("AI_SERVICES", "96.20"), item("WAREHOUSE_METERING", "412.75")]
    )
    assert sf["infrastructure"] == 412.75
    assert sf["excluded"] == 0.0
    assert sf["by_category"]["inference"] == 96.2

    by_service = {r[0]: r for r in rows_for(tenant_id, "cloudflare")}
    # counted = True: nothing else is counting this dollar.
    assert by_service["Workers AI"][2:5] == ("inference", True, None)


def test_atlas_bills_only_infrastructure(tenant_id):
    summary = store_for(
        tenant_id,
        "mongodb_atlas",
        [item("ATLAS_AWS_INSTANCE_M30", "1234.56"), item("ATLAS_BACKUP", "88.00")],
    )
    assert summary["infrastructure"] == 1322.56
    assert summary["by_category"] == {"infrastructure": 1322.56}


@pytest.mark.parametrize("provider_type", PLATFORMS)
def test_every_platform_is_idempotent_and_independent(tenant_id, provider_type):
    items = [item("Some Service", "10.00"), item("Another Service", "20.00")]
    first = store_for(tenant_id, provider_type, items)
    second = store_for(tenant_id, provider_type, items)

    assert first == second
    assert len(rows_for(tenant_id, provider_type)) == 2
    # A sync of one platform leaves the others' rows alone.
    store_for(tenant_id, "aws", [item("Amazon S3", "5.00")])
    assert len(rows_for(tenant_id, provider_type)) == 2


@pytest.mark.parametrize("provider_type", PLATFORMS)
def test_every_platform_attributes_by_the_same_tag(tenant_id, provider_type):
    triage = features.add_feature(tenant_id, f"Feature for {provider_type}")
    features.add_signal(tenant_id, triage["id"], "usage_tag", "triage")

    summary = store_for(tenant_id, provider_type, [item("Some Service", "40.00", tag="triage")])

    assert summary["attributed"] == 40.0
    assert rows_for(tenant_id, provider_type)[0][5] == "direct"


def test_the_registry_lists_every_connected_cloud_and_platform(tenant_id):
    types = [p["type"] for p in infrastructure.PROVIDERS]
    assert types == [
        "aws",
        "azure_cloud",
        "gcp",
        "digitalocean",
        "mongodb_atlas",
        "cloudflare",
        "snowflake",
        "vercel_cloud",
        "redis_cloud",
        "supabase",
        "neon",
    ]
    # Every provider declares how its numbers arrive, and the two sets partition
    # the registry — a provider that is in neither could never be synced at all.
    assert set(infrastructure.LIVE_PROVIDERS) | set(infrastructure.CSV_PROVIDERS) == set(types)
    assert not set(infrastructure.LIVE_PROVIDERS) & set(infrastructure.CSV_PROVIDERS)
    for p in infrastructure.PROVIDERS:
        assert p["status"] == "available"
        assert p["ingest"] in ("api", "csv"), p["type"]
        expected = (
            infrastructure.CSV_PROVIDERS if p["ingest"] == "csv" else infrastructure.LIVE_PROVIDERS
        )
        assert p["type"] in expected, p["type"]


@pytest.mark.parametrize("provider_type", PLATFORMS)
def test_every_platform_has_a_client(provider_type):
    # A registry entry with no client behind it would fail only at sync time,
    # on a customer's first attempt, after they had pasted a credential.
    with pytest.raises(ProviderError):
        infrastructure.make_client(provider_type, "{}")


def test_no_platform_config_panel_can_show_a_secret(tenant_id):
    secrets = {
        "digitalocean": ('{"token":"dop_v1_SECRETTOKENVALUE"}', "dop_v1_SECRETTOKENVALUE"),
        "mongodb_atlas": (
            '{"public_key":"pk","private_key":"ATLASSECRETVALUE","org_id":"org1"}',
            "ATLASSECRETVALUE",
        ),
        "cloudflare": (
            '{"api_token":"CFSECRETTOKENVALUE","account_id":"acct-1"}',
            "CFSECRETTOKENVALUE",
        ),
        "snowflake": (
            '{"account":"MYORG-ACME","user":"u","private_key":"SNOWFLAKEPEMSECRET"}',
            "SNOWFLAKEPEMSECRET",
        ),
    }
    for connector, (blob, _) in secrets.items():
        credentials.save_credential(tenant_id, connector, blob)

    status = repr(infrastructure.provider_status(tenant_id))
    for _, (_, secret) in secrets.items():
        assert secret not in status, secret


def test_vercel_splits_one_invoice_across_build_run_and_inference(tenant_id):
    # The AI Gateway line is recorded but not counted: the Vercel AI Gateway
    # connector on the Inference tab is already counting those dollars.
    summary = store_for(
        tenant_id,
        "vercel_cloud",
        [
            item("Edge Functions", "142.50"),
            item("Blob", "18.00"),
            item("Build Execution", "64.00"),
            item("AI Gateway", "980.00"),
        ],
        metric="BilledCost",
    )

    assert summary["infrastructure"] == 160.5
    assert summary["excluded"] == 980.0
    assert summary["by_category"]["build"] == 64.0

    by_service = {r[0]: r for r in rows_for(tenant_id, "vercel_cloud")}
    assert by_service["AI Gateway"][2:5] == ("inference", False, "vercel")
    assert infrastructure.summary(tenant_id, "vercel_cloud", MONTH)["excluded"] == 980.0


# ---------------------------------------------------------------------------
# CSV import: Redis Cloud, Supabase and Neon.
# ---------------------------------------------------------------------------
CSV_PROVIDERS = ["redis_cloud", "supabase", "neon"]
BILL = (
    "Subscription,Database,Usage Date,Cost (USD)\n"
    "prod-cache,sessions,2026-05-01,142.50\n"
    "prod-cache,search,2026-05-02,88.25\n"
    "staging,scratch,2026-05-03,12.10\n"
)


def test_a_preview_writes_nothing(tenant_id):
    report = infrastructure.import_csv(tenant_id, "redis_cloud", BILL, dry_run=True)

    assert report["dry_run"] is True
    assert report["rows_imported"] == 3
    assert report["total"] == 242.85
    assert report["mapping"]["amount"] == "Cost (USD)"
    # Nothing reached the database: a preview that half-imported would be worse
    # than no preview at all.
    assert rows_for(tenant_id, "redis_cloud") == []
    status = {p["type"]: p for p in infrastructure.provider_status(tenant_id)}["redis_cloud"]
    assert status["connected"] is False


@pytest.mark.parametrize("provider_type", CSV_PROVIDERS)
def test_an_import_stores_the_bill_as_infrastructure(tenant_id, provider_type):
    result = infrastructure.import_csv(tenant_id, provider_type, BILL)

    assert result["dry_run"] is False
    assert result["items"] == 3
    assert result["infrastructure"] == 242.85
    rows = rows_for(tenant_id, provider_type)
    assert len(rows) == 3
    assert {r[2] for r in rows} == {"infrastructure"}


def test_a_bill_with_no_service_column_is_labelled_with_the_platform(tenant_id):
    # Rather than landing the whole import in Unclassified.
    infrastructure.import_csv(tenant_id, "neon", "Date,Amount\n2026-05-01,10.00\n")
    assert rows_for(tenant_id, "neon")[0][0] == "Neon"


@pytest.mark.parametrize("provider_type", CSV_PROVIDERS)
def test_re_importing_a_corrected_file_converges_rather_than_doubling(tenant_id, provider_type):
    infrastructure.import_csv(tenant_id, provider_type, BILL)
    corrected = BILL.replace("142.50", "150.00")
    result = infrastructure.import_csv(tenant_id, provider_type, corrected)

    assert result["infrastructure"] == 250.35
    assert len(rows_for(tenant_id, provider_type)) == 3
    assert infrastructure.summary(tenant_id, provider_type, MONTH)["total"] == 250.35


def test_an_import_never_disturbs_rows_an_api_connector_wrote(tenant_id):
    # The two sources share the table; a file import replacing its own window
    # must not take an API sync's rows with it.
    store_for(tenant_id, "redis_cloud", [item("Api Written", "999.00")])
    infrastructure.import_csv(tenant_id, "redis_cloud", BILL)

    services = sorted(r[0] for r in rows_for(tenant_id, "redis_cloud"))
    assert "Api Written" in services
    assert len(services) == 4


def test_a_csv_provider_is_connected_once_it_has_been_given_a_file(tenant_id):
    by_type = {p["type"]: p for p in infrastructure.provider_status(tenant_id)}
    assert by_type["redis_cloud"]["ingest"] == "csv"
    assert by_type["redis_cloud"]["connected"] is False

    infrastructure.import_csv(tenant_id, "redis_cloud", BILL)

    after = {p["type"]: p for p in infrastructure.provider_status(tenant_id)}["redis_cloud"]
    assert after["connected"] is True
    assert after["last_sync"]["status"] == "success"
    assert after["last_sync"]["items"] == 3
    # No credential exists, so there is no configuration to show.
    assert after["config"] is None


def test_an_imported_bill_attributes_by_its_tag_column(tenant_id):
    triage = features.add_feature(tenant_id, "AI threat triage")
    features.add_signal(tenant_id, triage["id"], "usage_tag", "prod-cache")

    result = infrastructure.import_csv(tenant_id, "supabase", BILL)

    # The Subscription column was matched as the tag.
    assert result["attributed"] == 230.75
    assert result["unattributed"] == 12.10


@pytest.mark.parametrize("provider_type", ["aws", "vercel_cloud"])
def test_an_api_provider_refuses_a_file(tenant_id, provider_type):
    with pytest.raises(infrastructure.InfraError, match="nothing to import"):
        infrastructure.import_csv(tenant_id, provider_type, BILL)


def test_an_unreadable_file_is_reported_not_half_imported(tenant_id):
    with pytest.raises(infrastructure.InfraError, match="an amount"):
        infrastructure.import_csv(tenant_id, "neon", "Date,Notes\n2026-05-01,hello\n")
    assert rows_for(tenant_id, "neon") == []


def test_csv_providers_are_not_polled_by_the_scheduled_run(tenant_id, monkeypatch):
    # They have no API to poll. A scheduled run that tried would fail nightly
    # against a provider that is working perfectly well.
    infrastructure.import_csv(tenant_id, "redis_cloud", BILL)
    calls = []
    monkeypatch.setattr(
        infrastructure, "_make_client", lambda p, s: calls.append(p) or _FakeClient([])
    )

    infrastructure.run_scheduled_infra_sync(months=1)

    assert not any(c in infrastructure.CSV_PROVIDERS for c in calls)
