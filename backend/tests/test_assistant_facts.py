"""What the assistant may know about a tenant's data — and what it must not."""

from __future__ import annotations

import datetime as dt
import json

from meter import assistant_facts, credentials, traces
from meter.db import app_dsn, connect, tenant_tx

NOW = dt.datetime.now(dt.timezone.utc)


def ev(kind, trace, *, at=None, op="resolve-ticket", **over):
    base = {
        "event_type": kind,
        "trace_id": trace,
        "application": "support-agent",
        "operation_name": op,
        "environment": "production",
        "occurred_at": (at or NOW).isoformat(),
    }
    base.update(over)
    return base


def span(trace, sid, *, op="classify", **over):
    fields = {
        "span_id": sid,
        "span_kind": "llm",
        "operation_name": op,
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "tokens_in": 1000,
        "tokens_out": 500,
    }
    fields.update(over)
    return ev("span.completed", trace, **fields)


def test_a_quiet_run_is_visible_as_a_stuck_agent(tenant_id):
    # "Is an agent stuck?" is exactly the question the handbook cannot answer.
    traces.ingest(tenant_id, [ev("trace.started", "t-stuck")])
    later = NOW + dt.timedelta(hours=2)

    facts = assistant_facts.snapshot(tenant_id, "is an agent stuck in a loop?", now=later)
    agents = facts["agents"]
    assert agents["stale_now"] == 1
    assert agents["running_now"] == 0
    assert agents["quiet_runs"][0]["workflow"] == "resolve-ticket"
    assert agents["quiet_runs"][0]["running_for_minutes"] >= 100


def test_a_repeated_step_is_reported_with_a_caveat(tenant_id):
    traces.ingest(
        tenant_id,
        [ev("trace.started", "t-loop")]
        + [span("t-loop", f"s{i}", op="call-search") for i in range(6)]
        + [ev("trace.completed", "t-loop")],
    )
    facts = assistant_facts.snapshot(tenant_id, "is an agent looping?")
    loops = facts["agents"]["repeated_steps_last_7d"]
    assert loops[0]["step"] == "call-search"
    assert loops[0]["repeats"] == 6
    # A repeat is evidence, not a verdict: some workflows call a step twice by
    # design, and the assistant must not assert a loop from the count alone.
    assert "not proof of a loop" in facts["agents"]["note"]


def test_freshness_is_always_included_whatever_was_asked(tenant_id):
    # Almost every answer is wrong if the data is stale and nobody says so.
    for question in ("how is build cost calculated?", "which traces used most tokens?", ""):
        assert "freshness" in assistant_facts.snapshot(tenant_id, question)


def test_freshness_says_how_old_the_data_is(tenant_id):
    traces.ingest(tenant_id, [ev("trace.started", "t-1", at=NOW - dt.timedelta(days=3))])
    facts = assistant_facts.snapshot(tenant_id, "when was my data last refreshed?")
    assert facts["freshness"]["last_trace_age"] == "3 days ago"


def test_the_largest_traces_are_named(tenant_id):
    traces.ingest(
        tenant_id,
        [
            ev("trace.started", "t-big"),
            span("t-big", "s1", tokens_in=900_000),
            ev("trace.completed", "t-big"),
        ],
    )
    traces.ingest(
        tenant_id,
        [
            ev("trace.started", "t-small"),
            span("t-small", "s1", tokens_in=10),
            ev("trace.completed", "t-small"),
        ],
    )
    biggest = assistant_facts.snapshot(tenant_id, "which traces used the most tokens?")["traces"]
    assert biggest["largest_by_tokens"][0]["trace_id"] == "t-big"
    assert biggest["runs"] == 2


def test_a_question_about_cost_carries_spend_but_not_agent_detail(tenant_id):
    # Selecting by question is what keeps a snapshot small enough to prompt with.
    facts = assistant_facts.snapshot(tenant_id, "what is my budget this month?")
    assert "spend" in facts
    assert "agents" not in facts


def test_the_snapshot_carries_no_prompt_content_or_credentials(tenant_id):
    # The privacy guarantee does not get an exception for the chatbot.
    credentials.save_credential(tenant_id, "anthropic", "sk-ant-SECRET-KEY")
    traces.ingest(
        tenant_id,
        [
            ev("trace.started", "t-1", customer_id="customer-42"),
            span("t-1", "s1", prompt_id="answer-ticket"),
            ev("trace.completed", "t-1"),
        ],
    )
    blob = json.dumps(
        assistant_facts.snapshot(tenant_id, "agents traces cost refresh alerts connectors"),
        default=str,
    )
    for forbidden in ("sk-ant-SECRET-KEY", "customer-42", str(tenant_id)):
        assert forbidden not in blob


def test_the_snapshot_stays_small_however_much_data_there_is(tenant_id):
    for i in range(40):
        traces.ingest(
            tenant_id,
            [
                ev("trace.started", f"t-{i}"),
                span(f"t-{i}", "s1"),
                ev("trace.completed", f"t-{i}"),
            ],
        )
    blob = json.dumps(
        assistant_facts.snapshot(tenant_id, "agents traces cost refresh alerts"), default=str
    )
    # Summaries and top-N lists, never rows: this is pasted into a prompt.
    assert len(blob) < 6000


def test_one_tenant_never_sees_another(tenant_id, app_env):
    # The snapshot runs under the same RLS as every other read.
    traces.ingest(tenant_id, [ev("trace.started", "t-mine", op="my-workflow")])
    other = app_env.execute(
        "INSERT INTO tenant (name) VALUES ('Other Co') RETURNING id"
    ).fetchone()[0]
    app_env.commit()
    with connect(app_dsn()) as conn, tenant_tx(conn, str(other)):
        conn.execute(
            "INSERT INTO ai_application (tenant_id, name, slug) VALUES (%s, 'Theirs', 'theirs')",
            (str(other),),
        )
    facts = assistant_facts.snapshot(str(other), "is an agent stuck?")
    assert facts["agents"]["quiet_runs"] == []
    assert facts["agents"]["stale_now"] == 0
