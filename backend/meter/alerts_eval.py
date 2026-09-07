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

from . import budgets, notify
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
    conn, rule: dict, month: dt.date, *, tenant_id: str
) -> tuple[Optional[Decimal], bool]:
    """Return (observed_lhs, breached) or (None, False) when insufficient data.

    observed_lhs is the number the user compares against the threshold: the metric
    value for 'exceeds', or the computed percentage for the pct conditions.
    """
    value = _metric_value(conn, rule, month)
    if value is None:
        return None, False
    threshold = Decimal(str(rule["threshold"]))
    cond = rule["condition_type"]
    if cond == "exceeds":
        return value, value > threshold
    if cond == "increase_pct":
        prev = _metric_value(conn, rule, month_start(_prev_month(month)))
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

        observed, breached = _observed_and_breach(conn, rule, month, tenant_id=tenant_id)
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


def _message(event_type: str, rule: dict, observed: Decimal, threshold) -> str:
    verb = "triggered" if event_type == "triggered" else "resolved"
    return (
        f"{rule['metric']} {verb}: observed {float(observed):,.2f} "
        f"vs threshold {float(threshold):,.2f}."
    )


def _deep_link_path(scope_type: str, scope_ref) -> str:
    """The most relevant in-app cost view for a rule's scope.

    Feature-scoped alerts open that feature's detail page; provider/model alerts
    open Cost Sources; everything else lands on the Overview.
    """
    if scope_type == "feature" and scope_ref:
        return f"/features/{scope_ref}"
    if scope_type in ("provider", "model"):
        return "/cost-sources"
    return "/"


def _payload(
    tenant_id: str, alert_id: str, event_type: str, observed, threshold, rule: dict
) -> dict:
    base = os.environ.get("APP_BASE_URL", "")
    link = f"{base}{_deep_link_path(rule['scope_type'], rule['scope_ref'])}"
    org = _org_name(tenant_id)
    text = (
        f"[{org}] Alert {'RESOLVED' if event_type == 'resolved' else 'TRIGGERED'}: "
        f"{rule['metric']} ({rule['scope_type']}) — observed {float(observed):,.2f}, "
        f"threshold {float(threshold):,.2f} over the {rule['window']} window. {link}"
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
