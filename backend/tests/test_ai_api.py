"""The request-level API: filters, sorting, paging, isolation, retention."""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from meter import ai_reads, traces
from meter.api import create_app
from meter.db import app_dsn, connect, tenant_tx

PASSWORD = "correct horse battery"
NOW = dt.datetime.now(dt.timezone.utc)


@pytest.fixture
def client(admin_conn, admin_conninfo, app_conninfo, monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "unit-test-secret-key")
    monkeypatch.setenv("DATABASE_URL", admin_conninfo)
    monkeypatch.setenv("DATABASE_APP_URL", app_conninfo)
    c = TestClient(create_app())
    c.post("/api/auth/signup", json={"email": "cto@acme.com", "password": PASSWORD})
    return c


def tenant_of(client) -> str:
    return client.get("/api/auth/me").json()["tenant_id"]


def ev(kind, trace, **over):
    base = {
        "event_type": kind,
        "trace_id": trace,
        "application": "support-agent",
        "operation_name": "resolve-ticket",
        "environment": "production",
        "occurred_at": NOW.isoformat(),
    }
    base.update(over)
    return base


def llm(trace, span, **over):
    fields = {
        "span_id": span,
        "span_kind": "llm",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "tokens_in": 1000,
        "tokens_out": 500,
        "operation_name": "classify",
    }
    fields.update(over)
    return ev("span.completed", trace, **fields)


def seed(client, count=3):
    tenant = tenant_of(client)
    for i in range(count):
        traces.ingest(
            tenant,
            [
                ev("trace.started", f"t{i}", operation_name=f"run-{i}"),
                llm(f"t{i}", "s1", tokens_in=1000 * (i + 1)),
                ev("trace.completed", f"t{i}"),
            ],
        )
    return tenant


# --- applications ----------------------------------------------------------
def test_applications_list_is_empty_before_anything_runs(client):
    body = client.get("/api/ai/applications").json()
    assert body["applications"] == []


def test_applications_report_economics_for_the_window(client):
    seed(client)
    (app,) = client.get("/api/ai/applications").json()["applications"]

    assert app["slug"] == "support-agent"
    assert app["runs"] == 3
    assert app["spend"] > 0
    assert app["tokens"] == (1000 + 2000 + 3000) + (500 * 3)
    assert app["cost_per_run"] == pytest.approx(app["spend"] / 3)
    assert app["error_rate"] == 0
    assert "tenant_id" not in app


def test_an_application_with_no_runs_reports_no_cost_per_run_rather_than_zero(client):
    # "Free" and "nothing happened" are different answers.
    client.post("/api/ai/applications", json={"name": "Document Review"})
    (app,) = client.get("/api/ai/applications").json()["applications"]
    assert app["runs"] == 0
    assert app["cost_per_run"] is None
    assert app["error_rate"] is None


def test_an_application_can_be_created_and_renamed_without_moving_its_slug(client):
    created = client.post("/api/ai/applications", json={"name": "Document Review"}).json()
    assert created["slug"] == "document-review"

    renamed = client.patch(
        f"/api/ai/applications/{created['id']}",
        json={"name": "Contract Review", "owner": "platform-team"},
    ).json()
    # The SDK resolves by slug; changing it would orphan every event in flight.
    assert renamed["slug"] == "document-review"
    assert renamed["name"] == "Contract Review"
    assert renamed["owner"] == "platform-team"


def test_a_duplicate_slug_is_refused(client):
    client.post("/api/ai/applications", json={"name": "Support Agent"})
    r = client.post("/api/ai/applications", json={"name": "support agent"})
    assert r.status_code == 400
    assert "already exists" in r.json()["detail"]


def test_application_detail_breaks_spend_down(client):
    seed(client)
    app_id = client.get("/api/ai/applications").json()["applications"][0]["id"]
    detail = client.get(f"/api/ai/applications/{app_id}").json()

    assert {b["model"] for b in detail["by_model"]} == {"claude-sonnet-4-6"}
    assert detail["by_feature"][0]["feature"] == "Unattributed"


def test_a_missing_application_is_a_404(client):
    r = client.get("/api/ai/applications/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


# --- traces ----------------------------------------------------------------
def test_traces_list_newest_first_by_default(client):
    seed(client)
    body = client.get("/api/ai/traces").json()
    assert body["total"] == 3
    assert len(body["traces"]) == 3
    assert [t["operation_name"] for t in body["traces"]][0].startswith("run-")
    assert "tenant_id" not in body["traces"][0]


@pytest.mark.parametrize("sort", ["newest", "expensive", "tokens", "slowest", "longest", "steps"])
def test_every_sort_returns_a_full_deterministic_page(client, sort):
    seed(client)
    first = client.get(f"/api/ai/traces?sort={sort}").json()["traces"]
    second = client.get(f"/api/ai/traces?sort={sort}").json()["traces"]
    assert [t["id"] for t in first] == [t["id"] for t in second]
    assert len(first) == 3


def test_a_caller_cannot_reach_the_order_by_clause(client):
    seed(client)
    # Two layers, both real. A long injection string never reaches the handler:
    # the parameter is length-bounded and FastAPI refuses it.
    assert client.get("/api/ai/traces?sort=total_cost;DROP TABLE ai_span").status_code == 422
    # And a short unknown sort is looked up in a closed map, so it falls back to
    # the default rather than being interpolated into the query.
    body = client.get("/api/ai/traces?sort=1;DROP").json()
    assert body["total"] == 3
    assert [t["id"] for t in body["traces"]] == [
        t["id"] for t in client.get("/api/ai/traces?sort=newest").json()["traces"]
    ]


def test_most_expensive_sorts_by_cost(client):
    seed(client)
    body = client.get("/api/ai/traces?sort=expensive").json()["traces"]
    costs = [t["total_cost"] for t in body]
    assert costs == sorted(costs, reverse=True)


def test_paging_is_stable_and_bounded(client):
    seed(client, count=5)
    page1 = client.get("/api/ai/traces?limit=2&offset=0").json()
    page2 = client.get("/api/ai/traces?limit=2&offset=2").json()

    assert page1["total"] == page2["total"] == 5
    assert len(page1["traces"]) == len(page2["traces"]) == 2
    assert not {t["id"] for t in page1["traces"]} & {t["id"] for t in page2["traces"]}
    # A caller cannot ask for an unbounded page.
    assert client.get(f"/api/ai/traces?limit={ai_reads.MAX_PAGE + 500}").status_code == 422


@pytest.mark.parametrize(
    "query, expected",
    [
        ("provider=anthropic", 3),
        ("provider=openai", 0),
        ("model=claude-sonnet-4-6", 3),
        ("environment=production", 3),
        ("environment=staging", 0),
        ("trace_status=success", 3),
        ("trace_status=error", 0),
        ("min_cost=1000", 0),
    ],
)
def test_filters_narrow_the_listing(client, query, expected):
    seed(client)
    assert client.get(f"/api/ai/traces?{query}").json()["total"] == expected


def test_prompt_identity_is_filterable(client):
    tenant = tenant_of(client)
    traces.ingest(tenant, [llm("t1", "s1", prompt_id="classify", prompt_version="2.1")])
    traces.ingest(tenant, [llm("t2", "s1", prompt_id="answer", prompt_version="1.0")])

    assert client.get("/api/ai/traces?prompt_id=classify").json()["total"] == 1
    assert client.get("/api/ai/traces?prompt_version=1.0").json()["total"] == 1


# --- running and stale -----------------------------------------------------
def test_running_and_stale_are_derived_from_one_stored_status(client):
    tenant = tenant_of(client)
    traces.ingest(tenant, [ev("trace.started", "live")])
    traces.ingest(tenant, [ev("trace.started", "quiet")])
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant):
        conn.execute(
            "UPDATE ai_trace SET last_activity_at = now() - interval '3 hours' "
            "WHERE external_trace_id = 'quiet'"
        )

    running = client.get("/api/ai/traces?running=true").json()
    stale = client.get("/api/ai/traces?stale=true").json()

    assert [t["trace_id"] for t in running["traces"]] == ["live"]
    assert [t["trace_id"] for t in stale["traces"]] == ["quiet"]
    # Both are stored as `running`; only the presentation differs.
    assert stale["traces"][0]["status"] == "running"
    assert stale["traces"][0]["live_status"] == "stale"
    assert running["traces"][0]["live_status"] == "running"


def test_becoming_stale_changes_no_money(client):
    tenant = tenant_of(client)
    traces.ingest(tenant, [ev("trace.started", "t1"), llm("t1", "s1")])
    before = client.get("/api/ai/traces").json()["traces"][0]["total_cost"]
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant):
        conn.execute("UPDATE ai_trace SET last_activity_at = now() - interval '3 hours'")

    after = client.get("/api/ai/traces?stale=true").json()["traces"][0]
    assert after["total_cost"] == before


# --- trace detail ----------------------------------------------------------
def test_trace_detail_returns_ordered_spans_with_their_parents(client):
    tenant = tenant_of(client)
    traces.ingest(
        tenant,
        [
            ev("trace.started", "t1"),
            ev(
                "span.completed",
                "t1",
                span_id="root",
                span_kind="workflow",
                operation_name="resolve",
            ),
            llm("t1", "child", parent_span_id="root"),
            ev("trace.completed", "t1"),
        ],
    )
    detail = client.get("/api/ai/traces/t1").json()

    assert detail["live_status"] == "success"
    assert detail["application"]["slug"] == "support-agent"
    kinds = {s["external_span_id"]: s for s in detail["spans"]}
    assert kinds["child"]["parent_span_id"] == "root"
    assert kinds["child"]["tokens_in"] == 1000
    assert kinds["child"]["amount"] > 0
    assert kinds["root"]["amount"] == 0


def test_a_span_whose_parent_never_arrived_is_flagged_not_dropped(client):
    tenant = tenant_of(client)
    traces.ingest(tenant, [llm("t1", "orphan", parent_span_id="never-sent")])
    detail = client.get("/api/ai/traces/t1").json()

    (span,) = detail["spans"]
    assert span["parent_span_id"] == "never-sent"
    assert span["parent_missing"] is True


def test_trace_detail_never_returns_internal_or_content_fields(client):
    seed(client, count=1)
    detail = client.get("/api/ai/traces/t0").json()
    blob = str(detail)
    for banned in (
        "tenant_id",
        "prompt_text",
        "response",
        "messages",
        "stack_trace",
        "token_hash",
        "ciphertext",
    ):
        assert banned not in blob


def test_a_missing_trace_is_a_404(client):
    assert client.get("/api/ai/traces/does-not-exist").status_code == 404


# --- auth and isolation ----------------------------------------------------
@pytest.mark.parametrize("path", ["/api/ai/applications", "/api/ai/traces", "/api/ai/traces/t1"])
def test_every_route_requires_a_session(client, path):
    client.post("/api/auth/logout")
    assert client.get(path).status_code == 401


def test_one_tenant_never_sees_another_s_traces_through_the_api(client, admin_conn):
    seed(client, count=2)
    other = str(
        admin_conn.execute("INSERT INTO tenant (name) VALUES ('Other') RETURNING id").fetchone()[0]
    )
    admin_conn.commit()
    traces.ingest(other, [llm("secret-trace", "s1")])

    body = client.get("/api/ai/traces").json()
    assert body["total"] == 2
    assert "secret-trace" not in str(body)
    assert client.get("/api/ai/traces/secret-trace").status_code == 404


# --- retention -------------------------------------------------------------
def test_retention_deletes_old_traces_and_their_spans(client):
    tenant = tenant_of(client)
    traces.ingest(
        tenant, [ev("trace.started", "old"), llm("old", "s1"), ev("trace.completed", "old")]
    )
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant):
        conn.execute("UPDATE ai_trace SET started_at = now() - interval '400 days'")

    result = ai_reads.purge_expired_traces(tenant)

    assert result["deleted"] == 1
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant):
        assert conn.execute("SELECT count(*) FROM ai_trace").fetchone()[0] == 0
        # Spans cascade from the trace.
        assert conn.execute("SELECT count(*) FROM ai_span").fetchone()[0] == 0


def test_retention_never_touches_the_money(client):
    # What a month cost is a fact about the month, not about how long we keep
    # the traces that explain it.
    tenant = tenant_of(client)
    traces.ingest(
        tenant, [llm("old", "s1", customer_id="customer-1"), ev("trace.completed", "old")]
    )
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant):
        before = conn.execute(
            "SELECT (SELECT COALESCE(SUM(amount),0) FROM inference_cost),"
            "       (SELECT COALESCE(SUM(amount),0) FROM customer_cost),"
            "       (SELECT COALESCE(SUM(request_count),0) FROM inference_cost)"
        ).fetchone()
        conn.execute("UPDATE ai_trace SET started_at = now() - interval '400 days'")

    ai_reads.purge_expired_traces(tenant)

    with connect(app_dsn()) as conn, tenant_tx(conn, tenant):
        after = conn.execute(
            "SELECT (SELECT COALESCE(SUM(amount),0) FROM inference_cost),"
            "       (SELECT COALESCE(SUM(amount),0) FROM customer_cost),"
            "       (SELECT COALESCE(SUM(request_count),0) FROM inference_cost)"
        ).fetchone()
    assert after == before
    assert before[0] > 0


def test_retention_leaves_a_running_agent_alone(client):
    # A long-lived agent that started before the window must not be deleted out
    # from under itself.
    tenant = tenant_of(client)
    traces.ingest(tenant, [ev("trace.started", "long-runner")])
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant):
        conn.execute("UPDATE ai_trace SET started_at = now() - interval '400 days'")

    assert ai_reads.purge_expired_traces(tenant)["deleted"] == 0


def test_retention_is_rerunnable(client):
    tenant = tenant_of(client)
    traces.ingest(tenant, [llm("old", "s1"), ev("trace.completed", "old")])
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant):
        conn.execute("UPDATE ai_trace SET started_at = now() - interval '400 days'")

    assert ai_reads.purge_expired_traces(tenant)["deleted"] == 1
    assert ai_reads.purge_expired_traces(tenant)["deleted"] == 0


# --- settings --------------------------------------------------------------
def test_the_new_settings_have_safe_defaults(client):
    body = client.get("/api/settings").json()
    assert body["trace_retention_days"] == 30
    assert body["agent_stale_after_minutes"] == 10
    assert body["content_capture"] == "disabled"


@pytest.mark.parametrize("days", [7, 30, 90])
def test_trace_retention_accepts_the_supported_windows(client, days):
    r = client.patch("/api/settings", json={"trace_retention_days": days})
    assert r.status_code == 200
    assert r.json()["trace_retention_days"] == days


def test_an_unsupported_retention_window_is_refused(client):
    assert client.patch("/api/settings", json={"trace_retention_days": 365}).status_code == 400


def test_the_stale_threshold_is_bounded(client):
    assert client.patch("/api/settings", json={"agent_stale_after_minutes": 45}).status_code == 200
    assert client.patch("/api/settings", json={"agent_stale_after_minutes": 0}).status_code == 400
    assert (
        client.patch("/api/settings", json={"agent_stale_after_minutes": 100000}).status_code == 400
    )


@pytest.mark.parametrize("value", ["redacted", "full"])
def test_content_capture_cannot_be_enabled_through_the_api(client, value):
    # The reserved values exist in the schema so a future consented feature
    # needs no migration. Nothing may turn them on before that feature exists.
    r = client.patch("/api/settings", json={"content_capture": value})
    assert r.status_code == 400
    assert "cannot be enabled" in r.json()["detail"]
    assert client.get("/api/settings").json()["content_capture"] == "disabled"


def test_the_stale_threshold_setting_actually_moves_the_line(client):
    tenant = tenant_of(client)
    traces.ingest(tenant, [ev("trace.started", "t1")])
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant):
        conn.execute("UPDATE ai_trace SET last_activity_at = now() - interval '30 minutes'")

    # Default 10 minutes: quiet for 30 is stale.
    assert client.get("/api/ai/traces?stale=true").json()["total"] == 1
    client.patch("/api/settings", json={"agent_stale_after_minutes": 60})
    # Raised to 60: the same trace is simply still running.
    assert client.get("/api/ai/traces?stale=true").json()["total"] == 0
    assert client.get("/api/ai/traces?running=true").json()["total"] == 1


# ---- Trace search ---------------------------------------------------------
def test_search_narrows_traces_by_workflow_name(client):
    tenant = tenant_of(client)
    traces.ingest(tenant, [ev("trace.started", "t-a", operation_name="resolve-ticket")])
    traces.ingest(tenant, [ev("trace.started", "t-b", operation_name="summarize-report")])

    found = client.get("/api/ai/traces?q=resolve").json()
    assert [t["operation_name"] for t in found["traces"]] == ["resolve-ticket"]
    # The total must agree with the page, or paging lies about how much there is.
    assert found["total"] == 1

    assert client.get("/api/ai/traces?q=TICKET").json()["total"] == 1  # case-insensitive
    assert client.get("/api/ai/traces?q=nothing").json()["total"] == 0
    assert client.get("/api/ai/traces?q=").json()["total"] == 2  # empty is not a filter


def test_search_treats_like_metacharacters_as_text(client):
    # A workflow whose name contains LIKE's wildcards. Unescaped, "_" matches
    # any character and "%" matches everything, so a search would quietly
    # return rows the customer did not ask for.
    tenant = tenant_of(client)
    traces.ingest(tenant, [ev("trace.started", "t-a", operation_name="sync_user")])
    traces.ingest(tenant, [ev("trace.started", "t-b", operation_name="syncXuser")])
    traces.ingest(tenant, [ev("trace.started", "t-c", operation_name="100% coverage")])

    names = [t["operation_name"] for t in client.get("/api/ai/traces?q=sync_user").json()["traces"]]
    assert names == ["sync_user"]
    assert client.get("/api/ai/traces?q=100%25").json()["total"] == 1


def test_search_combines_with_the_other_filters(client):
    # Search narrows within a filter rather than replacing it — otherwise
    # typing in the box would silently widen the window someone had chosen.
    tenant = tenant_of(client)
    traces.ingest(tenant, [ev("trace.started", "t-a", operation_name="resolve-ticket")])
    traces.ingest(
        tenant,
        [ev("trace.started", "t-b", operation_name="resolve-ticket", environment="staging")],
    )
    assert client.get("/api/ai/traces?q=resolve").json()["total"] == 2
    assert client.get("/api/ai/traces?q=resolve&environment=staging").json()["total"] == 1
