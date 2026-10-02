"""Live tests: a cheaper model on a share of real traffic (EX-3).

The offline test (offline_tests.py) asks whether a cheaper model answers the
customer's own cases as well. This asks what happens on their real traffic.
The customer's own feature-flag tool sends a share of requests to the cheaper
model; the SDK tags every call with this test and the group it was in; Meter
compares the two groups on what it already measures for every call — cost,
priced by Meter at ingest; whether the call failed; how long it took — plus a
quality score their own system sends, where there is one.

Three rules shape it (docs/experiments-spec.md §9):

- **No verdict before the minimum.** A test is decided only once each group
  has its minimum calls AND the minimum days have passed. Stopping the moment a
  result looks good produces false wins, so until then the comparison is shown
  as provisional and nothing changes on the recommendation.
- **Guardrails can stop it early.** If the cheaper model's error rate or its
  slowest-5% latency drifts past its limit, the test stops as failed, and its
  guardrail alert fires — a bad canary should page someone, not wait a week.
- **Cost must be clearly lower, and quality not clearly worse.** The cheaper
  model passes only if its cost per call is lower with a 95% interval clear of
  zero, and — where quality scores exist — its average is not below the
  current model's by more than the margin, at 95%.

The customer may loosen any dial (decision 4); a loosened rule is said out loud.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
from typing import Optional

from . import dashboard, offline_tests
from .db import admin_dsn, app_dsn, connect, tenant_tx

logger = logging.getLogger("meter.live_tests")

#: Meter's rule. Each is a dial a customer may move within RANGES.
DEFAULTS = {
    "traffic_share": 10,
    "min_calls": 500,
    "min_days": 7,
    "max_error_increase": 0.01,  # one percentage point
    "max_latency_increase": 0.25,  # a quarter slower at the slowest 5%
    "quality_margin": 0.05,  # 5% below the current model's average
}
RANGES = {
    "traffic_share": (1, 50),
    "min_calls": (100, 100_000),
    "min_days": (1, 30),
    "max_error_increase": (0.0, 0.2),
    "max_latency_increase": (0.0, 2.0),
    "quality_margin": (0.0, 0.5),
}
#: Calls each group needs before a guardrail can stop a test. Fewer, and one
#: unlucky timeout reads as a doubled error rate.
GUARDRAIL_MIN_CALLS = 100
#: Scores each group needs before quality is part of the verdict.
MIN_SCORES = 30
Z95 = 1.96
#: A test is checked at most this often when someone opens it.
EVALUATE_EVERY = dt.timedelta(minutes=5)


class LiveTestError(ValueError):
    """A request the application refuses, in words (maps to HTTP 400)."""


# ---------------------------------------------------------------------------
# The setting
# ---------------------------------------------------------------------------
def validate_setting(conn, feature_id: str, period: dt.date, control, candidate, dials) -> dict:
    """The models, checked as an offline test checks them, and the dials."""
    try:
        models = offline_tests.validate_setting(
            conn, feature_id, period, control, candidate, None, None
        )
    except offline_tests.OfflineTestError as exc:
        raise LiveTestError(str(exc)) from exc
    setting = {k: models[k] for k in ("provider", "control_model", "candidate_model")}
    for name, default in DEFAULTS.items():
        value = dials.get(name)
        value = default if value is None else value
        lo, hi = RANGES[name]
        if not lo <= float(value) <= hi:
            raise LiveTestError(_range_message(name, lo, hi))
        setting[name] = int(value) if isinstance(default, int) else round(float(value), 4)
    return setting


def _range_message(name: str, lo, hi) -> str:
    words = {
        "traffic_share": f"The share of traffic must be between {lo}% and {hi}%.",
        "min_calls": f"The calls needed per group must be between {lo:,} and {hi:,}.",
        "min_days": f"The days needed must be between {lo} and {hi}.",
        "max_error_increase": "The error-rate guardrail must be between 0 and 20 points.",
        "max_latency_increase": "The latency guardrail must be between 0% and 200% slower.",
        "quality_margin": "The quality margin must be between 0% and 50%.",
    }
    return words[name]


def relaxed(setting: dict) -> bool:
    """Whether the customer loosened Meter's rule (the traffic share is not a rule)."""
    return (
        int(setting["min_calls"]) < DEFAULTS["min_calls"]
        or int(setting["min_days"]) < DEFAULTS["min_days"]
        or float(setting["max_error_increase"]) > DEFAULTS["max_error_increase"]
        or float(setting["max_latency_increase"]) > DEFAULTS["max_latency_increase"]
        or float(setting["quality_margin"]) > DEFAULTS["quality_margin"]
    )


def rule_words(setting: dict) -> str:
    return (
        f"at least {int(setting['min_calls']):,} calls per group over "
        f"{int(setting['min_days'])} days; error rate at most "
        f"{float(setting['max_error_increase']) * 100:g} points higher; slowest 5% at most "
        f"{round(float(setting['max_latency_increase']) * 100)}% slower; quality at most "
        f"{round(float(setting['quality_margin']) * 100)}% lower"
    )


# ---------------------------------------------------------------------------
# What the two groups did
# ---------------------------------------------------------------------------
def stats(conn, experiment_id: str, setting: dict) -> dict:
    """Each group's calls, cost, errors, latency and quality, from what Meter
    already stores. Costs are Meter's own prices, set at ingest."""
    groups = {}
    for group, expected in (
        ("control", setting["control_model"]),
        ("candidate", setting["candidate_model"]),
    ):
        row = conn.execute(
            """
            SELECT COUNT(*),
                   COUNT(*) FILTER (WHERE status = 'error'),
                   -- Cost over successful calls only: a failed call costs next
                   -- to nothing, and counting it would make the model that
                   -- fails more look cheaper. Failures are the guardrail's job.
                   COUNT(*) FILTER (WHERE status = 'success'),
                   COALESCE(SUM(amount) FILTER (WHERE status = 'success'), 0),
                   COALESCE(SUM(amount * amount) FILTER (WHERE status = 'success'), 0),
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)
                       FILTER (WHERE latency_ms IS NOT NULL),
                   COUNT(*) FILTER (WHERE model IS NOT NULL AND model NOT LIKE %s),
                   MIN(started_at)
              FROM ai_span
             WHERE experiment_id = %s AND experiment_group = %s
               AND span_kind = 'llm' AND status <> 'running'
            """,
            (expected.replace("%", r"\%").replace("_", r"\_") + "%", experiment_id, group),
        ).fetchone()
        n, errors, ok, total, squares, p95, other_model, first = row
        total, squares = float(total), float(squares)
        mean = total / ok if ok else None
        var = (squares - ok * mean * mean) / (ok - 1) if ok and ok > 1 else 0.0
        score = conn.execute(
            """
            SELECT COALESCE(SUM(n), 0), COALESCE(SUM(total), 0), COALESCE(SUM(total_squares), 0)
              FROM experiment_score WHERE experiment_id = %s AND experiment_group = %s
            """,
            (experiment_id, group),
        ).fetchone()
        sn, stotal, ssq = int(score[0]), float(score[1]), float(score[2])
        smean = stotal / sn if sn else None
        svar = (ssq - sn * smean * smean) / (sn - 1) if sn > 1 else 0.0
        groups[group] = {
            "model": expected,
            "calls": int(n),
            "errors": int(errors),
            "error_rate": errors / n if n else None,
            "successes": int(ok),
            "cost_per_call": mean,
            "cost_variance": max(var, 0.0),
            "latency_p95_ms": int(p95) if p95 is not None else None,
            # Calls tagged into this group on a different model: the flag and
            # the model choice disagree somewhere in the customer's code.
            "other_model_calls": int(other_model),
            "scores": sn,
            "quality": smean,
            "quality_variance": max(svar, 0.0),
            "first_call_at": first.isoformat() if first else None,
        }
    return groups


def guardrails(groups: dict, setting: dict) -> list:
    """Guardrails the candidate has breached, in words. Empty until both groups
    have enough calls for a rate to mean anything."""
    control, candidate = groups["control"], groups["candidate"]
    if min(control["calls"], candidate["calls"]) < GUARDRAIL_MIN_CALLS:
        return []
    breaches = []
    allowed = float(setting["max_error_increase"])
    if candidate["error_rate"] - control["error_rate"] > allowed:
        breaches.append(
            f"{candidate['model']} failed {candidate['error_rate'] * 100:.1f}% of calls against "
            f"{control['error_rate'] * 100:.1f}% — more than {allowed * 100:g} points higher."
        )
    slower = float(setting["max_latency_increase"])
    c95, k95 = control["latency_p95_ms"], candidate["latency_p95_ms"]
    if c95 and k95 and k95 > c95 * (1 + slower):
        breaches.append(
            f"Its slowest 5% of calls took {k95:,} ms against {c95:,} ms — more than "
            f"{round(slower * 100)}% slower."
        )
    return breaches


def decide(groups: dict, setting: dict, elapsed: dt.timedelta) -> tuple:
    """(outcome or None while running, reason). The rule, in order."""
    control, candidate = groups["control"], groups["candidate"]
    breaches = guardrails(groups, setting)
    if breaches:
        return "failed", "Stopped by a guardrail: " + " ".join(breaches) + (
            f" Send this traffic back to {control['model']}."
        )
    needed, days = int(setting["min_calls"]), int(setting["min_days"])
    if min(control["calls"], candidate["calls"]) < needed or elapsed < dt.timedelta(days=days):
        return None, (
            f"{control['calls']:,} and {candidate['calls']:,} of the {needed:,} calls each group "
            f"needs, {min(elapsed.days, days)} of {days} days. No verdict before both — an early "
            "lead is not a result."
        )
    c_cost, k_cost = control["cost_per_call"], candidate["cost_per_call"]
    if not c_cost or k_cost is None or k_cost >= c_cost:
        return "failed", f"{candidate['model']} did not cost less per call on live traffic."
    se = math.sqrt(
        control["cost_variance"] / max(control["successes"], 1)
        + candidate["cost_variance"] / max(candidate["successes"], 1)
    )
    if (k_cost - c_cost) + Z95 * se >= 0:
        return "inconclusive", (
            f"{candidate['model']} looked cheaper, but the calls vary too much for the saving to "
            "be clear of zero. Run it longer, or on more traffic."
        )
    cheaper = round((1 - k_cost / c_cost) * 100)
    reason = (
        f"{candidate['model']} cost {cheaper}% less per call over {candidate['calls']:,} calls "
        f"(against {control['calls']:,} on {control['model']}), within the error and latency "
        "guardrails."
    )
    if min(control["scores"], candidate["scores"]) >= MIN_SCORES:
        c_q, k_q = control["quality"], candidate["quality"]
        floor = -float(setting["quality_margin"]) * abs(c_q)
        q_se = math.sqrt(
            control["quality_variance"] / control["scores"]
            + candidate["quality_variance"] / candidate["scores"]
        )
        diff = k_q - c_q
        if diff < floor:
            return "failed", (
                f"Quality fell: average score {k_q:.3g} against {c_q:.3g} — more than "
                f"{round(float(setting['quality_margin']) * 100)}% lower."
            )
        if diff - Z95 * q_se < floor:
            return "inconclusive", (
                f"{reason} But the quality scores vary too much to rule out a drop of more than "
                f"{round(float(setting['quality_margin']) * 100)}%. Run it longer."
            )
        reason += f" Average quality {k_q:.3g} against {c_q:.3g}."
    else:
        reason += (
            " Quality was not measured: no quality scores were sent, so this says nothing about "
            "whether the answers held up."
        )
    return "passed", reason


# ---------------------------------------------------------------------------
# Keeping a running test up to date
# ---------------------------------------------------------------------------
def _setting_of(row: dict) -> dict:
    keys = ("provider", "control_model", "candidate_model", *DEFAULTS)
    return {
        k: (
            float(row[k])
            if k in ("max_error_increase", "max_latency_increase", "quality_margin")
            else row[k]
        )
        for k in keys
    }


def evaluate(conn, experiment_id: str, *, now: Optional[dt.datetime] = None) -> Optional[dict]:
    """Re-read a running live test and decide it if it is due. Returns the
    stored result, or None if it is not a running live test."""
    now = now or dt.datetime.now(dt.timezone.utc)
    row = conn.execute(
        """
        SELECT id, feature_id, status, created_at, guardrail_alert_id, provider, control_model,
               candidate_model, traffic_share, min_calls, min_days, max_error_increase,
               max_latency_increase, quality_margin
          FROM experiment WHERE id = %s AND mode = 'live' AND status = 'running'
        """,
        (experiment_id,),
    ).fetchone()
    if row is None:
        return None
    keys = (
        "id",
        "feature_id",
        "status",
        "created_at",
        "guardrail_alert_id",
        "provider",
        "control_model",
        "candidate_model",
        *DEFAULTS,
    )
    d = dict(zip(keys, row))
    setting = _setting_of(d)
    groups = stats(conn, experiment_id, setting)
    outcome, reason = decide(groups, setting, now - d["created_at"])
    if outcome and relaxed(setting):
        reason += f" Decided under a loosened rule: {rule_words(setting)}."
    c_cost, k_cost = groups["control"]["cost_per_call"], groups["candidate"]["cost_per_call"]
    fraction = (1 - k_cost / c_cost) if c_cost and k_cost is not None else 0.0
    period = dashboard._resolve_period(conn, None)
    spend = offline_tests.control_spend(
        conn, str(d["feature_id"]), period, setting["control_model"]
    )
    result = {
        "kind": "live",
        "groups": groups,
        "guardrails": guardrails(groups, setting),
        "saving_fraction": fraction,
        "control_monthly_spend": round(spend, 2),
        "monthly_saving": round(spend * fraction, 2) if outcome == "passed" else 0.0,
        "rule": {**{k: setting[k] for k in DEFAULTS}, "relaxed": relaxed(setting)},
        "provisional": outcome is None,
    }
    conn.execute(
        """
        UPDATE experiment
           SET result = %s::jsonb, outcome_reason = %s, last_evaluated_at = %s,
               status = CASE WHEN %s::text IS NULL THEN status ELSE 'completed' END,
               outcome = %s,
               completed_at = CASE WHEN %s::text IS NULL THEN NULL ELSE %s::timestamptz END,
               period = %s
         WHERE id = %s
        """,
        (
            json.dumps(result),
            reason[:500],
            now,
            outcome,
            outcome,
            outcome,
            now,
            period,
            experiment_id,
        ),
    )
    if outcome and not result["guardrails"] and d["guardrail_alert_id"]:
        # Nothing left to guard: a finished test that did not breach a guardrail
        # has no reason to keep its alert on. One that did keeps it, triggered,
        # until someone turns it off — the breach really happened.
        conn.execute(
            "UPDATE alert_rule SET enabled = false WHERE id = %s", (d["guardrail_alert_id"],)
        )
    return result


def evaluate_if_due(conn, experiment_id: str, last: Optional[dt.datetime]) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    if last is None or now - last >= EVALUATE_EVERY:
        evaluate(conn, experiment_id, now=now)


def guardrail_observed(conn, alert_id: str) -> tuple:
    """For the guardrail alert (alerts_eval): (breaches, breached), or
    (None, False) while there is not yet enough traffic to judge."""
    from decimal import Decimal

    row = conn.execute(
        "SELECT id, status, result FROM experiment WHERE guardrail_alert_id = %s", (alert_id,)
    ).fetchone()
    if row is None:
        return None, False
    experiment_id, state, result = row
    if state == "running":
        result = evaluate(conn, str(experiment_id)) or result
    if not result:
        return None, False
    groups = result.get("groups") or {}
    if min((g.get("calls") or 0) for g in groups.values() or [{}]) < GUARDRAIL_MIN_CALLS:
        return None, False
    count = len(result.get("guardrails") or [])
    return Decimal(count), count > 0


def register_guardrail(
    conn, tenant_id: str, experiment_id: str, feature_id: str, setting: dict, actor: str
) -> str:
    """The alert this test's guardrails fire through. System-managed: created
    here, never from the alert form, and turned off when the test passes or is
    cancelled."""
    rid = conn.execute(
        """
        INSERT INTO alert_rule
            (tenant_id, name, description, metric, scope_type, scope_ref, condition_type,
             threshold, "window", cooldown, recovery_notify, enabled, status, created_by,
             next_eval_at)
        VALUES (%s, %s, %s, 'experiment_guardrail', 'feature', %s, 'exceeds', 0, 'hourly',
                'day', false, true, 'insufficient_data', %s, now())
        RETURNING id
        """,
        (
            tenant_id,
            f"Live test guardrail: {setting['candidate_model']} in place of "
            f"{setting['control_model']}"[:200],
            "Fires if the cheaper model's error rate or slowest-5% latency drifts past its "
            "limit during the live test. Managed by the test.",
            feature_id,
            actor,
        ),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO alert_destination (tenant_id, alert_id, channel) VALUES (%s, %s, 'in_app')",
        (tenant_id, rid),
    )
    conn.execute(
        "UPDATE experiment SET guardrail_alert_id = %s WHERE id = %s", (rid, experiment_id)
    )
    return str(rid)


def disable_guardrail(conn, experiment_id: str) -> None:
    conn.execute(
        """
        UPDATE alert_rule SET enabled = false
         WHERE id = (SELECT guardrail_alert_id FROM experiment WHERE id = %s)
        """,
        (experiment_id,),
    )


def run_scheduled(now: Optional[dt.datetime] = None) -> list:
    """Cron entry point: bring every running live test up to date (all tenants).

    Runs before the alert evaluation in the scheduled job, so a guardrail
    breach found here is notified in the same run.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    with connect(admin_dsn()) as conn:
        due = conn.execute(
            "SELECT id, tenant_id FROM experiment WHERE mode = 'live' AND status = 'running'"
        ).fetchall()
    results = []
    for experiment_id, tenant_id in due:
        try:
            with connect(app_dsn()) as conn, tenant_tx(conn, str(tenant_id)):
                evaluate(conn, str(experiment_id), now=now)
            results.append({"experiment_id": str(experiment_id), "status": "evaluated"})
        except Exception as exc:  # noqa: BLE001 — one test failing must not stop the rest
            logger.warning("live test evaluation failed for %s: %s", experiment_id, exc)
            results.append({"experiment_id": str(experiment_id), "status": "error"})
    return results


if __name__ == "__main__":
    summary = run_scheduled()
    print(f"Evaluated {len(summary)} running live tests.")
