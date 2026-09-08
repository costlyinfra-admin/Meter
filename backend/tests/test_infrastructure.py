"""Infrastructure ingest: classification, attribution, idempotency, and the
guarantee that Bedrock's dollars never reach an infrastructure total."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from meter import credentials, features, infrastructure
from meter.db import app_dsn, connect, tenant_tx
from meter.providers import AwsCostItem

DAY = dt.date(2026, 5, 4)
MONTH = dt.date(2026, 5, 1)
WINDOW = (dt.date(2026, 5, 1), dt.date(2026, 6, 1))


def item(service, amount, *, tag=None, usage_type=None, operation=None, region=None, account=None):
    """One AWS line item, shaped as the Cost Explorer client returns it."""
    return AwsCostItem(
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
def test_the_registry_lists_aws_live_and_the_others_as_coming_soon(tenant_id):
    by_type = {p["type"]: p for p in infrastructure.provider_status(tenant_id)}
    assert by_type["aws"]["status"] == "available"
    assert by_type["azure_cloud"]["status"] == "coming_soon"
    assert by_type["gcp"]["status"] == "coming_soon"
    assert all(p["connected"] is False for p in by_type.values())


def test_the_azure_openai_connector_does_not_light_up_the_azure_cloud_card(tenant_id):
    # "azure" is the Azure OpenAI *inference* connector. It reads a narrow slice
    # of the same bill with a different credential, and connecting it must not
    # make the (unbuilt) Azure infrastructure card claim to be connected.
    credentials.save_credential(tenant_id, "azure", '{"tenant_id":"t","client_id":"c"}')

    by_type = {p["type"]: p for p in infrastructure.provider_status(tenant_id)}
    assert by_type["azure_cloud"]["connected"] is False
    assert by_type["azure_cloud"]["config"] is None


def test_a_provider_that_is_not_built_yet_refuses_to_sync():
    with pytest.raises(infrastructure.InfraError, match="not available yet"):
        infrastructure.make_client("gcp", "{}")


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
        "region": "us-east-1",
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
