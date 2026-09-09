"""Alert evaluation — the scheduled state machine.

For each due, enabled rule this:
  1. computes the metric for the org / scope / current window (org timezone),
  2. compares it against the condition,
  3. transitions Healthy <-> Triggered (or Insufficient data when it can't be
     evaluated reliably), opening/closing exactly one incident per real change,
  4. records exactly one Triggered/Resolved event per transition (idempotent),
  5. dispatches notifications independently by channel (after commit),
  6. schedules the next evaluation.

Idempotency + concurrency safety: the rule row is locked FOR UPDATE for the whole
transition, so two workers can't both open an incident; a unique ``event_key`` and
the "one open incident per rule" partial index make duplicate events impossible
even if that lock were bypassed.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

from . import ai_reads, alerts, budgets, notify
from .db import admin_dsn, app_dsn, connect, tenant_tx
from .providers import month_start

logger = logging.getLogger("meter.alerts")

_ACTIVE = "(environment IS NULL OR environment <> 'ignore')"

_WINDOW_DELTA = {
    "hourly": dt.timedelta(hours=1),
    "daily": dt.timedelta(days=1),
    "weekly": dt.timedelta(weeks=1),
    "monthly": dt.timedelta(days=30),
}
_COOLDOWN_DELTA = {
    "none": dt.timedelta(0),
    "hour": dt.timedelta(hours=1),
    "day": dt.timedelta(days=1),
    "week": dt.timedelta(weeks=1),
}


def _org_timezone(tenant_id: str) -> ZoneInfo:
    with connect(admin_dsn()) as conn:
        row = conn.execute("SELECT timezone FROM tenant WHERE id = %s", (tenant_id,)).fetchone()
    try:
        return ZoneInfo(row[0]) if row and row[0] else ZoneInfo("UTC")
    except Exception:  # noqa: BLE001 — bad tz falls back to UTC
        return ZoneInfo("UTC")


def _current_month(tenant_id: str, now: dt.datetime) -> dt.date:
    """The first-of-month for `now` in the org's timezone (calendar boundary)."""
    return month_start(now.astimezone(_org_timezone(tenant_id)).date())


# ---- Metric computation (scope-aware, ignore-excluded) --------------------
def _scope_sql(rule: dict) -> tuple[str, list]:
    st, ref = rule["scope_type"], rule["scope_ref"]
    if st == "provider":
        return " AND provider = %s", [ref]
    if st == "model":
        return " AND model = %s", [ref]
    if st == "feature":
        return " AND feature_id = %s", [ref]
    return "", []


def _sum(conn, sql: str, params: list) -> tuple[Decimal, int]:
    row = conn.execute(sql, params).fetchone()
    return (Decimal(str(row[0])) if row[0] is not None else Decimal("0")), int(row[1])


# ---- Request-level metrics (measured over traces, not months) -------------
# These read ai_trace/ai_span over the rule's own window ending at `end`, which
# is what lets the same function produce the previous period's value: ask for
# the window ending one window earlier.
#
# A trace tagged environment 'ignore' is excluded, as ignored spend is
# everywhere else in reporting.
_TRACE_ACTIVE = "(t.environment IS NULL OR t.environment <> 'ignore')"


def _trace_scope_sql(rule: dict) -> tuple[str, list]:
    """Scope clause for a query whose ai_trace is aliased `t`.

    Only organization/application/feature reach here: alerts.valid_scopes()
    refuses provider and model for these metrics, because one run can call
    several providers and belongs wholly to none of them.
    """
    st, ref = rule["scope_type"], rule["scope_ref"]
    if st == "application":
        return " AND t.application_id = %s", [ref]
    if st == "feature":
        return " AND t.feature_id = %s", [ref]
    return "", []


def _trace_metric_value(conn, rule: dict, end: dt.datetime) -> Optional[Decimal]:
    """The request-level metric over the window ending at `end`, or None.

    None means "cannot be evaluated" and always maps to insufficient_data — it
    is never conflated with zero. Zero stale agents is a real, healthy reading;
    no traces at all is not a reading.
    """
    metric = rule["metric"]
    scope, sp = _trace_scope_sql(rule)
    start = end - _WINDOW_DELTA[rule["window"]]
    # Half-open the other way round: (start, end]. `end` is the moment of
    # evaluation, so a run that started this instant belongs to the window being
    # evaluated -- an exclusive upper bound would drop it, and with an hourly
    # window on a short workflow that is most of them. Excluding `start` keeps
    # this window and the previous one from both claiming the same instant.
    window = " AND t.started_at > %s AND t.started_at <= %s"
    wp = [start, end]

    if metric == "stale_agents":
        # Deliberately NOT windowed by started_at: a run that began three hours
        # ago and went quiet ten minutes ago is exactly the one to alert on, and
        # an hourly window would have already forgotten it. Staleness uses the
        # tenant's own threshold, so the alert agrees with what the Traces page
        # shows rather than inventing a second definition.
        cutoff = end - dt.timedelta(minutes=ai_reads.stale_threshold(conn))
        row = conn.execute(
            f"SELECT count(*) FILTER (WHERE t.status = 'running' "
            f"                        AND t.last_activity_at <= %s), count(*) "
            f"FROM ai_trace t WHERE {_TRACE_ACTIVE}{scope}",  # noqa: S608
            [cutoff, *sp],
        ).fetchone()
        return Decimal(int(row[0])) if int(row[1]) else None

    if metric == "agent_runtime":
        # A run still in flight counts at its runtime SO FAR — the whole point
        # is to catch the one that has not come back.
        row = conn.execute(
            f"SELECT MAX(COALESCE(t.duration_ms, "
            f"           EXTRACT(EPOCH FROM (%s - t.started_at)) * 1000)), count(*) "
            f"FROM ai_trace t WHERE {_TRACE_ACTIVE}{window}{scope}",  # noqa: S608
            [end, *wp, *sp],
        ).fetchone()
        if not int(row[1]) or row[0] is None:
            return None
        return Decimal(str(row[0])) / Decimal("60000")  # ms -> minutes

    if metric == "agent_steps":
        row = conn.execute(
            f"SELECT MAX(t.span_count), count(*) "
            f"FROM ai_trace t WHERE {_TRACE_ACTIVE}{window}{scope}",  # noqa: S608
            [*wp, *sp],
        ).fetchone()
        return Decimal(int(row[0])) if int(row[1]) and row[0] is not None else None

    if metric == "cost_per_run":
        # Finished runs only. A run that is still going has only part of its
        # cost recorded, and averaging it in makes every busy period look cheap.
        row = conn.execute(
            f"SELECT COALESCE(SUM(t.total_cost), 0), count(*) FROM ai_trace t "
            f"WHERE {_TRACE_ACTIVE} AND t.status <> 'running'{window}{scope}",  # noqa: S608
            [*wp, *sp],
        ).fetchone()
        n = int(row[1])
        return (Decimal(str(row[0])) / Decimal(n)) if n else None

    if metric == "retry_loop":
        # The most times any ONE step repeated inside a SINGLE run. A workflow
        # that calls "search" twice by design reads as 2 forever; one stuck in a
        # retry loop climbs, which is what a threshold above the design catches.
        row = conn.execute(
            f"""
            SELECT MAX(repeats), COUNT(*) FROM (
                SELECT COUNT(*) AS repeats
                FROM ai_span s JOIN ai_trace t ON t.id = s.trace_id
                WHERE {_TRACE_ACTIVE}{window}{scope}
                GROUP BY s.trace_id, s.span_kind, s.operation_name
            ) AS per_step
            """,  # noqa: S608
            [*wp, *sp],
        ).fetchone()
        return Decimal(int(row[0])) if int(row[1]) and row[0] is not None else None

    if metric == "failed_run_cost":
        # Money spent on runs that returned nothing. Zero is a real reading, so
        # the denominator is "were there any runs", not "were there any errors".
        row = conn.execute(
            f"SELECT COALESCE(SUM(t.total_cost) FILTER (WHERE t.status = 'error'), 0), "
            f"       count(*) "
            f"FROM ai_trace t WHERE {_TRACE_ACTIVE}{window}{scope}",  # noqa: S608
            [*wp, *sp],
        ).fetchone()
        return Decimal(str(row[0])) if int(row[1]) else None

    if metric == "cache_hit_rate":
        # Share of input tokens served from the prompt cache. Cache writes are
        # excluded from the denominator: a write is the price of a later hit,
        # not a missed one.
        row = conn.execute(
            f"SELECT COALESCE(SUM(s.cache_read_tokens), 0), "
            f"       COALESCE(SUM(s.cache_read_tokens), 0) + COALESCE(SUM(s.tokens_in), 0) "
            f"FROM ai_span s JOIN ai_trace t ON t.id = s.trace_id "
            f"WHERE s.span_kind = 'llm' AND {_TRACE_ACTIVE}{window}{scope}",  # noqa: S608
            [*wp, *sp],
        ).fetchone()
        denominator = int(row[1])
        if not denominator:
            return None  # nothing read any input tokens -> no rate to speak of
        return Decimal(int(row[0])) / Decimal(denominator) * Decimal("100")

    return None


def _current_value(conn, rule: dict, month: dt.date, now: dt.datetime) -> Optional[Decimal]:
    if rule["metric"] in alerts.TRACE_METRICS:
        return _trace_metric_value(conn, rule, now)
    return _metric_value(conn, rule, month)


def _prior_value(conn, rule: dict, month: dt.date, now: dt.datetime) -> Optional[Decimal]:
    """The same metric one period earlier — the denominator for increase_pct."""
    if rule["metric"] in alerts.TRACE_METRICS:
        return _trace_metric_value(conn, rule, now - _WINDOW_DELTA[rule["window"]])
    return _metric_value(conn, rule, month_start(_prev_month(month)))


def _metric_value(conn, rule: dict, month: dt.date) -> Optional[Decimal]:
    """The metric's value for `month`, or None when it can't be evaluated (no data)."""
    metric, st = rule["metric"], rule["scope_type"]
    scope_clause, scope_params = _scope_sql(rule)

    def infer(extra: str = "") -> tuple[Decimal, int]:
        return _sum(
            conn,
            f"SELECT COALESCE(SUM(amount),0), COUNT(*) FROM inference_cost "
            f"WHERE period = %s AND {_ACTIVE}{extra}{scope_clause}",  # noqa: S608
            [month, *scope_params],
        )

    def build() -> tuple[Decimal, int]:
        # build_cost has no provider/model columns; only feature scope applies.
        clause = " AND feature_id = %s" if st == "feature" else ""
        params = [month] + ([rule["scope_ref"]] if st == "feature" else [])
        return _sum(
            conn,
            f"SELECT COALESCE(SUM(amount),0), COUNT(*) FROM build_cost WHERE period = %s{clause}",  # noqa: S608
            params,
        )

    if metric == "inference_cost":
        val, n = infer()
        return val if n else None
    if metric == "build_cost":
        val, n = build()
        return val if n else None
    if metric == "combined_cost":
        iv, ic = infer()
        bv, bc = build()
        return (iv + bv) if (ic or bc) else None
    if metric == "unattributed_cost":
        iv, ic = infer(" AND feature_id IS NULL")
        bv, bc = _sum(
            conn,
            "SELECT COALESCE(SUM(amount),0), COUNT(*) FROM build_cost "
            "WHERE period = %s AND feature_id IS NULL",
            [month],
        )
        return (iv + bv) if (ic or bc) else None
    if metric == "token_usage":
        row = conn.execute(
            f"SELECT COALESCE(SUM(tokens_in),0)+COALESCE(SUM(tokens_out),0), COUNT(*) "
            f"FROM inference_cost WHERE period = %s AND {_ACTIVE}{scope_clause}",  # noqa: S608
            [month, *scope_params],
        ).fetchone()
        return Decimal(int(row[0])) if int(row[1]) else None
    if metric == "cost_per_user":
        iv, ic = infer()
        if not ic:
            return None
        users = conn.execute(
            "SELECT COALESCE(SUM(active_users),0) FROM feature_usage WHERE period = %s"
            + (" AND feature_id = %s" if st == "feature" else ""),
            [month] + ([rule["scope_ref"]] if st == "feature" else []),
        ).fetchone()[0]
        if not users:
            return None  # no active users -> can't compute cost per user
        return iv / Decimal(int(users))
    return None


def _observed_and_breach(
    conn, rule: dict, month: dt.date, *, tenant_id: str, now: dt.datetime
) -> tuple[Optional[Decimal], bool]:
    """Return (observed_lhs, breached) or (None, False) when insufficient data.

    observed_lhs is the number the user compares against the threshold: the metric
    value for 'exceeds'/'falls_below', or the computed percentage for the pct
    conditions.
    """
    value = _current_value(conn, rule, month, now)
    if value is None:
        return None, False
    threshold = Decimal(str(rule["threshold"]))
    cond = rule["condition_type"]
    if cond == "exceeds":
        return value, value > threshold
    if cond == "falls_below":
        return value, value < threshold
    if cond == "increase_pct":
        prev = _prior_value(conn, rule, month, now)
        if prev is None or prev <= 0:
            return None, False  # can't compute a percentage change reliably
        pct = (value - prev) / prev * Decimal("100")
        return pct, pct > threshold
    if cond == "budget_pct":
        # The denominator is the organization's persisted budget, prorated to
        # this month — which is what makes an annual budget and an effective date
        # work here for free. No budget means no denominator: insufficient data,
        # never a default or a demo figure.
        configured = budgets.get_budget(tenant_id)
        if configured is None:
            return None, False
        applicable = Decimal(str(budgets.prorate(configured, month, month)["amount"]))
        if applicable <= 0:
            return None, False
        pct = value / applicable * Decimal("100")
        return pct, pct > threshold
    return None, False


def _prev_month(month: dt.date) -> dt.date:
    return (month - dt.timedelta(days=1)).replace(day=1)


# ---- State machine --------------------------------------------------------
_RULE_FIELDS = (
    'metric, scope_type, scope_ref, condition_type, threshold, budget_amount, "window", '
    "cooldown, recovery_notify, enabled, last_notified_at"
)


def _load_rule(conn, alert_id: str) -> Optional[dict]:
    row = conn.execute(
        f"SELECT {_RULE_FIELDS} FROM alert_rule WHERE id = %s FOR UPDATE", (alert_id,)
    ).fetchone()
    if row is None:
        return None
    keys = [
        "metric",
        "scope_type",
        "scope_ref",
        "condition_type",
        "threshold",
        "budget_amount",
        "window",
        "cooldown",
        "recovery_notify",
        "enabled",
        "last_notified_at",
    ]
    return dict(zip(keys, row))


def evaluate_rule(tenant_id: str, alert_id: str, *, now: Optional[dt.datetime] = None) -> dict:
    """Evaluate one rule and apply the state transition. Returns a small summary.

    Notifications are dispatched AFTER the transition commits, so their
    ``alert_notification`` rows reference a persisted event and a delivery failure
    can never roll back the state change.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    month = _current_month(tenant_id, now)
    pending: Optional[tuple[str, dict]] = None  # (event_id, payload) to notify after commit

    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        rule = _load_rule(conn, alert_id)  # locks the row FOR UPDATE
        if rule is None or not rule["enabled"]:
            return {"status": "skipped"}

        observed, breached = _observed_and_breach(conn, rule, month, tenant_id=tenant_id, now=now)
        next_eval = now + _WINDOW_DELTA[rule["window"]]

        if observed is None:
            conn.execute(
                "UPDATE alert_rule SET status='insufficient_data', last_evaluated_at=%s, "
                "next_eval_at=%s WHERE id=%s",
                (now, next_eval, alert_id),
            )
            return {"status": "insufficient_data"}

        open_row = conn.execute(
            "SELECT id FROM alert_incident WHERE alert_id=%s AND status='open'", (alert_id,)
        ).fetchone()
        threshold = rule["threshold"]

        if breached and open_row is None:
            inc_id = conn.execute(
                """
                INSERT INTO alert_incident (tenant_id, alert_id, observed_value, threshold)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (alert_id) WHERE status='open' DO NOTHING
                RETURNING id
                """,
                (tenant_id, alert_id, observed, threshold),
            ).fetchone()
            if inc_id is not None:  # we opened the incident (won the race)
                event_id = _record_event(
                    conn,
                    tenant_id,
                    alert_id,
                    str(inc_id[0]),
                    "triggered",
                    observed,
                    threshold,
                    rule,
                )
                if event_id and _cooldown_ok(rule, now):
                    pending = (
                        event_id,
                        _payload(tenant_id, alert_id, "triggered", observed, threshold, rule),
                    )
                    conn.execute(
                        "UPDATE alert_rule SET last_notified_at=%s WHERE id=%s", (now, alert_id)
                    )
            conn.execute(
                "UPDATE alert_rule SET status='triggered', last_observed=%s, last_evaluated_at=%s, "
                "last_triggered_at=%s, next_eval_at=%s WHERE id=%s",
                (observed, now, now, next_eval, alert_id),
            )
        elif not breached and open_row is not None:
            conn.execute(
                "UPDATE alert_incident SET status='resolved', resolved_at=%s WHERE id=%s",
                (now, open_row[0]),
            )
            event_id = _record_event(
                conn, tenant_id, alert_id, str(open_row[0]), "resolved", observed, threshold, rule
            )
            if event_id and rule["recovery_notify"]:
                pending = (
                    event_id,
                    _payload(tenant_id, alert_id, "resolved", observed, threshold, rule),
                )
            conn.execute(
                "UPDATE alert_rule SET status='healthy', last_observed=%s, last_evaluated_at=%s, "
                "next_eval_at=%s WHERE id=%s",
                (observed, now, next_eval, alert_id),
            )
        else:
            conn.execute(
                "UPDATE alert_rule SET status=%s, last_observed=%s, last_evaluated_at=%s, "
                "next_eval_at=%s WHERE id=%s",
                ("triggered" if breached else "healthy", observed, now, next_eval, alert_id),
            )

    # ---- after commit: deliver notifications (independent per channel) ----
    if pending is not None:
        event_id, payload = pending
        notify.dispatch(tenant_id, alert_id, event_id, payload)
    return {"status": "triggered" if breached else "healthy"}


def _record_event(
    conn,
    tenant_id: str,
    alert_id: str,
    incident_id: str,
    event_type: str,
    observed: Decimal,
    threshold,
    rule: dict,
) -> Optional[str]:
    """Insert a triggered/resolved event idempotently. Returns the id if new."""
    event_key = f"{event_type}:{incident_id}"
    row = conn.execute(
        """
        INSERT INTO alert_event
            (tenant_id, alert_id, incident_id, event_type, event_key, observed_value,
             threshold, "window", window_start, message)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), %s)
        ON CONFLICT (tenant_id, event_key) DO NOTHING
        RETURNING id
        """,
        (
            tenant_id,
            alert_id,
            incident_id,
            event_type,
            event_key,
            observed,
            threshold,
            rule["window"],
            _message(event_type, rule, observed, threshold),
        ),
    ).fetchone()
    return str(row[0]) if row else None


def _cooldown_ok(rule: dict, now: dt.datetime) -> bool:
    last = rule.get("last_notified_at")
    if last is None or rule["cooldown"] == "none":
        return True
    return (now - last) >= _COOLDOWN_DELTA[rule["cooldown"]]


#: Kept in step with UNIT_LABELS/quantity() in web/src/alertLabels.ts, so the
#: Slack message and the in-app feed say the same thing about the same number.
_UNIT_SUFFIX = {
    "money": "",
    "tokens": " tokens",
    "runs": " runs",
    "minutes": " minutes",
    "steps": " steps",
    "repeats": "x",
    "percent": "%",
}


def _round(value) -> str:
    """One decimal place, with a bare integer left bare. "45 minutes", not "45.0"."""
    return f"{float(value):,.1f}".removesuffix(".0")


def _suffix(unit: str, value) -> str:
    """The unit word, singular when there is exactly one of the thing."""
    word = _UNIT_SUFFIX[unit]
    return word[:-1] if float(value) == 1 and word.endswith("s") else word


def _quantity(rule: dict, value) -> str:
    """A number in the units the rule is actually about.

    A percentage CONDITION overrides the metric's own unit: the observed value
    for an increase_pct rule is a percentage change, whatever the metric counts.
    """
    if rule["condition_type"] in ("increase_pct", "budget_pct"):
        return f"{_round(value)}%"
    unit = alerts.METRIC_UNITS.get(rule["metric"], "money")
    if unit == "money":
        return f"${float(value):,.2f}"
    if unit in ("runs", "steps", "repeats", "tokens"):
        return f"{float(value):,.0f}{_suffix(unit, value)}"
    return f"{_round(value)}{_suffix(unit, value)}"


def _message(event_type: str, rule: dict, observed: Decimal, threshold) -> str:
    verb = "triggered" if event_type == "triggered" else "resolved"
    comparison = "below" if rule["condition_type"] == "falls_below" else "vs"
    label = alerts.METRIC_LABELS.get(rule["metric"], rule["metric"])
    return (
        f"{label} {verb}: observed {_quantity(rule, observed)} "
        f"{comparison} threshold {_quantity(rule, threshold)}."
    )


def _deep_link_path(scope_type: str, scope_ref) -> str:
    """The most relevant in-app cost view for a rule's scope.

    Feature-scoped alerts open that feature's detail page; provider/model alerts
    open Cost Sources; everything else lands on the Overview.
    """
    if scope_type == "feature" and scope_ref:
        return f"/features/{scope_ref}"
    if scope_type == "application" and scope_ref:
        return f"/applications/{scope_ref}"
    if scope_type in ("provider", "model"):
        return "/cost-sources"
    return "/"


def _payload(
    tenant_id: str, alert_id: str, event_type: str, observed, threshold, rule: dict
) -> dict:
    base = os.environ.get("APP_BASE_URL", "")
    link = f"{base}{_deep_link_path(rule['scope_type'], rule['scope_ref'])}"
    org = _org_name(tenant_id)
    label = alerts.METRIC_LABELS.get(rule["metric"], rule["metric"])
    text = (
        f"[{org}] Alert {'RESOLVED' if event_type == 'resolved' else 'TRIGGERED'}: "
        f"{label} ({rule['scope_type']}) — observed {_quantity(rule, observed)}, "
        f"threshold {_quantity(rule, threshold)} over the {rule['window']} window. {link}"
    )
    return {
        "org": org,
        "event_type": event_type,
        "metric": rule["metric"],
        "scope_type": rule["scope_type"],
        "scope_ref": rule["scope_ref"],
        "observed": float(observed),
        "threshold": float(threshold),
        "window": rule["window"],
        "link": link,
        "text": text,
    }


def _org_name(tenant_id: str) -> str:
    with connect(admin_dsn()) as conn:
        row = conn.execute("SELECT name FROM tenant WHERE id = %s", (tenant_id,)).fetchone()
    return row[0] if row else "your organization"


def send_test(tenant_id: str, alert_id: str) -> dict:
    """Send a test notification through the rule's channels (records a 'test' event)."""
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        exists = conn.execute("SELECT 1 FROM alert_rule WHERE id=%s", (alert_id,)).fetchone()
        if not exists:
            return {"ok": False, "error": "not_found"}
        event_id = conn.execute(
            """
            INSERT INTO alert_event (tenant_id, alert_id, event_type, event_key, message)
            VALUES (%s, %s, 'test', %s, 'Test notification.')
            RETURNING id
            """,
            (
                tenant_id,
                alert_id,
                f"test:{alert_id}:{dt.datetime.now(dt.timezone.utc).timestamp()}",
            ),
        ).fetchone()[0]
    payload = {
        "org": _org_name(tenant_id),
        "event_type": "test",
        "text": "Meter test notification.",
    }
    results = notify.dispatch(tenant_id, alert_id, str(event_id), payload)
    return {"ok": True, "deliveries": results}


def run_scheduled_alert_eval(now: Optional[dt.datetime] = None) -> list[dict]:
    """Cron entry point: evaluate every enabled rule that's due (all tenants)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    with connect(admin_dsn()) as conn:
        due = conn.execute(
            "SELECT id, tenant_id FROM alert_rule WHERE enabled AND next_eval_at <= %s", (now,)
        ).fetchall()
    results = []
    for alert_id, tenant_id in due:
        try:
            results.append(
                {"alert_id": str(alert_id), **evaluate_rule(str(tenant_id), str(alert_id), now=now)}
            )
        except Exception as exc:  # noqa: BLE001 — one rule failing must not stop the rest
            logger.warning("alert eval failed for %s: %s", alert_id, exc)
            results.append({"alert_id": str(alert_id), "status": "error", "error": str(exc)})
    return results


if __name__ == "__main__":
    summary = run_scheduled_alert_eval()
    print(f"Evaluated {len(summary)} due alert rules.")
