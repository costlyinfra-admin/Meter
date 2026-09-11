"""Inference ingest + attribution: confidence ladder, Unattributed bucket, totals."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from meter import features, inference, resources
from meter.providers import CostRecord, UsageRecord

PERIOD = dt.date(2026, 5, 1)


def _record(provider, amount, *, api_key_ref=None, project=None, model="m"):
    return CostRecord(
        provider=provider,
        period=PERIOD,
        amount=Decimal(str(amount)),
        api_key_ref=api_key_ref,
        project=project,
        model=model,
    )


def _mapped_tenant(tenant_id):
    """A tenant with a per-key feature and a per-project feature."""
    triage = features.add_feature(tenant_id, "AI threat triage")
    features.add_signal(tenant_id, triage["id"], "api_key", "key:triage")
    reports = features.add_feature(tenant_id, "Report generator")
    features.add_signal(tenant_id, reports["id"], "service", "proj_reports")
    return triage, reports


def test_attribution_confidence_and_unattributed(tenant_id):
    triage, reports = _mapped_tenant(tenant_id)

    anthropic_records = [
        _record("anthropic", 4200, api_key_ref="key:triage"),  # -> triage, high
        _record("anthropic", 760, api_key_ref="key:shared"),  # -> Unattributed, low
    ]
    openai_records = [
        _record("openai", 1850, project="proj_reports"),  # -> reports, med
    ]

    a_summary = inference.ingest_records(tenant_id, "anthropic", PERIOD, anthropic_records)
    inference.ingest_records(tenant_id, "openai", PERIOD, openai_records)

    assert a_summary["total"] == 4960.0
    assert a_summary["attributed"] == 4200.0
    assert a_summary["unattributed"] == 760.0

    summary = inference.inference_summary(tenant_id, PERIOD)
    by_name = {f["name"]: f for f in summary["features"]}
    assert by_name["AI threat triage"]["amount"] == 4200.0
    assert by_name["AI threat triage"]["confidence"] == "high"
    assert by_name["Report generator"]["confidence"] == "med"
    assert summary["unattributed"] == 760.0
    # Provider totals (== dashboard) are preserved across attributed + unattributed.
    assert summary["by_provider"] == {"anthropic": 4960.0, "openai": 1850.0}
    assert summary["total"] == 6810.0


def test_anthropic_current_month_ingest_reconciles_with_cost_report(tenant_id, monkeypatch):
    # "Verify August's month-to-date total against the Cost Report": ingesting the
    # CURRENT month persists exactly the Cost Report's authoritative dollars.
    today = dt.date.today()

    class _FakeAnthropic:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def fetch_costs(self, period):  # authoritative month-to-date billed dollars
            return [
                CostRecord("anthropic", period, Decimal("4200.00"), project="ws_triage", model="c")
            ]

        def fetch_usage(self, period):  # identity split (never the dollar source)
            return [
                UsageRecord(
                    workspace_id="ws_triage",
                    api_key_id="key1",
                    model="claude-sonnet-4-6",
                    tokens_in=1000,
                    tokens_out=500,
                    request_count=10,
                )
            ]

        def fetch_workspaces(self):
            return {"ws_triage": "Triage"}

        def fetch_api_keys(self):
            return {"key1": {"name": "prod", "workspace_id": "ws_triage"}}

    monkeypatch.setattr(inference, "_make_cost_client", lambda provider, key: _FakeAnthropic())
    summary = inference.run_inference_ingest(tenant_id, "anthropic", today, "admin-key")

    # The ingest total equals the Cost Report total, and it lands on the current month.
    assert summary["total"] == 4200.0
    assert summary["period"] == today.replace(day=1).isoformat()
    view = inference.inference_summary(tenant_id, today)
    assert view["by_provider"]["anthropic"] == 4200.0  # reconciles with the bill


def test_anthropic_estimates_not_yet_billed_current_month(tenant_id, monkeypatch):
    # Cost Report (billed) has 1,000 tokens for $10; Usage Report shows 1,500 tokens
    # through today -> the extra 500 tokens are not yet billed. Estimate scales the
    # bill by 500/1000 = $5.00, stored separately as source='cost_api_est'.
    from meter.db import app_dsn, connect, tenant_tx

    today = dt.date.today()

    class _FakeAnthropic:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def fetch_costs(self, period):
            return [
                CostRecord(
                    "anthropic",
                    period,
                    Decimal("10.00"),
                    project="ws",
                    model="c",
                    tokens_in=800,
                    tokens_out=200,
                )
            ]

        def fetch_usage(self, period):
            return [
                UsageRecord(
                    workspace_id="ws",
                    api_key_id="k",
                    model="c",
                    tokens_in=1200,
                    tokens_out=300,
                    request_count=10,
                )
            ]

        def fetch_workspaces(self):
            return {"ws": "Workspace"}

        def fetch_api_keys(self):
            return {"k": {"name": "key", "workspace_id": "ws"}}

    monkeypatch.setattr(inference, "_make_cost_client", lambda provider, key: _FakeAnthropic())
    summary = inference.run_inference_ingest(tenant_id, "anthropic", today, "admin-key")

    assert summary["total"] == 10.0  # authoritative billed, unchanged
    assert summary["estimated"] == 5.0  # 10 * (1500-1000)/1000

    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        billed, est = conn.execute(
            "SELECT "
            "COALESCE(SUM(amount) FILTER (WHERE source='cost_api'), 0), "
            "COALESCE(SUM(amount) FILTER (WHERE source='cost_api_est'), 0) "
            "FROM inference_cost WHERE provider='anthropic' AND period=%s",
            (today.replace(day=1),),
        ).fetchone()
    assert float(billed) == 10.0  # the bill stays authoritative and separate
    assert float(est) == 5.0  # estimate stored under its own source

    # The billed-only summary (Cost Sources) excludes the estimate.
    assert inference.inference_summary(tenant_id, today)["by_provider"]["anthropic"] == 10.0


def test_daily_rows_roll_up_to_the_monthly_total(tenant_id, monkeypatch):
    # Anthropic reports 3 daily cost buckets across a month; the daily table keeps
    # each day, and summing them equals the single monthly inference_cost total.
    from meter.db import app_dsn, connect, tenant_tx

    d1, d2, d3 = dt.date(2026, 5, 4), dt.date(2026, 5, 5), dt.date(2026, 5, 20)

    class _FakeAnthropic:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def fetch_costs(self, period):  # 3 days, one workspace
            return [
                CostRecord(
                    "anthropic",
                    d,
                    Decimal(str(amt)),
                    project="ws",
                    model="c",
                    tokens_in=100,
                    tokens_out=50,
                )
                for d, amt in [(d1, 30), (d2, 45), (d3, 25)]
            ]

        def fetch_usage(self, period):
            return [
                UsageRecord(
                    workspace_id="ws",
                    api_key_id="k",
                    model="c",
                    tokens_in=100,
                    tokens_out=50,
                    request_count=1,
                    day=d,
                )
                for d in (d1, d2, d3)
            ]

        def fetch_workspaces(self):
            return {"ws": "Workspace"}

        def fetch_api_keys(self):
            return {"k": {"name": "key", "workspace_id": "ws"}}

    monkeypatch.setattr(inference, "_make_cost_client", lambda provider, key: _FakeAnthropic())
    inference.run_inference_ingest(tenant_id, "anthropic", dt.date(2026, 5, 1), "key")

    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        monthly = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM inference_cost "
            "WHERE provider='anthropic' AND period=%s AND source='cost_api'",
            (dt.date(2026, 5, 1),),
        ).fetchone()[0]
        daily = conn.execute(
            "SELECT day, SUM(amount) FROM inference_cost_daily "
            "WHERE provider='anthropic' AND source='cost_api' GROUP BY day ORDER BY day",
            (),
        ).fetchall()

    assert float(monthly) == 100.0  # 30 + 45 + 25
    # Three distinct days persisted, each with its own amount, summing to the month.
    assert {d.isoformat(): float(a) for d, a in daily} == {
        "2026-05-04": 30.0,
        "2026-05-05": 45.0,
        "2026-05-20": 25.0,
    }
    assert round(sum(float(a) for _d, a in daily), 2) == float(monthly)


def test_estimate_is_day_precise_when_usage_has_daily_detail(tenant_id, monkeypatch):
    # Billed through the 18th ($100 for 1,000 tokens => $0.10/token). Usage on the
    # 19th and 20th (200 tokens) is not yet billed -> precise estimate = 200*0.10=$20,
    # NOT the coarse whole-month ratio.
    today = dt.date.today()
    m = today.replace(day=1)
    billed_day, d19, d20 = m.replace(day=18), m.replace(day=19), m.replace(day=20)

    class _FakeAnthropic:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def fetch_costs(self, period):  # billed only through the 18th
            return [
                CostRecord(
                    "anthropic",
                    billed_day,
                    Decimal("100.00"),
                    project="ws",
                    model="c",
                    tokens_in=800,
                    tokens_out=200,
                )
            ]

        def fetch_usage(self, period):  # billed day + two trailing unbilled days
            return [
                UsageRecord(
                    workspace_id="ws",
                    api_key_id="k",
                    model="c",
                    tokens_in=800,
                    tokens_out=200,
                    day=billed_day,
                ),  # billed
                UsageRecord(
                    workspace_id="ws",
                    api_key_id="k",
                    model="c",
                    tokens_in=80,
                    tokens_out=20,
                    day=d19,
                ),  # not billed
                UsageRecord(
                    workspace_id="ws",
                    api_key_id="k",
                    model="c",
                    tokens_in=80,
                    tokens_out=20,
                    day=d20,
                ),  # not billed
            ]

        def fetch_workspaces(self):
            return {"ws": "Workspace"}

        def fetch_api_keys(self):
            return {"k": {"name": "key", "workspace_id": "ws"}}

    monkeypatch.setattr(inference, "_make_cost_client", lambda provider, key: _FakeAnthropic())
    summary = inference.run_inference_ingest(tenant_id, "anthropic", today, "key")
    assert summary["total"] == 100.0  # billed authority
    assert summary["estimated"] == 20.0  # 200 trailing tokens * $0.10/token (day-precise)


def test_anthropic_no_estimate_for_a_past_month(tenant_id, monkeypatch):
    # A fully-elapsed month is entirely billed -> no estimate, even if usage > billed.
    class _FakeAnthropic:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def fetch_costs(self, period):
            return [CostRecord("anthropic", period, Decimal("10.00"), project="ws", tokens_in=1000)]

        def fetch_usage(self, period):
            return [UsageRecord(workspace_id="ws", api_key_id="k", tokens_in=5000)]

        def fetch_workspaces(self):
            return {"ws": "Workspace"}

        def fetch_api_keys(self):
            return {"k": {"name": "key", "workspace_id": "ws"}}

    monkeypatch.setattr(inference, "_make_cost_client", lambda provider, key: _FakeAnthropic())
    summary = inference.run_inference_ingest(tenant_id, "anthropic", dt.date(2025, 1, 1), "key")
    assert summary["estimated"] == 0.0


def test_reingest_is_idempotent(tenant_id):
    _mapped_tenant(tenant_id)
    records = [_record("anthropic", 4200, api_key_ref="key:triage")]
    inference.ingest_records(tenant_id, "anthropic", PERIOD, records)
    inference.ingest_records(tenant_id, "anthropic", PERIOD, records)  # run again

    summary = inference.inference_summary(tenant_id, PERIOD)
    assert summary["by_provider"]["anthropic"] == 4200.0  # not doubled


def test_run_inference_ingest_with_injected_client(tenant_id, monkeypatch):
    _mapped_tenant(tenant_id)

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def fetch_costs(self, period):
            return [_record("anthropic", 4200, api_key_ref="key:triage")]

    monkeypatch.setattr(inference, "_make_cost_client", lambda provider, key: _FakeClient())
    summary = inference.run_inference_ingest(tenant_id, "anthropic", PERIOD, "admin-key")
    assert summary["attributed"] == 4200.0


def test_unmapped_tenant_all_unattributed(tenant_id):
    records = [_record("openai", 500, project="proj_x"), _record("openai", 300, api_key_ref="k")]
    inference.ingest_records(tenant_id, "openai", PERIOD, records)
    summary = inference.inference_summary(tenant_id, PERIOD)
    assert summary["features"] == []
    assert summary["unattributed"] == 800.0
    assert summary["total"] == 800.0


# ---------------------------------------------------------------------------
# Anthropic detailed path: workspace/API-key identity, environment, reconcile.
# ---------------------------------------------------------------------------
def _usage(ws, key, model, tin, tout, *, tier=None, cached=0, reqs=0, day=None):
    return UsageRecord(
        workspace_id=ws,
        api_key_id=key,
        model=model,
        service_tier=tier,
        tokens_in=tin,
        tokens_out=tout,
        cached_tokens_in=cached,
        request_count=reqs,
        day=day,
    )


def _cost(ws, amount):
    return CostRecord("anthropic", PERIOD, Decimal(str(amount)), project=ws)


_WORKSPACES = {"ws_mcs": "mcs-dev", "ws_sos": "sos-dev"}
_API_KEYS = {
    "k_a": {"name": "service-a-prod", "workspace_id": "ws_mcs"},
    "k_b": {"name": "experimental", "workspace_id": "ws_mcs"},
    "k_c": {"name": "pentest-prod", "workspace_id": "ws_sos"},
}


def test_anthropic_reconciles_to_cost_report_total(tenant_id):
    cost = [_cost("ws_mcs", 1000), _cost("ws_sos", 200)]
    usage = [
        _usage("ws_mcs", "k_a", "claude-sonnet-4-6", 1_000_000, 1_000_000),
        _usage("ws_mcs", "k_b", "claude-sonnet-4-6", 1_000_000, 0),
        _usage("ws_sos", "k_c", "claude-haiku-4-5", 500_000, 100_000),
    ]
    summary = inference.ingest_anthropic(tenant_id, PERIOD, cost, usage, _WORKSPACES, _API_KEYS)
    # INVARIANT: persisted total == authoritative Cost Report total, to the cent.
    assert summary["total"] == 1200.0
    detail = inference.anthropic_resource_detail(tenant_id, PERIOD)
    assert sum(r["cost"] for r in detail["rows"]) == pytest.approx(1200.0, abs=0.01)


def test_anthropic_resources_default_unclassified_no_name_inference(tenant_id):
    # "service-a-prod" must NOT auto-become production. Everything starts unclassified.
    cost = [_cost("ws_mcs", 100)]
    usage = [
        _usage("ws_mcs", "k_a", "claude-sonnet-4-6", 1_000_000, 0),
        _usage("ws_mcs", "k_b", "claude-sonnet-4-6", 1_000_000, 0),
    ]
    inference.ingest_anthropic(tenant_id, PERIOD, cost, usage, _WORKSPACES, _API_KEYS)
    detail = inference.anthropic_resource_detail(tenant_id, PERIOD)
    by_name = {r["name"]: r for r in detail["rows"]}
    assert by_name["service-a-prod"]["classification"] == "unclassified"
    assert by_name["service-a-prod"]["group"] == "mcs-dev"
    assert by_name["experimental"]["classification"] == "unclassified"
    # Detail is one flat table (no by_environment / by_workspace summaries).
    assert set(detail) == {"provider", "period", "all_time", "classifiable", "columns", "rows"}
    assert detail["columns"] == {"group": "Workspace", "name": "API key"}


def test_classifying_a_key_restamps_existing_cost_rows(tenant_id):
    # Reproduces the bug: after ingest stamps a key 'unclassified', classifying it
    # production must re-stamp the persisted rows so the Overview trend shows the
    # spend under Production immediately — not only after the next re-sync.
    from meter import dashboard, resources
    from meter.db import app_dsn, connect, tenant_tx

    inference.ingest_anthropic(
        tenant_id,
        PERIOD,
        [_cost("ws_mcs", 100)],
        [_usage("ws_mcs", "k_a", "claude-sonnet-4-6", 1000, 0, day=PERIOD)],
        _WORKSPACES,
        _API_KEYS,
    )

    def _env(table):
        with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
            return conn.execute(
                f"SELECT DISTINCT environment FROM {table} WHERE api_key_id = 'k_a'"  # noqa: S608
            ).fetchall()

    assert _env("inference_cost") == [("unclassified",)]
    assert _env("inference_cost_daily") == [("unclassified",)]

    resources.set_classification(tenant_id, "anthropic", "api_key", "k_a", "production")

    # Both the monthly and daily rows are now production...
    assert _env("inference_cost") == [("production",)]
    assert _env("inference_cost_daily") == [("production",)]
    # ...so the Overview trend buckets the spend under Production, not Unclassified.
    board = dashboard.spend_by_provider(tenant_id, range_token="this_month")
    assert board["trend"][0]["production"] == 100.0
    assert board["trend"][0]["unclassified"] == 0.0


def test_resource_detail_lists_all_history_when_no_period(tenant_id):
    # A key that only spent in an EARLIER month must still be classifiable (the
    # Configure panel defaults to all-time), with cost totalled across months.
    apr = dt.date(2026, 4, 1)
    inference.ingest_anthropic(
        tenant_id,
        apr,
        [_cost("ws_sos", 40)],
        [_usage("ws_sos", "k_c", "claude-haiku-4-5", 100, 0)],
        _WORKSPACES,
        _API_KEYS,
    )
    inference.ingest_anthropic(
        tenant_id,
        PERIOD,
        [_cost("ws_mcs", 100)],
        [_usage("ws_mcs", "k_a", "claude-sonnet-4-6", 100, 0)],
        _WORKSPACES,
        _API_KEYS,
    )

    # Single-month (May) sees only May's key.
    may_only = {r["name"] for r in inference.anthropic_resource_detail(tenant_id, PERIOD)["rows"]}
    assert "service-a-prod" in may_only and "pentest-prod" not in may_only

    # All-time (no period) lists BOTH months' keys, with cost summed across history.
    detail = inference.anthropic_resource_detail(tenant_id)
    assert detail["all_time"] is True
    by_name = {r["name"]: r for r in detail["rows"]}
    assert {"service-a-prod", "pentest-prod"} <= set(by_name)
    assert by_name["pentest-prod"]["cost"] == 40.0  # the April-only key is classifiable


def test_anthropic_manual_classification_persists_and_survives_resync(tenant_id):
    cost = [_cost("ws_mcs", 100)]
    usage = [
        _usage("ws_mcs", "k_a", "claude-sonnet-4-6", 1_000_000, 0),
        _usage("ws_mcs", "k_b", "claude-sonnet-4-6", 1_000_000, 0),
    ]
    inference.ingest_anthropic(tenant_id, PERIOD, cost, usage, _WORKSPACES, _API_KEYS)
    # User classifies one key production. It shows immediately in the detail.
    resources.set_classification(tenant_id, "anthropic", "api_key", "k_a", "production")
    detail = inference.anthropic_resource_detail(tenant_id, PERIOD)
    by_name = {r["name"]: r for r in detail["rows"]}
    assert by_name["service-a-prod"]["classification"] == "production"

    # A later sync must NOT overwrite the manual choice, and must snapshot it onto
    # the cost rows so reporting reflects production.
    inference.ingest_anthropic(tenant_id, PERIOD, cost, usage, _WORKSPACES, _API_KEYS)
    assert resources.get_classifications(tenant_id, "anthropic")[("api_key", "k_a")] == "production"
    summary = inference.inference_summary(tenant_id, PERIOD)
    assert summary["by_provider"]["anthropic"] == pytest.approx(100.0, abs=0.01)


def test_anthropic_workspace_without_usage_is_not_lost(tenant_id):
    summary = inference.ingest_anthropic(
        tenant_id, PERIOD, [_cost("ws_mcs", 500)], [], _WORKSPACES, _API_KEYS
    )
    assert summary["total"] == 500.0
    detail = inference.anthropic_resource_detail(tenant_id, PERIOD)
    assert sum(r["cost"] for r in detail["rows"]) == pytest.approx(500.0, abs=0.01)
    assert all(r["classification"] == "unclassified" for r in detail["rows"])


def test_ignore_excluded_from_reporting_totals(tenant_id):
    cost = [_cost("ws_mcs", 100)]
    usage = [
        _usage("ws_mcs", "k_a", "claude-sonnet-4-6", 1_000_000, 0),  # -> ignore
        _usage("ws_mcs", "k_b", "claude-sonnet-4-6", 1_000_000, 0),  # -> unclassified
    ]
    inference.ingest_anthropic(tenant_id, PERIOD, cost, usage, _WORKSPACES, _API_KEYS)
    resources.set_classification(tenant_id, "anthropic", "api_key", "k_a", "ignore")
    inference.ingest_anthropic(
        tenant_id, PERIOD, cost, usage, _WORKSPACES, _API_KEYS
    )  # re-snapshot

    # The ignored $50 is excluded from reporting; the other $50 remains.
    summary = inference.inference_summary(tenant_id, PERIOD)
    assert summary["by_provider"]["anthropic"] == pytest.approx(50.0, abs=0.01)
    # But the source detail still shows every resource (auditability).
    detail = inference.anthropic_resource_detail(tenant_id, PERIOD)
    assert sum(r["cost"] for r in detail["rows"]) == pytest.approx(100.0, abs=0.01)


def test_run_inference_ingest_routes_anthropic_to_detailed_path(tenant_id, monkeypatch):
    class _FakeAnthropic:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def fetch_costs(self, period):
            return [_cost("ws_mcs", 1000)]

        def fetch_usage(self, period):
            return [
                _usage("ws_mcs", "k_a", "claude-sonnet-4-6", 1_000_000, 0),
                _usage("ws_mcs", "k_b", "claude-sonnet-4-6", 1_000_000, 0),
            ]

        def fetch_workspaces(self):
            return _WORKSPACES

        def fetch_api_keys(self):
            return _API_KEYS

    monkeypatch.setattr(inference, "_make_cost_client", lambda provider, key: _FakeAnthropic())
    summary = inference.run_inference_ingest(tenant_id, "anthropic", PERIOD, "admin-key")
    assert summary["total"] == 1000.0
    detail = inference.anthropic_resource_detail(tenant_id, PERIOD)
    assert {r["group"] for r in detail["rows"]} == {"mcs-dev"}
    assert all(r["classification"] == "unclassified" for r in detail["rows"])


# ---------------------------------------------------------------------------
# Multi-month backfill (Sync now pulls history, not just the current month).
# ---------------------------------------------------------------------------
def test_backfill_ingests_each_month_of_the_window(tenant_id, monkeypatch):
    seen: list = []

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def fetch_costs(self, period):
            seen.append(period)
            return [_record("openai", 100, project="proj_x")]

    monkeypatch.setattr(inference, "_make_cost_client", lambda provider, key: _FakeClient())
    anchor = dt.date(2026, 8, 1)
    summary = inference.run_inference_backfill(tenant_id, "openai", "key", months=12, anchor=anchor)

    assert len(seen) == 12  # one fetch per month
    assert min(seen) == dt.date(2025, 9, 1)  # 12 months back, inclusive
    assert max(seen) == anchor
    assert summary["months"] == 12
    assert len(summary["by_month"]) == 12
    assert summary["total"] == 1200.0  # 12 * 100
    # Persisted across the window: last-12-months range sees every month.
    view = inference.inference_summary(tenant_id, dt.date(2026, 8, 1))
    assert view["by_provider"]["openai"] == 100.0  # each month stored separately


def test_backfill_is_idempotent_per_month(tenant_id, monkeypatch):
    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def fetch_costs(self, period):
            return [_record("openai", 50, project="proj_x")]

    monkeypatch.setattr(inference, "_make_cost_client", lambda provider, key: _FakeClient())
    anchor = dt.date(2026, 8, 1)
    inference.run_inference_backfill(tenant_id, "openai", "key", months=3, anchor=anchor)
    inference.run_inference_backfill(tenant_id, "openai", "key", months=3, anchor=anchor)  # again
    # A month's total is not doubled by re-running the backfill.
    view = inference.inference_summary(tenant_id, anchor)
    assert view["by_provider"]["openai"] == 50.0


def test_backfill_survives_a_single_bad_month(tenant_id, monkeypatch):
    class _FlakyClient:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def fetch_costs(self, period):
            if period == dt.date(2026, 7, 1):
                raise RuntimeError("transient provider error")
            return [_record("openai", 10, project="proj_x")]

    monkeypatch.setattr(inference, "_make_cost_client", lambda provider, key: _FlakyClient())
    summary = inference.run_inference_backfill(
        tenant_id, "openai", "key", months=3, anchor=dt.date(2026, 8, 1)
    )
    assert len(summary["by_month"]) == 2  # June + August landed
    assert len(summary["errors"]) == 1  # July recorded, not fatal
    assert summary["errors"][0]["period"] == "2026-07-01"


def test_backfill_raises_when_every_month_fails(tenant_id, monkeypatch):
    class _DeadClient:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def fetch_costs(self, period):
            raise RuntimeError("bad admin key")

    monkeypatch.setattr(inference, "_make_cost_client", lambda provider, key: _DeadClient())
    with pytest.raises(RuntimeError, match="bad admin key"):
        inference.run_inference_backfill(tenant_id, "openai", "key", months=3)


# ---- Several accounts under one connector ---------------------------------
# A tenant can hold two billing accounts with the same provider. Ingestion is
# idempotent by clearing the provider's month and rewriting it, so ingesting one
# account at a time would have the second write delete the first — the customer
# would see one account's bill and nothing would look wrong.
class _AccountClient:
    """A cost API for one account, returning that account's own spend."""

    def __init__(self, key):
        self._key = key

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def fetch_costs(self, period):
        amounts = {"key-acme": "300.00", "key-labs": "120.00"}
        return [
            CostRecord(
                "openai",
                period,
                Decimal(amounts[self._key]),
                api_key_ref=f"ref:{self._key}",
                model="gpt-4o",
            )
        ]


def test_two_accounts_on_one_provider_are_summed_not_overwritten(tenant_id, monkeypatch):
    monkeypatch.setattr(inference, "_make_cost_client", lambda provider, key: _AccountClient(key))

    summary = inference.run_inference_ingest(
        tenant_id, "openai", PERIOD, ["key-acme", "key-labs"]
    )
    # $300 + $120. Reading one account would have reported one of them.
    assert summary["total"] == pytest.approx(420.0)

    dash = inference.inference_summary(tenant_id, PERIOD)
    assert dash["by_provider"]["openai"] == pytest.approx(420.0)


def test_re_ingesting_two_accounts_stays_idempotent(tenant_id, monkeypatch):
    monkeypatch.setattr(inference, "_make_cost_client", lambda provider, key: _AccountClient(key))
    keys = ["key-acme", "key-labs"]

    inference.run_inference_ingest(tenant_id, "openai", PERIOD, keys)
    inference.run_inference_ingest(tenant_id, "openai", PERIOD, keys)

    # Twice the same month must not be twice the money.
    assert inference.inference_summary(tenant_id, PERIOD)["by_provider"]["openai"] == pytest.approx(
        420.0
    )


def test_one_broken_key_does_not_rewrite_the_month_with_half_a_bill(tenant_id, monkeypatch):
    # Clearing the month and rewriting it from the healthy account alone would
    # silently halve the customer's bill. Last-good data must stand instead.
    monkeypatch.setattr(inference, "_make_cost_client", lambda provider, key: _AccountClient(key))
    inference.run_inference_ingest(tenant_id, "openai", PERIOD, ["key-acme", "key-labs"])

    class _Expired(_AccountClient):
        def fetch_costs(self, period):
            if self._key == "key-labs":
                raise RuntimeError("401 Unauthorized")
            return super().fetch_costs(period)

    monkeypatch.setattr(inference, "_make_cost_client", lambda provider, key: _Expired(key))
    with pytest.raises(RuntimeError):
        inference.run_inference_ingest(tenant_id, "openai", PERIOD, ["key-acme", "key-labs"])

    assert inference.inference_summary(tenant_id, PERIOD)["by_provider"]["openai"] == pytest.approx(
        420.0
    )


def test_a_single_key_still_works_unchanged(tenant_id, monkeypatch):
    # The old call shape — one key as a plain string — is what most callers pass.
    monkeypatch.setattr(inference, "_make_cost_client", lambda provider, key: _AccountClient(key))
    summary = inference.run_inference_ingest(tenant_id, "openai", PERIOD, "key-acme")
    assert summary["total"] == pytest.approx(300.0)
