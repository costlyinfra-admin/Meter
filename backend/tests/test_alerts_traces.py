"""Alerts driven by real traces: stale runs, runtime, steps, loops, cost, cache.

Every test here builds its evidence by ingesting events through
``traces.ingest`` — the same path the SDK uses — rather than writing rows
directly. An alert that fires on hand-written rows proves only that the SQL
runs; these prove the alert fires on what an agent actually reports.

Time is controlled by evaluating at a chosen ``now`` rather than by faking the
clock: ingestion always stamps ``last_activity_at`` with the server's own time,
which is exactly the behaviour that makes staleness trustworthy.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from meter import alerts, alerts_eval, notify, traces
from meter.db import app_dsn, connect, tenant_tx

NOW = dt.datetime.now(dt.timezone.utc)
HOUR = dt.timedelta(hours=1)
MINUTE = dt.timedelta(minutes=1)


# ---- building evidence ----------------------------------------------------
def ev(kind, trace, *, at=None, app="support-agent", **over):
    base = {
        "event_type": kind,
        "trace_id": trace,
        "application": app,
        "operation_name": "resolve-ticket",
        "environment": "production",
        "occurred_at": (at or NOW).isoformat(),
    }
    base.update(over)
    return base


def span(trace, span_id, *, at=None, app="support-agent", op="classify", **over):
    fields = {
        "span_id": span_id,
        "span_kind": "llm",
        "operation_name": op,
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "tokens_in": 1000,
        "tokens_out": 500,
    }
    fields.update(over)
    return ev("span.completed", trace, at=at, app=app, **fields)


def run(tenant_id, trace, *, at=None, spans=1, outcome="trace.completed", app="support-agent"):
    """One whole run: started, its steps, and how it ended."""
    at = at or NOW
    events = [ev("trace.started", trace, at=at, app=app)]
    events += [span(trace, f"s{i}", at=at, app=app) for i in range(spans)]
    if outcome:
        events.append(ev(outcome, trace, at=at, app=app))
    traces.ingest(tenant_id, events)


def rule_for(tenant_id, **over):
    base = {
        "name": "Trace alert",
        "metric": "stale_agents",
        "scope_type": "organization",
        "condition_type": "exceeds",
        "threshold": 0,
        "window": "hourly",
        "cooldown": "none",
        "channels": [{"channel": "in_app"}],
    }
    base.update(over)
    return alerts.create_rule(tenant_id, base)


def status_at(tenant_id, rule, when):
    alerts_eval.evaluate_rule(tenant_id, rule["id"], now=when)
    return alerts.get_rule(tenant_id, rule["id"])["status"]


def app_ids(tenant_id):
    """slug -> id, resolved the same way ingestion resolves an application."""
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        return {
            slug: str(aid)
            for aid, slug in conn.execute("SELECT id, slug FROM ai_application").fetchall()
        }


def trace_costs(tenant_id):
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        return conn.execute(
            "SELECT COALESCE(SUM(total_cost), 0), COUNT(*) FROM ai_trace WHERE status <> 'running'"
        ).fetchone()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(notify, "_sleep", lambda *_: None)
    monkeypatch.setattr(
        notify, "_http_post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no net"))
    )


# ---- an agent that goes quiet ---------------------------------------------
def test_a_run_that_goes_quiet_trips_the_stale_alert(tenant_id):
    traces.ingest(tenant_id, [ev("trace.started", "t-stuck")])
    rule = rule_for(tenant_id, metric="stale_agents", threshold=0)

    # It has only just started, so it is alive, not stale — and "0 stale runs"
    # is a real, healthy reading, not an absence of data.
    assert status_at(tenant_id, rule, NOW) == "healthy"

    # Two hours later nothing has moved and the default threshold is 10 minutes.
    assert status_at(tenant_id, rule, NOW + 2 * HOUR) == "triggered"


def test_a_finished_run_is_never_stale(tenant_id):
    run(tenant_id, "t-done")
    rule = rule_for(tenant_id, metric="stale_agents", threshold=0)
    # A run that ended is not "quiet"; it is over.
    assert status_at(tenant_id, rule, NOW + 2 * HOUR) == "healthy"


def test_a_late_completion_resolves_the_stale_alert(tenant_id):
    """Staleness is derived, so finishing late un-triggers without a fixup job."""
    traces.ingest(tenant_id, [ev("trace.started", "t-slow")])
    rule = rule_for(tenant_id, metric="stale_agents", threshold=0)
    assert status_at(tenant_id, rule, NOW + 2 * HOUR) == "triggered"

    traces.ingest(tenant_id, [ev("trace.completed", "t-slow")])
    assert status_at(tenant_id, rule, NOW + 2 * HOUR) == "healthy"


# ---- runtime, steps and loops ---------------------------------------------
def test_a_long_running_run_trips_the_runtime_alert(tenant_id):
    traces.ingest(tenant_id, [ev("trace.started", "t-long", at=NOW - 45 * MINUTE)])
    rule = rule_for(tenant_id, metric="agent_runtime", threshold=30, window="daily")
    # 45 minutes and still going > 30. A run in flight counts at its runtime so
    # far, which is the only way an unfinished run can ever be caught.
    assert status_at(tenant_id, rule, NOW) == "triggered"


def test_a_quick_run_leaves_the_runtime_alert_healthy(tenant_id):
    run(tenant_id, "t-quick", at=NOW - 2 * MINUTE)
    rule = rule_for(tenant_id, metric="agent_runtime", threshold=30, window="daily")
    assert status_at(tenant_id, rule, NOW) == "healthy"


def test_too_many_steps_in_one_run_trips_the_step_alert(tenant_id):
    run(tenant_id, "t-wide", spans=7)
    run(tenant_id, "t-narrow", spans=2)
    rule = rule_for(tenant_id, metric="agent_steps", threshold=5, window="daily")
    # The measure is the worst SINGLE run, not the average: one 7-step run must
    # not be hidden by a quiet one beside it.
    assert status_at(tenant_id, rule, NOW) == "triggered"


def test_a_repeated_step_trips_the_retry_loop_alert(tenant_id):
    traces.ingest(
        tenant_id,
        [ev("trace.started", "t-loop")]
        + [span("t-loop", f"s{i}", op="call-search") for i in range(4)]
        + [ev("trace.completed", "t-loop")],
    )
    rule = rule_for(tenant_id, metric="retry_loop", threshold=3, window="daily")
    assert status_at(tenant_id, rule, NOW) == "triggered"


def test_distinct_steps_are_not_a_retry_loop(tenant_id):
    traces.ingest(
        tenant_id,
        [ev("trace.started", "t-plan")]
        + [span("t-plan", f"s{i}", op=name) for i, name in enumerate("abcdef")]
        + [ev("trace.completed", "t-plan")],
    )
    rule = rule_for(tenant_id, metric="retry_loop", threshold=3, window="daily")
    # Six steps, each done once. A long workflow is not a loop.
    assert status_at(tenant_id, rule, NOW) == "healthy"


# ---- money ----------------------------------------------------------------
def test_cost_per_run_uses_finished_runs_only(tenant_id):
    run(tenant_id, "t-a")
    run(tenant_id, "t-b")
    total, n = trace_costs(tenant_id)
    assert n == 2 and total > 0
    average = Decimal(str(total)) / Decimal(n)

    # A third run is still in flight. Its partial cost must not drag the average
    # down and quietly clear the alert.
    traces.ingest(tenant_id, [ev("trace.started", "t-open")])

    over = rule_for(tenant_id, metric="cost_per_run", threshold=float(average) / 2, window="daily")
    assert status_at(tenant_id, over, NOW) == "triggered"

    under = rule_for(
        tenant_id,
        name="Under",
        metric="cost_per_run",
        threshold=float(average) * 2,
        window="daily",
    )
    assert status_at(tenant_id, under, NOW) == "healthy"


def test_cost_per_run_compares_against_the_previous_window(tenant_id):
    # One cheap run in the previous hour, one much dearer run in this one.
    traces.ingest(
        tenant_id,
        [
            ev("trace.started", "t-old", at=NOW - 90 * MINUTE),
            span("t-old", "s0", at=NOW - 90 * MINUTE, tokens_in=1000, tokens_out=100),
            ev("trace.completed", "t-old", at=NOW - 90 * MINUTE),
        ],
    )
    traces.ingest(
        tenant_id,
        [
            ev("trace.started", "t-new", at=NOW - 10 * MINUTE),
            span("t-new", "s0", at=NOW - 10 * MINUTE, tokens_in=100_000, tokens_out=10_000),
            ev("trace.completed", "t-new", at=NOW - 10 * MINUTE),
        ],
    )
    rule = rule_for(tenant_id, metric="cost_per_run", condition_type="increase_pct", threshold=50)
    assert status_at(tenant_id, rule, NOW) == "triggered"


def test_cost_spent_on_failed_runs(tenant_id):
    run(tenant_id, "t-ok")
    rule = rule_for(tenant_id, metric="failed_run_cost", threshold=0, window="daily")
    # Nothing has failed. Zero wasted dollars is a reading, and it is healthy.
    assert status_at(tenant_id, rule, NOW) == "healthy"

    run(tenant_id, "t-bad", outcome="trace.failed")
    assert status_at(tenant_id, rule, NOW) == "triggered"


# ---- cache efficiency -----------------------------------------------------
def test_a_falling_cache_hit_rate_trips_the_alert(tenant_id):
    traces.ingest(
        tenant_id,
        [
            ev("trace.started", "t-cold"),
            span("t-cold", "s0", tokens_in=900, cache_read_tokens=100),
            ev("trace.completed", "t-cold"),
        ],
    )
    rule = rule_for(
        tenant_id,
        metric="cache_hit_rate",
        condition_type="falls_below",
        threshold=40,
        window="daily",
    )
    # 100 of 1,000 input tokens came from cache — 10%, well under 40%.
    assert status_at(tenant_id, rule, NOW) == "triggered"


def test_a_healthy_cache_hit_rate_does_not_trip(tenant_id):
    traces.ingest(
        tenant_id,
        [
            ev("trace.started", "t-warm"),
            span("t-warm", "s0", tokens_in=100, cache_read_tokens=900),
            ev("trace.completed", "t-warm"),
        ],
    )
    rule = rule_for(
        tenant_id,
        metric="cache_hit_rate",
        condition_type="falls_below",
        threshold=40,
        window="daily",
    )
    assert status_at(tenant_id, rule, NOW) == "healthy"


# ---- scope ----------------------------------------------------------------
def test_an_application_scoped_alert_ignores_other_applications(tenant_id):
    run(tenant_id, "t-billing", spans=7, app="billing-agent")
    run(tenant_id, "t-support", spans=1, app="support-agent")

    apps = app_ids(tenant_id)
    quiet = rule_for(
        tenant_id,
        metric="agent_steps",
        scope_type="application",
        scope_ref=apps["support-agent"],
        threshold=5,
        window="daily",
    )
    noisy = rule_for(
        tenant_id,
        name="Billing steps",
        metric="agent_steps",
        scope_type="application",
        scope_ref=apps["billing-agent"],
        threshold=5,
        window="daily",
    )
    assert status_at(tenant_id, quiet, NOW) == "healthy"
    assert status_at(tenant_id, noisy, NOW) == "triggered"


def test_an_application_scope_accepts_the_slug_the_sdk_uses(tenant_id):
    run(tenant_id, "t-1", app="support-agent")
    apps = app_ids(tenant_id)
    rule = rule_for(
        tenant_id,
        metric="agent_steps",
        scope_type="application",
        scope_ref="support-agent",
        threshold=5,
        window="daily",
    )
    # Stored as the id, so renaming the application never re-points the alert.
    assert rule["scope_ref"] == apps["support-agent"]
    assert rule["scope_label"] == "support-agent"


# ---- no data is not zero --------------------------------------------------
@pytest.mark.parametrize(
    "metric,condition,threshold",
    [
        ("stale_agents", "exceeds", 0),
        ("agent_runtime", "exceeds", 30),
        ("agent_steps", "exceeds", 5),
        ("cost_per_run", "exceeds", 1),
        ("retry_loop", "exceeds", 3),
        ("failed_run_cost", "exceeds", 0),
        ("cache_hit_rate", "falls_below", 40),
    ],
)
def test_no_traces_is_insufficient_data_not_healthy(tenant_id, metric, condition, threshold):
    rule = rule_for(
        tenant_id, metric=metric, condition_type=condition, threshold=threshold, window="daily"
    )
    assert status_at(tenant_id, rule, NOW) == "insufficient_data"


# ---- validation -----------------------------------------------------------
def test_provider_and_model_scopes_are_refused_for_trace_metrics(tenant_id):
    # A single run can call several providers, so it belongs to none of them.
    for scope in ("provider", "model"):
        with pytest.raises(alerts.AlertError):
            rule_for(tenant_id, metric="cost_per_run", scope_type=scope, scope_ref="anthropic")


def test_an_unknown_application_is_refused(tenant_id):
    with pytest.raises(alerts.AlertError):
        rule_for(
            tenant_id,
            metric="agent_steps",
            scope_type="application",
            scope_ref="no-such-application",
            threshold=5,
        )


def test_a_percentage_metric_rejects_a_threshold_above_100(tenant_id):
    with pytest.raises(alerts.AlertError):
        rule_for(tenant_id, metric="cache_hit_rate", condition_type="falls_below", threshold=150)


def test_only_cost_per_run_offers_a_percentage_increase(tenant_id):
    assert alerts.valid_conditions("cost_per_run") == ("exceeds", "increase_pct")
    assert alerts.valid_conditions("agent_runtime") == ("exceeds",)
    assert alerts.valid_conditions("cache_hit_rate") == ("falls_below",)
    with pytest.raises(alerts.AlertError):
        rule_for(tenant_id, metric="agent_runtime", condition_type="increase_pct", threshold=25)


def test_an_application_alert_deep_links_to_that_application():
    assert alerts_eval._deep_link_path("application", "abc-123") == "/applications/abc-123"


def test_messages_read_in_the_metrics_own_units(tenant_id):
    run(tenant_id, "t-wide", spans=7)
    rule = rule_for(tenant_id, metric="agent_steps", threshold=5, window="daily")
    alerts_eval.evaluate_rule(tenant_id, rule["id"], now=NOW)
    message = alerts.rule_events(tenant_id, rule["id"])["events"][0]["message"]
    # "observed 7 steps", not "observed $7.00".
    assert "7 steps" in message
    assert "$" not in message


def test_a_single_stuck_run_reads_as_one_run(tenant_id):
    traces.ingest(tenant_id, [ev("trace.started", "t-stuck")])
    rule = rule_for(tenant_id, metric="stale_agents", threshold=0)
    alerts_eval.evaluate_rule(tenant_id, rule["id"], now=NOW + 2 * HOUR)
    message = alerts.rule_events(tenant_id, rule["id"])["events"][0]["message"]
    assert "observed 1 run vs" in message  # "1 run", not "1 runs"
