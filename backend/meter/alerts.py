"""Alert rules — CRUD, validation, destinations, summary, and the activity feed.

Everything is tenant-scoped through the app role (RLS), so a tenant only ever sees
its own rules/events. Channel secrets (Slack/webhook) are encrypted at rest with
the same ``crypto`` used for connector credentials and are NEVER returned to the
client — only a masked label is exposed.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Optional

from . import budgets, crypto
from .db import app_dsn, connect, tenant_tx

# ---- Domain vocabulary ----------------------------------------------------
METRICS = (
    "inference_cost",
    "build_cost",
    "combined_cost",
    "cost_per_user",
    "token_usage",
    "unattributed_cost",
)
METRIC_LABELS = {
    "inference_cost": "Inference cost",
    "build_cost": "Build cost",
    "combined_cost": "Combined AI cost",
    "cost_per_user": "Cost per active user",
    "token_usage": "Token usage",
    "unattributed_cost": "Unattributed spend",
}
SCOPES = ("organization", "provider", "model", "feature")
CONDITIONS = ("exceeds", "increase_pct", "budget_pct")
WINDOWS = ("hourly", "daily", "weekly", "monthly")
COOLDOWNS = ("none", "hour", "day", "week")
CHANNELS = ("in_app", "email", "slack", "webhook")

# Budget-percentage only makes sense for dollar-cost metrics.
_BUDGET_METRICS = {"inference_cost", "build_cost", "combined_cost", "unattributed_cost"}


def valid_conditions(metric: str) -> tuple[str, ...]:
    """Conditions that make sense for a metric (drives the form's condition list)."""
    conds = ["exceeds", "increase_pct"]
    if metric in _BUDGET_METRICS:
        conds.append("budget_pct")
    return tuple(conds)


# provider/model scope only applies to inference-derived metrics (build cost and
# cost-per-user have no provider/model dimension). Feature scope applies to all.
_PROVIDER_MODEL_METRICS = {"inference_cost", "token_usage", "unattributed_cost"}


def valid_scopes(metric: str) -> tuple[str, ...]:
    scopes = ["organization", "feature"]
    if metric in _PROVIDER_MODEL_METRICS:
        scopes[1:1] = ["provider", "model"]
    return tuple(scopes)


class AlertError(ValueError):
    """Invalid alert input (maps to HTTP 400)."""


# ---- Validation -----------------------------------------------------------
def _pos_number(value, field: str, *, allow_zero: bool = True) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise AlertError(f"{field} must be a number.") from exc
    if not d.is_finite():
        raise AlertError(f"{field} must be a finite number.")
    if d < 0 or (d == 0 and not allow_zero):
        raise AlertError(
            f"{field} must be {'zero or greater' if allow_zero else 'greater than 0'}."
        )
    return d


#: What the client is told when a budget-percentage rule has no budget to measure
#: against. The UI turns this into a link to Settings -> Budgets.
NO_BUDGET_MESSAGE = (
    "This alert measures spend against your organization's AI budget, and no "
    "budget is configured. Set one in Settings -> Budgets, then save this alert."
)


def _validate(payload: dict, *, tenant_id: str) -> dict:
    """Validate + normalize a rule payload. Returns the cleaned fields."""
    name = (payload.get("name") or "").strip()
    if not name:
        raise AlertError("Alert name is required.")
    if len(name) > 200:
        raise AlertError("Alert name must be at most 200 characters.")

    metric = payload.get("metric")
    if metric not in METRICS:
        raise AlertError(f"Unknown metric: {metric!r}.")

    scope_type = payload.get("scope_type") or "organization"
    if scope_type not in SCOPES:
        raise AlertError(f"Unknown scope: {scope_type!r}.")
    if scope_type not in valid_scopes(metric):
        raise AlertError(f"{scope_type.title()} scope isn't valid for {METRIC_LABELS.get(metric)}.")
    scope_ref = (payload.get("scope_ref") or "").strip() or None
    if scope_type != "organization" and not scope_ref:
        raise AlertError(f"A {scope_type} must be selected for a {scope_type}-scoped alert.")
    if scope_type == "organization":
        scope_ref = None

    condition = payload.get("condition_type")
    if condition not in valid_conditions(metric):
        raise AlertError(f"Condition {condition!r} is not valid for {METRIC_LABELS.get(metric)}.")

    threshold = _pos_number(payload.get("threshold"), "Threshold", allow_zero=False)
    if condition in ("increase_pct", "budget_pct") and threshold > Decimal("100000"):
        raise AlertError("Percentage threshold looks too large.")

    # A budget-percentage rule is measured against the organization's real,
    # persisted budget — not a number typed into the alert form, and never a
    # demo or default one. Without a budget the rule has no denominator, so it
    # cannot be saved.
    if condition == "budget_pct" and budgets.get_budget(tenant_id) is None:
        raise AlertError(NO_BUDGET_MESSAGE)

    window = payload.get("window")
    if window not in WINDOWS:
        raise AlertError(f"Unknown evaluation window: {window!r}.")
    cooldown = payload.get("cooldown") or "day"
    if cooldown not in COOLDOWNS:
        raise AlertError(f"Unknown cooldown: {cooldown!r}.")

    channels = payload.get("channels") or []
    if not channels:
        raise AlertError("Select at least one notification channel.")
    for ch in channels:
        if ch.get("channel") not in CHANNELS:
            raise AlertError(f"Unknown channel: {ch.get('channel')!r}.")
        if ch["channel"] in ("slack", "webhook") and not (ch.get("target") or ch.get("secret")):
            raise AlertError(f"{ch['channel'].title()} needs a URL.")

    return {
        "name": name,
        "description": (payload.get("description") or "").strip() or None,
        "metric": metric,
        "scope_type": scope_type,
        "scope_ref": scope_ref,
        "condition_type": condition,
        "threshold": threshold,
        # Deprecated: the denominator now comes from org_budget. Kept as a
        # column so existing rows survive; never written from here again.
        "budget_amount": None,
        "window": window,
        "cooldown": cooldown,
        "recovery_notify": bool(payload.get("recovery_notify", True)),
        "enabled": bool(payload.get("enabled", True)),
        "channels": channels,
    }


# ---- Destinations (channels) ---------------------------------------------
def _mask_url(url: str) -> str:
    """A safe, non-secret label for a webhook/Slack URL (scheme + host + …)."""
    try:
        scheme, rest = url.split("://", 1)
        host = rest.split("/", 1)[0]
        return f"{scheme}://{host}/…"
    except ValueError:
        return "configured"


def _destination_label(channel: str, target: Optional[str], has_secret: bool) -> str:
    if channel == "in_app":
        return "In-app"
    if channel == "email":
        return target or "(no address)"
    if channel == "slack":
        return "Slack webhook ••••" if has_secret else "Slack (needs URL)"
    if channel == "webhook":
        return _mask_url(target) if target else "Webhook ••••"
    return channel


def _write_destinations(conn, tenant_id: str, alert_id: str, channels: list[dict]) -> None:
    conn.execute("DELETE FROM alert_destination WHERE alert_id = %s", (alert_id,))
    for ch in channels:
        channel = ch["channel"]
        target = (ch.get("target") or "").strip() or None
        secret = ch.get("secret")
        # Slack incoming-webhook URLs ARE the secret -> store encrypted, not as target.
        cipher = None
        if channel == "slack" and target:
            cipher = crypto.encrypt(target)
            target = None
        elif channel == "webhook" and secret:
            cipher = crypto.encrypt(secret)
        conn.execute(
            """
            INSERT INTO alert_destination (tenant_id, alert_id, channel, target, secret_ciphertext)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (tenant_id, alert_id, channel, target, cipher),
        )


def _destinations(conn, alert_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, channel, target, secret_ciphertext IS NOT NULL "
        "FROM alert_destination WHERE alert_id = %s ORDER BY channel",
        (alert_id,),
    ).fetchall()
    return [
        {
            "id": str(rid),
            "channel": channel,
            "label": _destination_label(channel, target, has_secret),
            "target": target if channel == "email" else None,
            "configured": channel in ("in_app", "email") or has_secret or bool(target),
        }
        for rid, channel, target, has_secret in rows
    ]


# ---- Serialization --------------------------------------------------------
_RULE_COLS = (
    "id, name, description, metric, scope_type, scope_ref, condition_type, threshold, "
    'budget_amount, "window", cooldown, recovery_notify, enabled, status, last_observed, '
    "last_evaluated_at, last_triggered_at, next_eval_at, created_by, created_at, updated_at"
)


def _scope_label(conn, scope_type: str, scope_ref) -> Optional[str]:
    """Human-friendly scope reference — the feature's name rather than its UUID.

    For provider/model scopes the ref is already the readable name; for a feature
    scope we resolve the id to the feature name (falling back to the raw ref if the
    feature was since deleted). Organization scope has no ref.
    """
    if scope_type == "organization" or not scope_ref:
        return None
    if scope_type == "feature":
        row = conn.execute("SELECT name FROM feature WHERE id = %s", (scope_ref,)).fetchone()
        return row[0] if row else scope_ref
    return scope_ref


def _rule_dict(conn, row) -> dict:
    (
        rid,
        name,
        description,
        metric,
        scope_type,
        scope_ref,
        condition_type,
        threshold,
        budget,
        window,
        cooldown,
        recovery,
        enabled,
        status,
        last_observed,
        last_eval,
        last_trig,
        next_eval,
        created_by,
        created_at,
        updated_at,
    ) = row
    # A disabled rule reports 'disabled' regardless of its stored evaluation status.
    display_status = "disabled" if not enabled else status
    return {
        "id": str(rid),
        "name": name,
        "description": description,
        "metric": metric,
        "metric_label": METRIC_LABELS.get(metric, metric),
        "scope_type": scope_type,
        "scope_ref": scope_ref,
        "scope_label": _scope_label(conn, scope_type, scope_ref),
        "condition_type": condition_type,
        "threshold": float(threshold),
        "budget_amount": float(budget) if budget is not None else None,
        "window": window,
        "cooldown": cooldown,
        "recovery_notify": recovery,
        "enabled": enabled,
        "status": display_status,
        "last_observed": float(last_observed) if last_observed is not None else None,
        "last_evaluated_at": last_eval.isoformat() if last_eval else None,
        "last_triggered_at": last_trig.isoformat() if last_trig else None,
        "next_eval_at": next_eval.isoformat() if next_eval else None,
        "created_by": created_by,
        "created_at": created_at.isoformat() if created_at else None,
        "updated_at": updated_at.isoformat() if updated_at else None,
        "channels": _destinations(conn, str(rid)),
    }


def _get(conn, alert_id: str) -> Optional[dict]:
    row = conn.execute(f"SELECT {_RULE_COLS} FROM alert_rule WHERE id = %s", (alert_id,)).fetchone()
    return _rule_dict(conn, row) if row else None


# ---- Public API -----------------------------------------------------------
def list_rules(tenant_id: str) -> list[dict]:
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        rows = conn.execute(
            f"SELECT {_RULE_COLS} FROM alert_rule ORDER BY created_at DESC"
        ).fetchall()
        return [_rule_dict(conn, r) for r in rows]


def get_rule(tenant_id: str, alert_id: str) -> Optional[dict]:
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        return _get(conn, alert_id)


def create_rule(tenant_id: str, payload: dict, *, created_by: Optional[str] = None) -> dict:
    v = _validate(payload, tenant_id=tenant_id)
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        rid = conn.execute(
            """
            INSERT INTO alert_rule
                (tenant_id, name, description, metric, scope_type, scope_ref, condition_type,
                 threshold, budget_amount, "window", cooldown, recovery_notify, enabled,
                 status, created_by, next_eval_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    'insufficient_data', %s, now())
            RETURNING id
            """,
            (
                tenant_id,
                v["name"],
                v["description"],
                v["metric"],
                v["scope_type"],
                v["scope_ref"],
                v["condition_type"],
                v["threshold"],
                v["budget_amount"],
                v["window"],
                v["cooldown"],
                v["recovery_notify"],
                v["enabled"],
                created_by,
            ),
        ).fetchone()[0]
        _write_destinations(conn, tenant_id, str(rid), v["channels"])
        return _get(conn, str(rid))


def update_rule(tenant_id: str, alert_id: str, payload: dict) -> Optional[dict]:
    v = _validate(payload, tenant_id=tenant_id)
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        if _get(conn, alert_id) is None:
            return None
        conn.execute(
            """
            UPDATE alert_rule SET
                name = %s, description = %s, metric = %s, scope_type = %s, scope_ref = %s,
                condition_type = %s, threshold = %s, budget_amount = %s, "window" = %s,
                cooldown = %s, recovery_notify = %s, enabled = %s, updated_at = now()
            WHERE id = %s
            """,
            (
                v["name"],
                v["description"],
                v["metric"],
                v["scope_type"],
                v["scope_ref"],
                v["condition_type"],
                v["threshold"],
                v["budget_amount"],
                v["window"],
                v["cooldown"],
                v["recovery_notify"],
                v["enabled"],
                alert_id,
            ),
        )
        _write_destinations(conn, tenant_id, alert_id, v["channels"])
        return _get(conn, alert_id)


def set_enabled(tenant_id: str, alert_id: str, enabled: bool) -> Optional[dict]:
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        updated = conn.execute(
            "UPDATE alert_rule SET enabled = %s, updated_at = now(), "
            "next_eval_at = CASE WHEN %s THEN now() ELSE next_eval_at END "
            "WHERE id = %s RETURNING id",
            (enabled, enabled, alert_id),
        ).fetchone()
        return _get(conn, alert_id) if updated else None


def duplicate_rule(
    tenant_id: str, alert_id: str, *, created_by: Optional[str] = None
) -> Optional[dict]:
    original = get_rule(tenant_id, alert_id)
    if original is None:
        return None
    # Rebuild an editable payload from the original (channels come back without
    # secrets, so a duplicated Slack/webhook must be re-authorized — safest default).
    payload = {
        **original,
        "name": f"{original['name']} (copy)",
        "enabled": False,
        "channels": [
            {"channel": c["channel"], "target": c.get("target")}
            for c in original["channels"]
            if c["channel"] in ("in_app", "email")
        ]
        or [{"channel": "in_app"}],
    }
    return create_rule(tenant_id, payload, created_by=created_by)


def delete_rule(tenant_id: str, alert_id: str) -> bool:
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        deleted = conn.execute(
            "DELETE FROM alert_rule WHERE id = %s RETURNING id", (alert_id,)
        ).fetchone()
        return deleted is not None


def summary_counts(tenant_id: str) -> dict:
    """The four Alerts summary cards + the unread badge count."""
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        rows = conn.execute(
            "SELECT (CASE WHEN NOT enabled THEN 'disabled' ELSE status END) AS s, COUNT(*) "
            "FROM alert_rule GROUP BY s"
        ).fetchall()
        by_status = {s: int(c) for s, c in rows}
        unread = conn.execute(
            "SELECT COUNT(*) FROM alert_event WHERE NOT read "
            "AND event_type IN ('triggered', 'delivery_error')"
        ).fetchone()[0]
    return {
        "triggered": by_status.get("triggered", 0),
        "healthy": by_status.get("healthy", 0) + by_status.get("insufficient_data", 0),
        "delivery_errors": by_status.get("delivery_error", 0),
        "disabled": by_status.get("disabled", 0),
        "unread": int(unread),
    }


def unread_count(tenant_id: str) -> int:
    return summary_counts(tenant_id)["unread"]


# ---- Activity feed --------------------------------------------------------
def list_activity(tenant_id: str, *, limit: int = 100) -> list[dict]:
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        rows = conn.execute(
            """
            SELECT e.id, e.alert_id, r.name, r.metric, r.scope_type, r.scope_ref,
                   e.event_type, e.observed_value, e.threshold, e."window", e.message,
                   e.read, e.occurred_at
            FROM alert_event e JOIN alert_rule r ON r.id = e.alert_id
            ORDER BY e.occurred_at DESC LIMIT %s
            """,
            (limit,),
        ).fetchall()
        events = [
            {
                "id": str(eid),
                "alert_id": str(aid),
                "alert_name": name,
                "metric": metric,
                "metric_label": METRIC_LABELS.get(metric, metric),
                "scope_type": scope_type,
                "scope_ref": scope_ref,
                "scope_label": _scope_label(conn, scope_type, scope_ref),
                "event_type": etype,
                "observed_value": float(obs) if obs is not None else None,
                "threshold": float(thr) if thr is not None else None,
                "window": window,
                "message": message,
                "read": read,
                "occurred_at": occurred.isoformat() if occurred else None,
            }
            for (
                eid,
                aid,
                name,
                metric,
                scope_type,
                scope_ref,
                etype,
                obs,
                thr,
                window,
                message,
                read,
                occurred,
            ) in rows  # noqa: E501
        ]
        # Delivery status per event (safe metadata only).
        deliveries = conn.execute(
            "SELECT event_id, channel, status FROM alert_notification WHERE event_id = ANY(%s)",
            ([e["id"] for e in events],),
        ).fetchall()
    by_event: dict[str, list] = {}
    for eid, channel, status in deliveries:
        by_event.setdefault(str(eid), []).append({"channel": channel, "status": status})
    for e in events:
        e["deliveries"] = by_event.get(e["id"], [])
    return events


def mark_read(tenant_id: str, event_ids: list[str]) -> int:
    if not event_ids:
        return 0
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        n = conn.execute(
            "UPDATE alert_event SET read = true WHERE id = ANY(%s) AND NOT read RETURNING id",
            (list(event_ids),),
        ).fetchall()
        return len(n)


def mark_all_read(tenant_id: str) -> int:
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        n = conn.execute(
            "UPDATE alert_event SET read = true WHERE NOT read RETURNING id"
        ).fetchall()
        return len(n)


def rule_events(tenant_id: str, alert_id: str, *, limit: int = 30) -> list[dict]:
    """Recent events for one rule (detail view history)."""
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        rows = conn.execute(
            """
            SELECT id, event_type, observed_value, threshold, "window", message, read, occurred_at
            FROM alert_event WHERE alert_id = %s ORDER BY occurred_at DESC LIMIT %s
            """,
            (alert_id, limit),
        ).fetchall()
        notifs = conn.execute(
            "SELECT channel, status, error, attempts, created_at FROM alert_notification "
            "WHERE alert_id = %s ORDER BY created_at DESC LIMIT %s",
            (alert_id, limit),
        ).fetchall()
    return {
        "events": [
            {
                "id": str(eid),
                "event_type": etype,
                "observed_value": float(obs) if obs is not None else None,
                "threshold": float(thr) if thr is not None else None,
                "window": window,
                "message": message,
                "read": read,
                "occurred_at": occurred.isoformat() if occurred else None,
            }
            for (eid, etype, obs, thr, window, message, read, occurred) in rows
        ],
        "notifications": [
            {
                "channel": channel,
                "status": status,
                "error": error,
                "attempts": attempts,
                "created_at": created.isoformat() if created else None,
            }
            for (channel, status, error, attempts, created) in notifs
        ],
    }


def get_destination_secrets(tenant_id: str, alert_id: str) -> list[dict]:
    """Decrypted channels for the evaluator/notifier ONLY (never exposed via the API)."""
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        rows = conn.execute(
            "SELECT channel, target, secret_ciphertext FROM alert_destination WHERE alert_id = %s",
            (alert_id,),
        ).fetchall()
    out = []
    for channel, target, cipher in rows:
        secret = crypto.decrypt(cipher) if cipher else None
        # Slack stores its URL as the secret; expose it as the target for delivery.
        url = secret if channel == "slack" else target
        out.append({"channel": channel, "target": url, "secret": secret})
    return out


def preview_text(rule: dict) -> str:
    """Plain-language summary, e.g. 'Notify when daily inference cost exceeds $100.'"""
    metric = METRIC_LABELS.get(rule["metric"], rule["metric"]).lower()
    scope = rule.get("scope_ref") or (
        "the organization" if rule["scope_type"] == "organization" else ""
    )
    scope_part = f" for {scope}" if scope and rule["scope_type"] != "organization" else ""
    window = rule["window"]
    t = rule["threshold"]
    if rule["condition_type"] == "exceeds":
        cond = f"exceeds ${t:,.0f}"
    elif rule["condition_type"] == "increase_pct":
        cond = f"increases by more than {t:g}% vs the previous {window} period"
    else:
        cond = f"exceeds {t:g}% of the monthly budget"
    return f"Notify me when {window} {metric}{scope_part} {cond}."
