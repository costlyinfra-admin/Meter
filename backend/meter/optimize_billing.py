"""Billing-data optimization — recommendations possible WITHOUT the SDK.

The measured optimizer (optimize_measured.py) needs request-level telemetry from
the metering SDK. Until that is installed, Meter may still hold authoritative
provider billing plus resource metadata — enough for a real, if narrower, set of
recommendations. This module produces ONLY those, and never touches the measured
path or its totals.

TRUTHFULNESS RULES (the whole point of this module):
  * every number is read from stored data and a deterministic calculation;
  * we never infer that a model downgrade is safe, that prompts can be shortened,
    that calls are duplicated, that caching is possible, nor any per-feature,
    per-user or quality conclusion — those need request-level evidence;
  * spend under review is NEVER called savings. Savings is "not_quantified"
    unless an action makes cost disappear by definition (deterministic) or a
    before/after reduction was observed (measured);
  * a rule whose evidence is missing emits nothing at all.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from .db import app_dsn, connect, tenant_tx
from .providers import month_start

# 'ignore'-classified spend is excluded everywhere, as elsewhere in reporting.
_ACTIVE_ENV = "(environment IS NULL OR environment <> 'ignore')"

# --- Deterministic thresholds (defined in code, shown in the evidence) --------
#: Dust guard — below this a finding is noise, not a recommendation.
MIN_SPEND = 1.0
#: One resource at or above this share of period spend is "concentrated".
CONCENTRATION_SHARE_PCT = 40.0
#: Growth must clear BOTH a relative and an absolute bar to be worth surfacing.
GROWTH_MIN_PCT = 25.0
GROWTH_MIN_ABS = 50.0

#: Alert metrics that count as a cost control over billing-derived spend.
_COST_CONTROL_METRICS = (
    "inference_cost",
    "build_cost",
    "combined_cost",
    "token_usage",
    "unattributed_cost",
)

_CONFIDENCE_RANK = {"high": 2, "medium": 1}


def _not_quantified(explanation: str) -> dict:
    return {"kind": "not_quantified", "amount": None, "explanation": explanation}


def _opportunity(
    *,
    oid: str,
    otype: str,
    title: str,
    description: str,
    source: str,
    period_start: dt.date,
    period_end: dt.date,
    observed_cost: Optional[float],
    calculation: str,
    confidence: str,
    impact_kind: str,
    impact_amount: Optional[float],
    savings: dict,
    limitations: list[str],
    action: dict,
    token_count: Optional[int] = None,
    resource_id: Optional[str] = None,
) -> dict:
    return {
        "id": oid,
        "type": otype,
        "title": title,
        "description": description,
        "evidence": {
            "source": source,
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "observed_cost": observed_cost,
            "token_count": token_count,
            "resource_id": resource_id,
            "calculation": calculation,
        },
        "confidence": confidence,
        "impact": {"kind": impact_kind, "amount": impact_amount},
        "savings": savings,
        "limitations": limitations,
        "action": action,
    }


def _unclassified(conn, start: dt.date, end: dt.date) -> list[dict]:
    """Rule 1 — provider spend on resources the user has not classified yet."""
    rows = conn.execute(
        f"""
        SELECT provider,
               COALESCE(api_key_name, api_key_id, workspace_name, workspace_id) AS resource,
               COALESCE(api_key_id, workspace_id) AS resource_id,
               SUM(amount)
        FROM inference_cost
        WHERE period BETWEEN %s AND %s AND {_ACTIVE_ENV}
          AND (environment IS NULL OR environment = 'unclassified')
          AND (api_key_id IS NOT NULL OR workspace_id IS NOT NULL)
        GROUP BY provider, resource, resource_id
        HAVING SUM(amount) >= %s
        ORDER BY SUM(amount) DESC
        """,  # noqa: S608
        (start, end, MIN_SPEND),
    ).fetchall()
    out = []
    for provider, resource, resource_id, amount in rows:
        cost = float(amount)
        out.append(
            _opportunity(
                oid=f"unclassified:{provider}:{resource_id}",
                otype="unclassified_spend",
                title=f"Classify {resource}",
                description=(
                    f"{provider} spend on this resource is not classified, so it cannot be "
                    "split into production, development/test or internal reporting."
                ),
                source="provider billing (cost API) + resource classification",
                period_start=start,
                period_end=end,
                observed_cost=cost,
                resource_id=resource_id,
                calculation=(
                    "SUM(inference_cost.amount) for this resource over the period, where the "
                    "stored classification is unclassified"
                ),
                confidence="high",
                impact_kind="spend_to_review",
                impact_amount=cost,
                savings=_not_quantified(
                    "Classifying spend changes reporting, not the bill. No savings claimed."
                ),
                limitations=[
                    "Shows where spend is unlabelled — not that it is wasteful.",
                ],
                action={"label": "Review classification", "href": "/cost-sources"},
            )
        )
    return out


def _unattributed(conn, start: dt.date, end: dt.date) -> list[dict]:
    """Rule 2 — observed spend not mapped to any feature."""
    inf = float(
        conn.execute(
            f"SELECT COALESCE(SUM(amount), 0) FROM inference_cost "
            f"WHERE period BETWEEN %s AND %s AND feature_id IS NULL AND {_ACTIVE_ENV}",  # noqa: S608
            (start, end),
        ).fetchone()[0]
    )
    bld = float(
        conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM build_cost "
            "WHERE period BETWEEN %s AND %s AND feature_id IS NULL",
            (start, end),
        ).fetchone()[0]
    )
    out = []
    for kind, cost, table in (("inference", inf, "inference_cost"), ("build", bld, "build_cost")):
        if cost < MIN_SPEND:
            continue
        out.append(
            _opportunity(
                oid=f"unattributed:{kind}",
                otype="unattributed_spend",
                title=f"Attribute unmapped {kind} spend",
                description=(
                    f"This {kind} spend is not mapped to a feature, so it cannot be held to "
                    "an owner. Map the API key/project, or split shared keys per workload."
                ),
                source=f"provider billing ({table})",
                period_start=start,
                period_end=end,
                observed_cost=cost,
                calculation=f"SUM({table}.amount) over the period WHERE feature_id IS NULL",
                confidence="high",
                impact_kind="visibility",
                impact_amount=cost,
                savings=_not_quantified(
                    "Attribution improves accountability; it does not reduce the bill."
                ),
                limitations=[
                    "Meter maps spend only where you define an explicit key/project "
                    "mapping — it never guesses which feature spent the money.",
                ],
                action={"label": "View cost source", "href": "/cost-sources"},
            )
        )
    return out


def _dev_internal(conn, start: dt.date, end: dt.date) -> list[dict]:
    """Rule 3 — spend the USER classified as development/test or internal."""
    rows = conn.execute(
        """
        SELECT environment, SUM(amount)
        FROM inference_cost
        WHERE period BETWEEN %s AND %s AND environment IN ('development', 'internal')
        GROUP BY environment
        HAVING SUM(amount) >= %s
        ORDER BY SUM(amount) DESC
        """,
        (start, end, MIN_SPEND),
    ).fetchall()
    labels = {"development": "development/test", "internal": "internal"}
    out = []
    for env, amount in rows:
        cost = float(amount)
        out.append(
            _opportunity(
                oid=f"non_production:{env}",
                otype="non_production_spend",
                title=f"Review {labels[env]} spend",
                description=(
                    f"You classified this spend as {labels[env]}. Worth a periodic review of "
                    "budgets, rate limits, retention and schedules — and whether the workload "
                    "is still needed."
                ),
                source="provider billing + your classification",
                period_start=start,
                period_end=end,
                observed_cost=cost,
                calculation=(
                    f"SUM(inference_cost.amount) over the period WHERE environment = '{env}' "
                    "(classification set by you)"
                ),
                confidence="high",
                impact_kind="spend_to_review",
                impact_amount=cost,
                savings=_not_quantified(
                    "Non-production spend is not waste by definition. Savings are only "
                    "quantified once you confirm a resource is no longer needed."
                ),
                limitations=[
                    "Based on your own classification — Meter does not judge whether the "
                    "workload is necessary.",
                ],
                action={"label": "View cost source", "href": "/cost-sources"},
            )
        )
    return out


def _concentration(conn, start: dt.date, end: dt.date) -> list[dict]:
    """Rule 4 — one resource is a large, objectively computed share of spend."""
    total = float(
        conn.execute(
            f"SELECT COALESCE(SUM(amount), 0) FROM inference_cost "
            f"WHERE period BETWEEN %s AND %s AND {_ACTIVE_ENV}",  # noqa: S608
            (start, end),
        ).fetchone()[0]
    )
    if total < MIN_SPEND:
        return []
    top = conn.execute(
        f"""
        SELECT COALESCE(api_key_name, api_key_id, workspace_name, workspace_id) AS resource,
               COALESCE(api_key_id, workspace_id) AS resource_id,
               SUM(amount)
        FROM inference_cost
        WHERE period BETWEEN %s AND %s AND {_ACTIVE_ENV}
          AND (api_key_id IS NOT NULL OR workspace_id IS NOT NULL)
        GROUP BY resource, resource_id
        ORDER BY SUM(amount) DESC LIMIT 1
        """,  # noqa: S608
        (start, end),
    ).fetchone()
    if not top:
        return []
    resource, resource_id, amount = top
    cost = float(amount)
    share = cost / total * 100.0
    if share < CONCENTRATION_SHARE_PCT:
        return []
    return [
        _opportunity(
            oid=f"concentration:{resource_id}",
            otype="cost_concentration",
            title=f"{resource} is {share:.0f}% of inference spend",
            description=(
                "One resource carries most of the spend. Review it first, and consider an "
                "alert or budget on it — or splitting a shared key if attribution is unclear."
            ),
            source="provider billing (cost API)",
            period_start=start,
            period_end=end,
            observed_cost=cost,
            resource_id=resource_id,
            calculation=(
                f"{cost:.2f} / {total:.2f} = {share:.1f}% of period inference spend; "
                f"surfaced at >= {CONCENTRATION_SHARE_PCT:.0f}%"
            ),
            confidence="high",
            impact_kind="spend_to_review",
            impact_amount=cost,
            savings=_not_quantified(
                "Concentration is a risk/visibility signal, not waste. No savings claimed."
            ),
            limitations=[
                "A concentrated resource may be entirely legitimate — this only says where "
                "to look first.",
            ],
            action={"label": "Create alert", "href": "/alerts/new"},
        )
    ]


def _growth(conn, today: dt.date) -> list[dict]:
    """Rule 5 — period-over-period growth across two COMPLETE months.

    The current calendar month is month-to-date and therefore never comparable, so
    it is excluded outright rather than compared and caveated.
    """
    this_month = month_start(today)
    prev = _months_back(this_month, 1)  # last complete month
    prior = _months_back(this_month, 2)  # the one before it
    row = conn.execute(
        f"""
        SELECT COALESCE(SUM(amount) FILTER (WHERE period = %s), 0),
               COALESCE(SUM(amount) FILTER (WHERE period = %s), 0)
        FROM inference_cost
        WHERE period IN (%s, %s) AND {_ACTIVE_ENV}
        """,  # noqa: S608
        (prev, prior, prev, prior),
    ).fetchone()
    current, previous = float(row[0]), float(row[1])
    # Both complete months must actually carry data, else there is nothing to compare.
    if previous < MIN_SPEND or current < MIN_SPEND:
        return []
    delta = current - previous
    if delta < GROWTH_MIN_ABS:
        return []
    pct = delta / previous * 100.0
    if pct < GROWTH_MIN_PCT:
        return []
    return [
        _opportunity(
            oid=f"growth:{prev.isoformat()}",
            otype="cost_growth",
            title=f"Inference spend rose {pct:.0f}% month over month",
            description=(
                "Spend grew between the two most recent complete months. Investigate the "
                "provider/resource behind it, or set an alert to catch the next move."
            ),
            source="provider billing (cost API), complete months only",
            period_start=prior,
            period_end=prev,
            observed_cost=current,
            calculation=(
                f"{prev:%b %Y} {current:.2f} vs {prior:%b %Y} {previous:.2f} = "
                f"+{delta:.2f} (+{pct:.1f}%); surfaced at >= {GROWTH_MIN_PCT:.0f}% "
                f"and >= ${GROWTH_MIN_ABS:.0f}"
            ),
            confidence="medium",
            impact_kind="spend_to_review",
            impact_amount=delta,
            savings=_not_quantified(
                "Growth is a signal to investigate. Nothing is saved until you act on a cause."
            ),
            limitations=[
                "Compares whole months only; the in-progress month is excluded.",
                "Growth is not by itself an anomaly — expected load changes look the same.",
            ],
            action={"label": "Create alert", "href": "/alerts/new"},
        )
    ]


def _missing_cost_control(conn, start: dt.date, end: dt.date) -> list[dict]:
    """Rule 6 — meaningful spend exists but no cost alert guards it."""
    spend = float(
        conn.execute(
            f"""
            SELECT COALESCE((SELECT SUM(amount) FROM inference_cost
                             WHERE period BETWEEN %s AND %s AND {_ACTIVE_ENV}), 0)
                 + COALESCE((SELECT SUM(amount) FROM build_cost
                             WHERE period BETWEEN %s AND %s), 0)
            """,  # noqa: S608
            (start, end, start, end),
        ).fetchone()[0]
    )
    if spend < MIN_SPEND:
        return []
    existing = conn.execute(
        "SELECT COUNT(*) FROM alert_rule WHERE enabled AND metric = ANY(%s)",
        (list(_COST_CONTROL_METRICS),),
    ).fetchone()[0]
    if existing:
        return []
    return [
        _opportunity(
            oid="missing_cost_control",
            otype="missing_cost_control",
            title="No cost alert is watching this spend",
            description=(
                "You have AI spend but no enabled alert on inference, build, combined spend, "
                "token usage or unattributed spend. An alert turns a surprise into a warning."
            ),
            source="provider billing + alert rules",
            period_start=start,
            period_end=end,
            observed_cost=spend,
            calculation=(
                "SUM(inference_cost.amount) + SUM(build_cost.amount) over the period, with "
                "zero enabled alert_rule rows on a cost metric"
            ),
            confidence="high",
            impact_kind="risk_reduction",
            impact_amount=None,
            savings=_not_quantified(
                "A control prevents surprises; it does not reduce current spend."
            ),
            limitations=["Counts enabled alerts only; a disabled rule does not protect you."],
            action={"label": "Create alert", "href": "/alerts/new"},
        )
    ]


def _months_back(day: dt.date, n: int) -> dt.date:
    month, year = day.month - n, day.year
    while month <= 0:
        month += 12
        year -= 1
    return dt.date(year, month, 1)


def billing_opportunities(
    tenant_id: str,
    start: dt.date,
    end: dt.date,
    *,
    today: Optional[dt.date] = None,
) -> list[dict]:
    """Evidence-backed recommendations derivable from billing data alone.

    Ranked deterministically by observed spend, then evidence confidence, then
    recency, then actionability — never by an invented savings percentage.
    """
    today = today or dt.date.today()
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        found = (
            _unclassified(conn, start, end)
            + _unattributed(conn, start, end)
            + _dev_internal(conn, start, end)
            + _concentration(conn, start, end)
            + _growth(conn, today)
            + _missing_cost_control(conn, start, end)
        )
    found.sort(
        key=lambda o: (
            -(o["evidence"]["observed_cost"] or 0.0),  # 1. observed spend affected
            -_CONFIDENCE_RANK.get(o["confidence"], 0),  # 2. evidence confidence
            o["evidence"]["period_end"],  # 3. recency
            o["action"]["label"],  # 4. actionability (stable tiebreak)
        )
    )
    return found


def has_sdk_telemetry(tenant_id: str, start: dt.date, end: dt.date) -> bool:
    """Whether request-level SDK signals exist — gates the measured optimizer."""
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        return bool(
            conn.execute(
                "SELECT 1 FROM usage_signal WHERE period BETWEEN %s AND %s LIMIT 1",
                (start, end),
            ).fetchone()
        )


def has_billing_data(tenant_id: str, start: dt.date, end: dt.date) -> bool:
    """Whether any authoritative cost is stored for the period."""
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        return bool(
            conn.execute(
                """
                SELECT 1 WHERE EXISTS (
                    SELECT 1 FROM inference_cost WHERE period BETWEEN %s AND %s
                ) OR EXISTS (
                    SELECT 1 FROM build_cost WHERE period BETWEEN %s AND %s
                )
                """,
                (start, end, start, end),
            ).fetchone()
        )
