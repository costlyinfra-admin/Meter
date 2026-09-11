"""What the assistant is allowed to know about a tenant's own data.

The handbook explains how Meter works. It cannot answer "is an agent stuck in a
loop" or "when was my data last refreshed", because those are facts about this
customer's data at this moment. This module assembles those facts — bounded,
read-only, and tenant-scoped through the same RLS every other read uses.

Three rules shape it.

**Small.** Everything here is pasted into a prompt, so it is summaries and top-N
lists, never rows. A snapshot is a few hundred tokens whatever the tenant's size,
which is what keeps the assistant affordable and its answers on topic.

**Selected by the question.** A question about freshness should not carry a
month of spend. Groups are chosen by keyword, and freshness is always included
because almost every answer is wrong if the data is stale and nobody says so.

**Nothing that is not already on a screen.** No prompt or response text, no tool
arguments, no customer identifiers, no credentials, no tenant ids. The privacy
guarantee this product makes does not get an exception for the chatbot, and an
LLM prompt is the last place to start making one.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from .db import app_dsn, connect, tenant_tx

#: Rows per list. Enough to answer "which ones", small enough to stay a summary.
TOP_N = 5


def _iso(value) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _groups_for(question: str) -> set[str]:
    """Which fact groups a question needs.

    Deliberately generous: including a group that turns out to be irrelevant
    costs a few tokens, while omitting one the question needed produces a
    confident "I don't have that" about data sitting right there.
    """
    q = (question or "").lower()
    groups = {"freshness"}  # always: a stale answer that does not say so is wrong

    def any_of(*words: str) -> bool:
        return any(w in q for w in words)

    if any_of("agent", "loop", "stuck", "stale", "running", "hung", "trace", "run", "workflow"):
        groups.add("agents")
    if any_of("trace", "token", "expensive", "slow", "step", "span", "run", "workflow", "model"):
        groups.add("traces")
    if any_of(
        "cost", "spend", "spike", "bill", "budget", "expensive", "cheap", "increase", "month",
        "feature", "unattributed", "provider", "model",
    ):
        groups.add("spend")
    if any_of("refresh", "sync", "update", "stale", "connect", "connector", "provider", "source"):
        groups.add("connectors")
    if any_of("alert", "triggered", "threshold", "notify"):
        groups.add("alerts")
    return groups


def snapshot(tenant_id: str, question: str, *, now: Optional[dt.datetime] = None) -> dict:
    """A compact picture of this tenant's data, chosen for the question asked."""
    now = now or dt.datetime.now(dt.timezone.utc)
    groups = _groups_for(question)
    out: dict = {"as_of": now.isoformat()}

    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        if "freshness" in groups:
            out["freshness"] = _freshness(conn, now)
        if "connectors" in groups:
            out["connectors"] = _connectors(conn)
        if "agents" in groups:
            out["agents"] = _agents(conn, now)
        if "traces" in groups:
            out["traces"] = _traces(conn, now)
        if "spend" in groups:
            out["spend"] = _spend(conn, now)
        if "alerts" in groups:
            out["alerts"] = _alerts(conn)
    return out


def _freshness(conn, now: dt.datetime) -> dict:
    """When each kind of data last arrived. The grounding for every other answer."""
    last_trace = conn.execute("SELECT MAX(started_at) FROM ai_trace").fetchone()[0]
    last_cost = conn.execute(
        "SELECT MAX(updated_at) FROM inference_cost WHERE source = 'cost_api'"
    ).fetchone()[0]
    last_infra = conn.execute(
        "SELECT MAX(finished_at) FROM infra_sync_run WHERE status = 'success'"
    ).fetchone()[0]
    last_discovery = conn.execute(
        "SELECT MAX(finished_at) FROM discovery_run WHERE status = 'success'"
    ).fetchone()[0]

    def ago(value) -> Optional[str]:
        if value is None:
            return None
        hours = (now - value).total_seconds() / 3600
        if hours < 1:
            return "under an hour ago"
        if hours < 48:
            return f"{int(hours)} hours ago"
        return f"{int(hours / 24)} days ago"

    return {
        "inference_cost_last_synced": _iso(last_cost),
        "inference_cost_age": ago(last_cost),
        "infrastructure_last_synced": _iso(last_infra),
        "infrastructure_age": ago(last_infra),
        "feature_discovery_last_run": _iso(last_discovery),
        "feature_discovery_age": ago(last_discovery),
        "last_trace_received": _iso(last_trace),
        "last_trace_age": ago(last_trace),
    }


def _connectors(conn) -> dict:
    rows = conn.execute(
        "SELECT connector_type, count(*) FROM connector_credential GROUP BY connector_type"
    ).fetchall()
    return {
        "connected": sorted(r[0] for r in rows),
        "accounts_per_connector": {r[0]: int(r[1]) for r in rows},
    }


def _agents(conn, now: dt.datetime) -> dict:
    """Live agent health: what is running, what has gone quiet, what is looping."""
    minutes = conn.execute("SELECT agent_stale_after_minutes FROM tenant LIMIT 1").fetchone()
    threshold = int(minutes[0]) if minutes else 10
    cutoff = now - dt.timedelta(minutes=threshold)

    running, stale = conn.execute(
        "SELECT count(*) FILTER (WHERE last_activity_at > %s), "
        "       count(*) FILTER (WHERE last_activity_at <= %s) "
        "FROM ai_trace WHERE status = 'running'",
        (cutoff, cutoff),
    ).fetchone()

    # A step repeated many times inside one run is what a loop looks like from
    # the outside. Named per workflow so an answer can point at one.
    loops = conn.execute(
        """
        SELECT t.operation_name, s.operation_name, COUNT(*) AS repeats, t.external_trace_id
        FROM ai_span s JOIN ai_trace t ON t.id = s.trace_id
        WHERE t.started_at > %s
        GROUP BY t.id, t.operation_name, s.operation_name, t.external_trace_id
        HAVING COUNT(*) >= 3
        ORDER BY repeats DESC
        LIMIT %s
        """,
        (now - dt.timedelta(days=7), TOP_N),
    ).fetchall()

    stuck = conn.execute(
        """
        SELECT operation_name, external_trace_id, started_at, span_count
        FROM ai_trace
        WHERE status = 'running' AND last_activity_at <= %s
        ORDER BY started_at
        LIMIT %s
        """,
        (cutoff, TOP_N),
    ).fetchall()

    return {
        "stale_after_minutes": threshold,
        "running_now": int(running or 0),
        "stale_now": int(stale or 0),
        "quiet_runs": [
            {
                "workflow": name,
                "trace_id": ext,
                "running_for_minutes": int((now - started).total_seconds() / 60),
                "steps": int(steps or 0),
            }
            for name, ext, started, steps in stuck
        ],
        "repeated_steps_last_7d": [
            {"workflow": wf, "step": step, "repeats": int(n), "trace_id": ext}
            for wf, step, n, ext in loops
        ],
        "note": (
            "A repeated step is not proof of a loop: a workflow may call the same step "
            "by design. Compare the repeat count against what that workflow normally does."
        ),
    }


def _traces(conn, now: dt.datetime) -> dict:
    since = now - dt.timedelta(days=7)
    rows = conn.execute(
        """
        SELECT operation_name, external_trace_id, total_tokens, total_cost, span_count, status
        FROM ai_trace
        WHERE started_at > %s
        ORDER BY total_tokens DESC
        LIMIT %s
        """,
        (since, TOP_N),
    ).fetchall()
    totals = conn.execute(
        "SELECT count(*), COALESCE(SUM(total_cost), 0), "
        "count(*) FILTER (WHERE status = 'error') FROM ai_trace WHERE started_at > %s",
        (since,),
    ).fetchone()
    return {
        "window": "last 7 days",
        "runs": int(totals[0] or 0),
        "spend": float(totals[1] or 0),
        "failed_runs": int(totals[2] or 0),
        "largest_by_tokens": [
            {
                "workflow": name,
                "trace_id": ext,
                "tokens": int(tokens or 0),
                "cost": float(cost or 0),
                "steps": int(steps or 0),
                "status": status,
            }
            for name, ext, tokens, cost, steps, status in rows
        ],
    }


def _spend(conn, now: dt.datetime) -> dict:
    """This month against last, and what moved. The shape of a "spike" question."""
    this_month = dt.date(now.year, now.month, 1)
    last_month = (this_month - dt.timedelta(days=1)).replace(day=1)

    def month_total(table: str, period: dt.date) -> float:
        row = conn.execute(
            f"SELECT COALESCE(SUM(amount), 0) FROM {table} WHERE period = %s",  # noqa: S608
            (period,),
        ).fetchone()
        return float(row[0] or 0)

    movers = conn.execute(
        """
        SELECT COALESCE(f.name, 'Unattributed'),
               COALESCE(SUM(c.amount) FILTER (WHERE c.period = %s), 0) AS now_amount,
               COALESCE(SUM(c.amount) FILTER (WHERE c.period = %s), 0) AS prev_amount
        FROM inference_cost c LEFT JOIN feature f ON f.id = c.feature_id
        WHERE c.period IN (%s, %s)
        GROUP BY COALESCE(f.name, 'Unattributed')
        ORDER BY (COALESCE(SUM(c.amount) FILTER (WHERE c.period = %s), 0)
                  - COALESCE(SUM(c.amount) FILTER (WHERE c.period = %s), 0)) DESC
        LIMIT %s
        """,
        (this_month, last_month, this_month, last_month, this_month, last_month, TOP_N),
    ).fetchall()

    daily = conn.execute(
        """
        SELECT day, COALESCE(SUM(amount), 0)
        FROM inference_cost_daily
        WHERE day > %s
        GROUP BY day ORDER BY day DESC LIMIT 14
        """,
        (now.date() - dt.timedelta(days=14),),
    ).fetchall()

    return {
        "inference_this_month": month_total("inference_cost", this_month),
        "inference_last_month": month_total("inference_cost", last_month),
        "build_this_month": month_total("build_cost", this_month),
        "build_last_month": month_total("build_cost", last_month),
        "note": "Build cost and inference cost are never added together.",
        "biggest_movers_by_feature": [
            {"feature": name, "this_month": float(a or 0), "last_month": float(b or 0)}
            for name, a, b in movers
        ],
        "inference_by_day_recent": [
            {"day": d.isoformat(), "amount": float(a or 0)} for d, a in daily
        ],
    }


def _alerts(conn) -> dict:
    rows = conn.execute(
        "SELECT name, metric, status, last_observed, threshold FROM alert_rule "
        "WHERE enabled ORDER BY (status = 'triggered') DESC, name LIMIT %s",
        (TOP_N,),
    ).fetchall()
    return {
        "rules": [
            {
                "name": name,
                "metric": metric,
                "status": status,
                "observed": float(observed) if observed is not None else None,
                "threshold": float(threshold) if threshold is not None else None,
            }
            for name, metric, status, observed, threshold in rows
        ]
    }


def summarize(facts: Optional[dict]) -> Optional[str]:
    """The snapshot as plain sentences, for when no answering model is reachable.

    Not a substitute for the model — it cannot follow a conversation or explain
    a mechanism. But the questions worth asking a dashboard are largely "what is
    happening right now", and those have factual answers that need no prose
    generation. Returning them beats returning documentation.

    None when the snapshot holds nothing worth saying, so the caller can say so
    rather than emit an empty shell of a reply.
    """
    if not facts:
        return None

    lines: list[str] = []
    agents = facts.get("agents") or {}
    if agents:
        stale, running = agents.get("stale_now", 0), agents.get("running_now", 0)
        if stale:
            quiet = agents.get("quiet_runs") or []
            named = ", ".join(
                f"**{r['workflow']}** ({r['running_for_minutes']} min)" for r in quiet[:3]
            )
            lines.append(
                f"{stale} run{'s' if stale != 1 else ''} "
                f"{'have' if stale != 1 else 'has'} gone quiet for longer than "
                f"{agents.get('stale_after_minutes')} minutes"
                f"{': ' + named if named else ''}. A quiet run has not failed and may "
                f"still finish — see [Traces](/traces)."
            )
        elif running:
            lines.append(f"{running} run{'s' if running != 1 else ''} in flight, none gone quiet.")
        else:
            lines.append("No agent runs are active right now.")

        loops = agents.get("repeated_steps_last_7d") or []
        if loops:
            top = loops[0]
            lines.append(
                f"The most repeated step in the last 7 days is **{top['step']}** in "
                f"**{top['workflow']}**, {top['repeats']} times in one run. That is worth "
                f"a look, though some workflows call a step more than once by design."
            )

    traces = facts.get("traces") or {}
    if traces.get("runs"):
        biggest = (traces.get("largest_by_tokens") or [{}])[0]
        if biggest:
            lines.append(
                f"Over the last 7 days: {traces['runs']} runs, "
                f"{traces['failed_runs']} failed. The heaviest was **{biggest['workflow']}** "
                f"at {biggest['tokens']:,} tokens."
            )

    fresh = facts.get("freshness") or {}
    ages = [
        ("Inference cost", fresh.get("inference_cost_age")),
        ("Infrastructure", fresh.get("infrastructure_age")),
        ("Feature discovery", fresh.get("feature_discovery_age")),
        ("Traces", fresh.get("last_trace_age")),
    ]
    known = [f"{label} {age}" for label, age in ages if age]
    if known and not lines:
        # Freshness is in every snapshot, so it only becomes the answer when
        # nothing more specific applied — otherwise every reply would end with it.
        lines.append("Last updated — " + "; ".join(known) + ".")

    return "\n\n".join(lines) if lines else None
