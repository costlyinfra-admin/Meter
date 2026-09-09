"""Request-level ingestion: lifecycle, idempotency, money, and privacy."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from meter import applications, traces
from meter.db import app_dsn, connect, tenant_tx

NOW = dt.datetime.now(dt.timezone.utc)


def ev(kind, trace="t1", **over):
    base = {
        "event_type": kind,
        "trace_id": trace,
        "application": "support-agent",
        "operation_name": "resolve-ticket",
        "environment": "production",
        "occurred_at": NOW.isoformat(),
    }
    if kind.startswith("span."):
        base.update({"span_id": "s1", "span_kind": "llm", "operation_name": "classify"})
    base.update(over)
    return base


def llm(span_id, trace="t1", tin=1000, tout=500, **over):
    fields = {
        "span_id": span_id,
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "tokens_in": tin,
        "tokens_out": tout,
    }
    fields.update(over)
    return ev("span.completed", trace, **fields)


def rows(tenant_id, table, cols="*"):
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        return conn.execute(f"SELECT {cols} FROM {table}").fetchall()  # noqa: S608


def trace_row(tenant_id, external="t1"):
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        return conn.execute(
            "SELECT status, total_cost, total_tokens, span_count, ended_at, duration_ms, "
            "last_heartbeat_at, current_span_id FROM ai_trace WHERE external_trace_id = %s",
            (external,),
        ).fetchone()


def inference_total(tenant_id):
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        return conn.execute(
            "SELECT COALESCE(SUM(amount),0), COALESCE(SUM(request_count),0) "
            "FROM inference_cost WHERE source = 'hook'"
        ).fetchone()


# --- the simple case -------------------------------------------------------
def test_a_single_wrapped_call_becomes_a_one_span_trace(tenant_id):
    result = traces.ingest(tenant_id, [ev("trace.started"), llm("s1"), ev("trace.completed")])

    assert result["accepted"] == 3
    status, cost, tokens, spans, ended, duration, _, current = trace_row(tenant_id)
    assert status == "success"
    assert cost > 0 and tokens == 1500 and spans == 1
    assert ended is not None and duration is not None
    # A finished trace has no current operation.
    assert current is None


def test_an_llm_span_and_the_hook_agree_on_the_price(tenant_id):
    # §4: request-level evidence and monthly totals must be two views of one
    # number. Both paths must use the same pricing call on the same tokens.
    traces.ingest(tenant_id, [llm("s1", tin=1000, tout=500)])
    span_cost = rows(tenant_id, "ai_span", "amount")[0][0]
    total, count = inference_total(tenant_id)

    assert span_cost == total
    assert count == 1
    from meter.pricing import price

    assert span_cost == price("claude-sonnet-4-6", 1000, 500, "anthropic")


# --- idempotency -----------------------------------------------------------
def test_replaying_a_completion_adds_no_money(tenant_id):
    traces.ingest(tenant_id, [llm("s1")])
    first_cost, first_count = inference_total(tenant_id)

    for _ in range(3):
        traces.ingest(tenant_id, [llm("s1")])

    assert inference_total(tenant_id) == (first_cost, first_count)
    assert len(rows(tenant_id, "ai_span")) == 1
    _, cost, tokens, spans, *_ = trace_row(tenant_id)
    assert (cost, tokens, spans) == (first_cost, 1500, 1)


def test_replaying_a_whole_batch_is_a_no_op(tenant_id):
    batch = [ev("trace.started"), llm("s1"), llm("s2"), ev("trace.completed")]
    first = traces.ingest(tenant_id, batch, batch_id="b-1")
    second = traces.ingest(tenant_id, batch, batch_id="b-1")

    assert (second["accepted"], second["cost"]) == (first["accepted"], first["cost"])
    assert second["duplicate"] is True
    assert len(rows(tenant_id, "ai_span")) == 2
    assert inference_total(tenant_id)[1] == 2


def test_duplicate_span_starts_do_not_inflate_the_step_count(tenant_id):
    traces.ingest(tenant_id, [ev("span.started"), ev("span.started"), ev("span.started")])
    assert trace_row(tenant_id)[3] == 1


# --- lifecycle ordering ----------------------------------------------------
def test_a_completion_after_the_trace_looked_stale_still_completes_it(tenant_id):
    # Staleness is derived, never written, so there is no state to undo here.
    traces.ingest(tenant_id, [ev("trace.started")])
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        conn.execute("UPDATE ai_trace SET last_activity_at = now() - interval '3 hours'")

    traces.ingest(tenant_id, [ev("trace.completed")])
    assert trace_row(tenant_id)[0] == "success"


def test_a_heartbeat_never_reopens_a_finished_trace(tenant_id):
    traces.ingest(tenant_id, [ev("trace.started"), ev("trace.completed")])
    traces.ingest(tenant_id, [ev("trace.heartbeat")])
    assert trace_row(tenant_id)[0] == "success"


def test_a_late_started_event_never_reopens_a_finished_trace(tenant_id):
    traces.ingest(tenant_id, [ev("trace.failed")])
    traces.ingest(tenant_id, [ev("trace.started")])
    assert trace_row(tenant_id)[0] == "error"


def test_a_span_completion_may_arrive_before_its_start(tenant_id):
    traces.ingest(tenant_id, [llm("s1")])
    traces.ingest(tenant_id, [ev("span.started", span_id="s1")])

    (status, amount) = rows(tenant_id, "ai_span", "status, amount")[0]
    # Terminal is sticky: the late start must not put it back to running.
    assert status == "success"
    assert amount > 0
    assert inference_total(tenant_id)[1] == 1


def test_a_child_may_arrive_before_its_parent(tenant_id):
    traces.ingest(
        tenant_id,
        [
            ev("span.completed", span_id="child", parent_span_id="parent", span_kind="tool"),
            ev("span.completed", span_id="parent", span_kind="workflow"),
        ],
    )
    by_id = {r[0]: r[1] for r in rows(tenant_id, "ai_span", "external_span_id, parent_span_id")}
    # The relationship stays visible; ingestion did not fail over the ordering.
    assert by_id == {"child": "parent", "parent": None}


def test_a_missing_parent_does_not_break_ingestion(tenant_id):
    result = traces.ingest(
        tenant_id,
        [ev("span.completed", span_id="orphan", parent_span_id="never-sent", span_kind="tool")],
    )
    assert result["accepted"] == 1
    assert rows(tenant_id, "ai_span", "parent_span_id")[0][0] == "never-sent"


def test_the_first_span_names_a_trace_until_its_start_arrives(tenant_id):
    traces.ingest(tenant_id, [ev("span.completed", span_id="s1", operation_name="classify")])
    traces.ingest(tenant_id, [ev("trace.started", operation_name="resolve-ticket")])
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        name = conn.execute("SELECT operation_name FROM ai_trace").fetchone()[0]
    assert name == "resolve-ticket"


# --- what must never become money -----------------------------------------
@pytest.mark.parametrize(
    "kind",
    [
        "trace.started",
        "trace.heartbeat",
        "trace.completed",
        "trace.failed",
        "trace.cancelled",
        "span.started",
    ],
)
def test_lifecycle_events_never_create_cost(tenant_id, kind):
    traces.ingest(tenant_id, [ev(kind)])
    assert inference_total(tenant_id) == (0, 0)
    assert trace_row(tenant_id)[1] == 0


@pytest.mark.parametrize("span_kind", ["tool", "retrieval", "guardrail", "evaluation", "workflow"])
def test_non_llm_spans_never_create_cost(tenant_id, span_kind):
    traces.ingest(
        tenant_id,
        [
            ev(
                "span.completed",
                span_id="s1",
                span_kind=span_kind,
                provider="anthropic",
                model="claude-sonnet-4-6",
                tokens_in=999,
                tokens_out=999,
            )
        ],
    )
    assert inference_total(tenant_id) == (0, 0)


def test_a_cancelled_span_adds_no_artificial_cost(tenant_id):
    traces.ingest(
        tenant_id,
        [ev("span.cancelled", span_id="s1", provider="anthropic", model="claude-sonnet-4-6")],
    )
    assert inference_total(tenant_id) == (0, 0)


def test_a_failed_llm_call_still_costs_what_it_burned(tenant_id):
    # The tokens were really spent. Not counting them would understate the bill
    # and hide exactly the waste this product exists to find.
    traces.ingest(
        tenant_id,
        [
            ev(
                "span.failed",
                span_id="s1",
                provider="anthropic",
                model="claude-sonnet-4-6",
                tokens_in=1000,
                tokens_out=10,
            )
        ],
    )
    assert inference_total(tenant_id)[0] > 0
    assert rows(tenant_id, "ai_span", "status")[0][0] == "error"


def test_an_unpriced_provider_is_recorded_without_inventing_a_price(tenant_id):
    traces.ingest(tenant_id, [llm("s1", provider="some-new-provider")])
    assert inference_total(tenant_id) == (0, 0)
    assert rows(tenant_id, "ai_span", "amount")[0][0] == 0


# --- privacy ---------------------------------------------------------------
@pytest.mark.parametrize(
    "field",
    [
        "prompt",
        "messages",
        "response",
        "completion",
        "tool_args",
        "tool_result",
        "documents",
        "error_message",
        "stack_trace",
        "metadata",
        "content",
    ],
)
def test_content_bearing_fields_are_refused_loudly(tenant_id, field):
    # Silently dropping would leave a customer believing capture worked.
    with pytest.raises(traces.TraceError, match="does not accept prompt or response content"):
        traces.ingest(tenant_id, [llm("s1", **{field: "anything at all"})])


def test_nothing_resembling_content_reaches_the_database(tenant_id):
    traces.ingest(
        tenant_id, [llm("s1", prompt_id="classify-v2", prompt_version="2.1", prompt_hash="abc123")]
    )
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        columns = {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name IN ('ai_trace','ai_span')"
            )
        }
    # There is no column for it, which is stronger than not writing to one.
    for banned in (
        "prompt",
        "prompt_text",
        "response",
        "messages",
        "content",
        "metadata",
        "tool_args",
        "error_message",
        "stack_trace",
    ):
        assert banned not in columns


def test_a_prompt_hash_is_salted_so_it_cannot_be_dictionary_attacked(tenant_id):
    traces.ingest(tenant_id, [llm("s1", prompt_hash="client-side-hash")])
    stored = rows(tenant_id, "ai_span", "prompt_hash")[0][0]

    assert stored != "client-side-hash"
    assert len(stored) == 64
    # Equal prompts still compare equal within the tenant, which is the only
    # comparison the feature needs.
    traces.ingest(tenant_id, [llm("s2", prompt_hash="client-side-hash")])
    assert len({r[0] for r in rows(tenant_id, "ai_span", "prompt_hash")}) == 1


# --- validation ------------------------------------------------------------
def test_a_batch_larger_than_the_limit_is_refused(tenant_id):
    with pytest.raises(traces.TraceError, match="at most"):
        traces.ingest(tenant_id, [llm(f"s{i}") for i in range(traces.MAX_EVENTS_PER_BATCH + 1)])


@pytest.mark.parametrize(
    "bad, message",
    [
        ({"event_type": "nonsense"}, "event_type must be"),
        ({"event_type": "span.completed", "trace_id": None}, "trace_id is required"),
        ({"event_type": "span.completed", "trace_id": "t", "span_id": None}, "span_id is required"),
        (
            {
                "event_type": "span.completed",
                "trace_id": "t",
                "span_id": "s",
                "span_kind": "database",
            },
            "span_kind must be",
        ),
        (
            {"event_type": "span.completed", "trace_id": "t", "span_id": "s", "tokens_in": -5},
            "cannot be negative",
        ),
        (
            {"event_type": "span.completed", "trace_id": "t", "span_id": "s", "tokens_in": 10**12},
            "implausibly large",
        ),
    ],
)
def test_malformed_events_are_refused_with_a_reason(tenant_id, bad, message):
    with pytest.raises(traces.TraceError, match=message):
        traces.ingest(tenant_id, [bad])


def test_a_wrong_client_clock_cannot_write_cost_into_a_distant_month(tenant_id):
    traces.ingest(tenant_id, [llm("s1", occurred_at="2019-01-01T00:00:00Z")])
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        period = conn.execute("SELECT period FROM inference_cost").fetchone()[0]
    assert period.year >= NOW.year - 1


def test_an_unknown_feature_becomes_unattributed_rather_than_a_rejected_event(tenant_id):
    result = traces.ingest(tenant_id, [llm("s1", feature_id=str(uuid.uuid4()))])
    assert result["accepted"] == 1
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        assert conn.execute("SELECT feature_id FROM ai_trace").fetchone()[0] is None
    # The money is still counted; it lands in Unattributed.
    assert inference_total(tenant_id)[0] > 0


# --- applications ----------------------------------------------------------
def test_an_application_is_created_once_on_first_event(tenant_id):
    traces.ingest(tenant_id, [llm("s1"), llm("s2", trace="t2")])
    apps = rows(tenant_id, "ai_application", "slug, name")
    assert apps == [("support-agent", "support-agent")]


def test_application_slugs_are_normalized_before_resolution(tenant_id):
    traces.ingest(tenant_id, [llm("s1", application="Support Agent")])
    traces.ingest(tenant_id, [llm("s2", trace="t2", application="support-agent")])
    assert len(rows(tenant_id, "ai_application")) == 1


def test_a_tenant_cannot_create_unlimited_applications(tenant_id, monkeypatch):
    monkeypatch.setattr(applications, "MAX_APPLICATIONS_PER_TENANT", 3)
    for i in range(3):
        traces.ingest(tenant_id, [llm(f"s{i}", trace=f"t{i}", application=f"app-{i}")])
    with pytest.raises(applications.ApplicationError, match="already has 3"):
        traces.ingest(tenant_id, [llm("sx", trace="tx", application="app-overflow")])


# --- tenant isolation ------------------------------------------------------
def test_one_tenant_never_sees_another_s_traces(tenant_id, app_env):
    other = str(
        app_env.execute("INSERT INTO tenant (name) VALUES ('Other') RETURNING id").fetchone()[0]
    )
    app_env.commit()
    traces.ingest(tenant_id, [llm("s1")])
    traces.ingest(other, [llm("s1", trace="t-other")])

    assert [r[0] for r in rows(tenant_id, "ai_trace", "external_trace_id")] == ["t1"]
    assert [r[0] for r in rows(other, "ai_trace", "external_trace_id")] == ["t-other"]
    assert len(rows(tenant_id, "ai_application")) == 1
    assert len(rows(other, "ai_application")) == 1


def test_the_same_external_trace_id_in_two_tenants_stays_separate(tenant_id, app_env):
    other = str(
        app_env.execute("INSERT INTO tenant (name) VALUES ('Other') RETURNING id").fetchone()[0]
    )
    app_env.commit()
    traces.ingest(tenant_id, [llm("s1", tin=1000, tout=500)])
    traces.ingest(other, [llm("s1", tin=9000, tout=9000)])

    assert rows(tenant_id, "ai_span", "tokens_in")[0][0] == 1000
    assert rows(other, "ai_span", "tokens_in")[0][0] == 9000


# --- customer attribution --------------------------------------------------
def test_customer_attribution_flows_from_the_trace(tenant_id):
    traces.ingest(tenant_id, [llm("s1", customer_id="customer-123")])
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        row = conn.execute("SELECT customer_id, amount FROM customer_cost").fetchone()
    assert row[0] == "customer-123"
    assert row[1] > 0


def test_replaying_a_customer_attributed_span_does_not_double_their_cost(tenant_id):
    traces.ingest(tenant_id, [llm("s1", customer_id="customer-123")])
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        first = conn.execute("SELECT amount FROM customer_cost").fetchone()[0]
    traces.ingest(tenant_id, [llm("s1", customer_id="customer-123")])
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        assert conn.execute("SELECT amount FROM customer_cost").fetchone()[0] == first


# --- token detail ----------------------------------------------------------
def test_cache_and_reasoning_tokens_are_recorded(tenant_id):
    traces.ingest(
        tenant_id, [llm("s1", cache_read_tokens=800, cache_write_tokens=200, reasoning_tokens=64)]
    )
    row = rows(tenant_id, "ai_span", "cache_read_tokens, cache_write_tokens, reasoning_tokens")[0]
    assert row == (800, 200, 64)


def test_heartbeats_move_activity_without_touching_money(tenant_id):
    traces.ingest(tenant_id, [ev("trace.started"), llm("s1")])
    before = trace_row(tenant_id)[1]
    traces.ingest(tenant_id, [ev("trace.heartbeat")])
    status, cost, _, _, _, _, heartbeat, _ = trace_row(tenant_id)
    assert (status, cost) == ("running", before)
    assert heartbeat is not None
