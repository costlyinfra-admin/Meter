"""Offline tests, run on the customer's machine, results back as numbers (EX-2).

Whether a cheaper model holds quality is a question about answers, and Meter
does not hold answers. So this test runs where they are. Meter decides what is
being tested and how it will be judged; the customer's machine runs it — their
test cases, their keys, a judge model of their choosing — and sends back only
numbers: tokens, latency, whether a call failed or broke a check, and which
answer the judge preferred. Meter then prices the tokens itself and applies its
own rule. Nothing a customer typed, and nothing a model wrote, ever arrives.

The run holds one credential: a token that can fetch this experiment's
instructions and post its results once. It cannot read anything, and it is
spent the moment results arrive.

The decision rule is Meter's (docs/experiments-spec.md §9), with two dials a
customer may move (decision 4): how many more cases may go worse than better,
as a share of those judged, and how many cases are needed at all. A loosened
rule is recorded and said out loud wherever the result appears.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import secrets
import statistics
from decimal import Decimal
from typing import Optional

from . import dashboard, pricing, prompt_eval
from .db import admin_dsn, app_dsn, connect, tenant_tx

#: Providers the runner can call. Gemini models are right-sized too, but the
#: runner does not speak to Gemini yet, so those recommendations are not
#: offered for testing rather than offered a test that cannot run.
RUNNABLE = ("anthropic", "openai")
#: The rule as Meter would set it: the same as a prompt rewrite's (PO-4).
DEFAULT_LOSS_MARGIN = 0.10
DEFAULT_MIN_CASES = 20
#: The bounds on what a customer may set it to. Tighter is unremarkable;
#: looser is allowed and recorded (decision 4).
LOSS_MARGIN_RANGE = (0.0, 0.5)
MIN_CASES_RANGE = (10, 500)
#: Cases in one run. Enough to say something; few enough to be cheap to run.
MAX_CASES = 500
#: How long a run's token stays usable if no results arrive.
TOKEN_LIFETIME = dt.timedelta(days=14)
#: The deterministic checks, by name, as prompt_eval.failed_checks applies them.
CHECKS = ("empty_answer", "not_json", "refused", "much_longer")


class OfflineTestError(ValueError):
    """A request the application refuses, in words (maps to HTTP 400)."""


# ---------------------------------------------------------------------------
# What can be tested on this feature
# ---------------------------------------------------------------------------
def _feature_models(conn, feature_id: str, period: dt.date) -> list:
    """(model, spend, tokens_in, tokens_out) on the reconciled basis, as the
    right-sizing detector reads them."""
    return conn.execute(
        f"""
        SELECT model, SUM(amount),
               SUM(COALESCE(tokens_in, 0)), SUM(COALESCE(tokens_out, 0))
        FROM inference_cost
        WHERE feature_id = %s AND period = %s AND model IS NOT NULL
          AND {dashboard._ACTIVE_ENV} {dashboard._NOT_DOUBLE_COUNTED}
        GROUP BY model
        ORDER BY SUM(amount) DESC
        """,  # noqa: S608 - the clauses are dashboard's constants
        (feature_id, period, dashboard._connector_providers(conn, period)),
    ).fetchall()


def _choices(conn, feature_id: str, period: dt.date) -> list:
    out = []
    for model, spend, tin, tout in _feature_models(conn, feature_id, period):
        provider = pricing._vendor_of(model)
        if provider not in RUNNABLE:
            continue
        candidates = pricing.cheaper_same_vendor(model, int(tin or 0), int(tout or 0))
        if not candidates:
            continue
        names = [c["model"] for c in candidates]
        target = pricing.downgrade_target(model)
        out.append(
            {
                "model": model,
                "provider": provider,
                "monthly_spend": round(float(spend), 2),
                "default_candidate": target if target in names else names[0],
                "candidates": [
                    {"model": c["model"], "save_fraction": round(c["save_fraction"], 4)}
                    for c in candidates
                ],
            }
        )
    return out


def options(tenant_id: str, feature_id: str) -> Optional[dict]:
    """The models a customer can test on this feature, and the rule's dials."""
    from . import live_tests  # imported here: live_tests imports this module

    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        if conn.execute("SELECT 1 FROM feature WHERE id = %s", (feature_id,)).fetchone() is None:
            return None
        period = dashboard._resolve_period(conn, None)
        return {
            "period": period.isoformat(),
            "controls": _choices(conn, feature_id, period),
            "rule": {
                "loss_margin": DEFAULT_LOSS_MARGIN,
                "min_cases": DEFAULT_MIN_CASES,
                "loss_margin_range": list(LOSS_MARGIN_RANGE),
                "min_cases_range": list(MIN_CASES_RANGE),
            },
            # A live test's dials (EX-3): Meter's defaults, and how far each
            # may be moved.
            "live": {
                "defaults": dict(live_tests.DEFAULTS),
                "ranges": {k: list(v) for k, v in live_tests.RANGES.items()},
            },
        }


def validate_setting(
    conn,
    feature_id: str,
    period: dt.date,
    control_model,
    candidate_model,
    loss_margin,
    min_cases,
) -> dict:
    """The customer's choice, checked against what this feature can test."""
    choices = {c["model"]: c for c in _choices(conn, feature_id, period)}
    if control_model not in choices:
        raise OfflineTestError(
            "Choose a model this feature used this month, on a provider the test runner "
            "can call (Anthropic or OpenAI)."
        )
    allowed = {c["model"] for c in choices[control_model]["candidates"]}
    if candidate_model not in allowed:
        raise OfflineTestError(
            f"Choose a cheaper model from the same provider to test in place of {control_model}."
        )
    margin = DEFAULT_LOSS_MARGIN if loss_margin is None else float(loss_margin)
    if not LOSS_MARGIN_RANGE[0] <= margin <= LOSS_MARGIN_RANGE[1]:
        raise OfflineTestError("The allowance for worse answers must be between 0% and 50%.")
    cases = DEFAULT_MIN_CASES if min_cases is None else int(min_cases)
    if not MIN_CASES_RANGE[0] <= cases <= MIN_CASES_RANGE[1]:
        raise OfflineTestError(
            f"The number of cases needed must be between {MIN_CASES_RANGE[0]} and "
            f"{MIN_CASES_RANGE[1]}."
        )
    return {
        "provider": choices[control_model]["provider"],
        "control_model": control_model,
        "candidate_model": candidate_model,
        "loss_margin": round(margin, 3),
        "min_cases": cases,
    }


def relaxed(setting: dict) -> bool:
    """Whether the customer loosened Meter's rule."""
    return (
        float(setting["loss_margin"]) > DEFAULT_LOSS_MARGIN
        or int(setting["min_cases"]) < DEFAULT_MIN_CASES
    )


def rule_words(setting: dict) -> str:
    return (
        f"up to {round(float(setting['loss_margin']) * 100)}% more cases worse than better, "
        f"at least {int(setting['min_cases'])} cases"
    )


# ---------------------------------------------------------------------------
# The run's credential
# ---------------------------------------------------------------------------
def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token(conn, tenant_id: str, experiment_id: str) -> str:
    """A token for one run. Shown once; only its hash is kept."""
    token = "mtx_" + secrets.token_urlsafe(32)
    conn.execute(
        """
        INSERT INTO experiment_token (experiment_id, tenant_id, token_hash, expires_at)
        VALUES (%s, %s, %s, now() + %s)
        """,
        (experiment_id, tenant_id, _hash(token), TOKEN_LIFETIME),
    )
    return token


def resolve_token(token: str) -> Optional[tuple]:
    """(tenant_id, experiment_id) for a live run token, else None.

    The admin path, like the ingest token: the token is how the tenant is
    known, so there is no tenant to scope the lookup to yet.
    """
    if not token or not token.startswith("mtx_") or len(token) > 100:
        return None
    with connect(admin_dsn()) as conn:
        row = conn.execute(
            """
            SELECT t.tenant_id, t.experiment_id
              FROM experiment_token t JOIN experiment e ON e.id = t.experiment_id
             WHERE t.token_hash = %s AND t.used_at IS NULL AND t.expires_at > now()
               AND e.status = 'waiting_for_results'
            """,
            (_hash(token),),
        ).fetchone()
    return (str(row[0]), str(row[1])) if row else None


def spec(tenant_id: str, experiment_id: str) -> Optional[dict]:
    """What the runner needs to run this test. No tenant data beyond the models."""
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        row = conn.execute(
            """
            SELECT provider, control_model, candidate_model, loss_margin, min_cases
              FROM experiment WHERE id = %s AND status = 'waiting_for_results'
            """,
            (experiment_id,),
        ).fetchone()
    if row is None:
        return None
    provider, control, candidate, margin, min_cases = row
    return {
        "experiment_id": experiment_id,
        "lever": "model_rightsizing",
        "provider": provider,
        "control_model": control,
        "candidate_model": candidate,
        "rule": {"loss_margin": float(margin), "min_cases": int(min_cases)},
        "max_cases": MAX_CASES,
        "checks": list(CHECKS),
        "judge": {
            # The same instructions and the same both-orders protocol Meter
            # uses for a prompt rewrite, so a verdict means one thing.
            "system": prompt_eval._JUDGE_SYSTEM,
            "protocol": "both_orders",
        },
    }


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
def _cost_per_call(model: str, provider: str, calls: list) -> Optional[Decimal]:
    """Priced once over the summed tokens, so small calls keep their precision."""
    if not calls:
        return None
    tin = sum(c["tokens_in"] for c in calls)
    tout = sum(c["tokens_out"] for c in calls)
    return (pricing.price(model, tin, tout, provider) / Decimal(len(calls))).quantize(
        Decimal("0.000001")
    )


def _latency(calls: list) -> dict:
    values = sorted(c["latency_ms"] for c in calls)
    if not values:
        return {"p50": None, "p95": None}
    p95 = values[min(len(values) - 1, round(0.95 * (len(values) - 1)))]
    return {"p50": int(statistics.median(values)), "p95": int(p95)}


def decide(
    compared: int,
    better: int,
    same: int,
    worse: int,
    broken: int,
    control_cpc: Optional[Decimal],
    candidate_cpc: Optional[Decimal],
    setting: dict,
) -> tuple:
    """The rule. Meter's, with the customer's dials.

    In order: enough cases to mean anything; nothing broken; not losing more
    often than winning beyond the allowance; and actually cheaper. Each failure
    says which of the four it was.
    """
    margin = float(setting["loss_margin"])
    needed = int(setting["min_cases"])
    candidate = setting["candidate_model"]
    judged = better + same + worse
    if compared < needed:
        return "inconclusive", (
            f"Only {compared} case(s) could be compared; this test needs {needed}."
        )
    if broken:
        return "failed", (
            f"{broken} of {compared} answers from {candidate} came back broken — a failed "
            "call, or a check the current model's answer passed."
        )
    if judged < needed:
        return "inconclusive", (
            f"The judge could decide only {judged} of {compared} cases; this test needs {needed}."
        )
    if worse > better + margin * judged:
        return "failed", (
            f"Worse on {worse} and better on {better} of {judged} judged cases — more losses "
            "than the rule allows."
        )
    if control_cpc is None or candidate_cpc is None or candidate_cpc >= control_cpc:
        return "failed", f"{candidate} did not cost less per call on these cases."
    pct = round(float((control_cpc - candidate_cpc) / control_cpc) * 100)
    return "passed", (
        f"No broken answers. Better on {better}, same on {same}, worse on {worse} of "
        f"{judged}; {candidate} cost {pct}% less per call on these cases."
    )


def control_spend(conn, feature_id: str, period: dt.date, model: str) -> float:
    for m, spend, _tin, _tout in _feature_models(conn, feature_id, period):
        if m == model:
            return float(spend)
    return 0.0


def submit(tenant_id: str, experiment_id: str, payload: dict) -> Optional[dict]:
    """Record a run's results and decide. Returns None if the run is not open.

    `payload` has passed the request model (api.ExperimentResultsRequest),
    which refuses any field it does not name. The token is spent in the same
    transaction that records the results, so a second post finds it used.
    """

    cases = payload["cases"]
    if len({c["case"] for c in cases}) != len(cases):
        raise OfflineTestError("Each case may appear only once.")
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        spent = conn.execute(
            """
            UPDATE experiment_token SET used_at = now()
             WHERE experiment_id = %s AND used_at IS NULL AND expires_at > now()
            RETURNING experiment_id
            """,
            (experiment_id,),
        ).fetchone()
        row = conn.execute(
            """
            SELECT provider, control_model, candidate_model, loss_margin, min_cases, feature_id
              FROM experiment WHERE id = %s AND status = 'waiting_for_results'
            """,
            (experiment_id,),
        ).fetchone()
        if spent is None or row is None:
            return None
        feature_id, control = row[5], row[1]
        period = dashboard._resolve_period(conn, None)
        return record(
            conn,
            tenant_id,
            experiment_id,
            payload,
            period,
            control_spend(conn, feature_id, period, control),
        )


def record(
    conn, tenant_id: str, experiment_id: str, payload: dict, period: dt.date, spend: float
) -> dict:
    """Store a run's cases, decide, and complete the experiment, in `conn`.

    The part of `submit` after the token: also how the demo seeds a finished
    test, with `spend` (the feature's spend on the tested model this month)
    computed by the caller for its own tenant.
    """
    from . import experiments

    cases = payload["cases"]
    row = conn.execute(
        """
        SELECT provider, control_model, candidate_model, loss_margin, min_cases
          FROM experiment WHERE id = %s
        """,
        (experiment_id,),
    ).fetchone()
    provider, control, candidate, margin, min_cases = row
    setting = {
        "provider": provider,
        "control_model": control,
        "candidate_model": candidate,
        "loss_margin": float(margin),
        "min_cases": int(min_cases),
    }
    for c in cases:
        conn.execute(
            """
            INSERT INTO experiment_case
                (tenant_id, experiment_id, case_hash,
                 control_tokens_in, control_tokens_out, control_latency_ms, control_error,
                 candidate_tokens_in, candidate_tokens_out, candidate_latency_ms,
                 candidate_error, check_failures, verdict)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                tenant_id,
                experiment_id,
                c["case"],
                c["control"]["tokens_in"],
                c["control"]["tokens_out"],
                c["control"]["latency_ms"],
                c["control"]["error"],
                c["candidate"]["tokens_in"],
                c["candidate"]["tokens_out"],
                c["candidate"]["latency_ms"],
                c["candidate"]["error"],
                c["check_failures"],
                c["verdict"],
            ),
        )

    # A case the current model could not answer says nothing about the
    # candidate, so it is left out rather than counted against either.
    compared = [c for c in cases if not c["control"]["error"]]
    broken = sum(1 for c in compared if c["candidate"]["error"] or c["check_failures"] > 0)
    both_ok = [c for c in compared if not c["candidate"]["error"]]
    counts = {
        v: sum(1 for c in compared if c["verdict"] == v)
        for v in ("better", "same", "worse", "unjudged")
    }
    control_cpc = _cost_per_call(control, provider, [c["control"] for c in both_ok])
    candidate_cpc = _cost_per_call(candidate, provider, [c["candidate"] for c in both_ok])
    outcome, reason = decide(
        len(compared),
        counts["better"],
        counts["same"],
        counts["worse"],
        broken,
        control_cpc,
        candidate_cpc,
        setting,
    )
    if relaxed(setting):
        reason += f" Decided under a loosened rule: {rule_words(setting)}."
    fraction = (
        float((control_cpc - candidate_cpc) / control_cpc)
        if control_cpc and candidate_cpc is not None and control_cpc > 0
        else 0.0
    )

    # What the run itself cost, priced here from the tokens it reported.
    test_cost = sum(
        (
            pricing.price(control, c["control"]["tokens_in"], c["control"]["tokens_out"], provider)
            + pricing.price(
                candidate, c["candidate"]["tokens_in"], c["candidate"]["tokens_out"], provider
            )
            for c in cases
        ),
        Decimal("0"),
    )
    judge = payload.get("judge_usage")
    if judge and payload.get("judge_model"):
        test_cost += pricing.price(
            payload["judge_model"], judge["tokens_in"], judge["tokens_out"], judge["provider"]
        )

    result = {
        "kind": "offline",
        "cases": len(cases),
        "compared": len(compared),
        "control_errors": len(cases) - len(compared),
        "broken": broken,
        **counts,
        "control": {
            "model": control,
            "cost_per_call": float(control_cpc) if control_cpc is not None else None,
            "latency_ms": _latency([c["control"] for c in both_ok]),
        },
        "candidate": {
            "model": candidate,
            "cost_per_call": float(candidate_cpc) if candidate_cpc is not None else None,
            "latency_ms": _latency([c["candidate"] for c in both_ok]),
        },
        # Unrounded: the card recomputes the saving from it each month, and
        # a rounded copy would disagree with this result by a cent.
        "saving_fraction": fraction,
        "control_monthly_spend": round(spend, 2),
        "monthly_saving": round(spend * fraction, 2) if outcome == "passed" else 0.0,
        "rule": {
            **{k: setting[k] for k in ("loss_margin", "min_cases")},
            "relaxed": relaxed(setting),
        },
    }
    conn.execute(
        """
        UPDATE experiment
           SET status = 'completed', outcome = %s, outcome_reason = %s, result = %s::jsonb,
               results_source = %s, judge_model = %s, test_cost = %s, period = %s,
               completed_at = now()
         WHERE id = %s
        """,
        (
            outcome,
            reason[:500],
            json.dumps(result),
            payload["source"],
            payload.get("judge_model"),
            test_cost,
            period,
            experiment_id,
        ),
    )
    return experiments._to_dict(experiments._fetch(conn, experiment_id))


# ---------------------------------------------------------------------------
# Feeding a result back into the recommendation
# ---------------------------------------------------------------------------
def layer(conn, o: dict, done: dict, feature_id: str, period: dt.date) -> bool:
    """Apply finished offline tests to a right-sizing recommendation.

    `done` maps each tested model to its latest finished test. A
    recommendation can cover several models and each test covers one, so the
    figure is built part by part: a passed model contributes what the feature
    spent on it this month times the saving its test measured; a failed one
    contributes nothing; an untested or inconclusive one keeps its ceiling. The
    figure is **tested** only when every part passed or failed a test and at
    least one passed. Returns whether the figure changed.
    """
    parts = [t for t in o["trail"] if t.get("from_model")]
    tested = [t for t in parts if (done.get(t["from_model"]) or {}).get("outcome")]
    if not tested:
        return False
    total = 0.0
    passed, failed, open_parts = [], [], 0
    for t in parts:
        exp = done.get(t["from_model"])
        outcome = exp["outcome"] if exp else None
        if outcome == "passed":
            fraction = float(exp["result"]["saving_fraction"])
            total += control_spend(conn, feature_id, period, t["from_model"]) * fraction
            passed.append(exp)
        elif outcome == "failed":
            failed.append(exp)
        else:  # untested, or a test that could not decide
            total += float(t.get("monthly", 0.0))
            open_parts += 1
    all_live = bool(passed) and all(e["mode"] == "live" for e in passed)
    if passed:
        o["validation"] = "tested_live" if all_live else "tested_offline"
    elif failed:
        o["validation"] = "failed"
    else:
        o["validation"] = "inconclusive"
        return False
    if not passed and not open_parts:
        # Every part was tested and none held: the card stays, out of every total.
        o["test_failed"] = True
        return False
    if passed and not open_parts:
        o["savings_type"] = "tested"
        # A sample of cases is not the traffic: offline right-sizing tops out at
        # med. Real traffic that held up earns high (spec §7.3).
        o["confidence"] = "high" if all_live else "med"
    _set(o, total)  # after the confidence, which the ranking reads
    sentences = []
    for exp in passed:
        r = exp["result"]
        pct = round(float(r["saving_fraction"]) * 100)
        swap = f"{exp['setting']['candidate_model']} in place of {exp['setting']['control_model']}"
        if exp["mode"] == "live":
            g = r["groups"]["candidate"]
            sentences.append(
                f"Tested on live traffic: {swap} over {g['calls']:,} calls — {pct}% less per "
                "call, within the error and latency guardrails."
            )
        else:
            sentences.append(
                f"Tested on {r['compared']} of your own cases: {swap} — better on "
                f"{r['better']}, same on {r['same']}, worse on {r['worse']}, nothing broken, "
                f"{pct}% less per call."
            )
    for exp in failed:
        where = "on live traffic" if exp["mode"] == "live" else "on your own cases"
        sentences.append(
            f"{exp['setting']['control_model']} was tested {where} and did not hold, "
            "so its part is left out."
        )
    if open_parts:
        sentences.append("Other models in this figure are untested ceilings.")
    o["evidence"] = " ".join(sentences)
    if passed:
        o["fix"] = (
            "; ".join(
                f"Move {e['setting']['control_model']} → {e['setting']['candidate_model']}"
                for e in passed
            )
            + " — held up on your test cases."
        )
    return True


def _set(o: dict, monthly: float) -> None:
    from . import optimize_measured

    o["projected_monthly_savings"] = round(monthly, 2)
    o["projected_annual_savings"] = round(monthly * 12, 2)
    o["priority_score"] = optimize_measured._priority(
        o["projected_monthly_savings"], o["confidence"], o["engineering_effort"]
    )
