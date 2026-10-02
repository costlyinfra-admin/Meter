"""Test a recommendation before changing anything (EX-1, docs/experiments-spec.md).

Meter finds an opportunity and prices it under its own assumptions. This module
lets the customer replace those assumptions with theirs, and see what the
opportunity is worth then, before anyone changes a line of code.

EX-1 tests by **simulation**: no prompts, no model calls, no cost. Two
recommendations can be tested that way, because each rests on one assumption
the customer knows better than Meter does and the SDK's counters can answer
for any value of it:

- **Repeated requests.** The detector assumes a cached answer may be reused for
  ten minutes. Whether that is true depends on the customer's data, so the
  customer picks the limit (1 minute to 24 hours), and whether to count only
  repeats inside a named customer or cache scope. The answer is how many calls
  a cache that fresh would have served, and what they cost.
- **Prompt caching.** The detector prices the provider's default 5-minute
  cache. Anthropic also sells a 1-hour cache, which costs more per write but is
  rewritten less often; which saves more depends on the gaps between calls.

What an experiment stores is the customer's **setting**, and what it found at
the time. What a recommendation SHOWS is recomputed every period under that
setting, the way every detector is — so the figure stays current and the
experiment remains the record of what was seen when the test was run.

Three rules govern how a result changes a recommendation (spec §7):

- passed: the figure becomes the simulated one, and the recommendation says it
  was simulated;
- failed: the recommendation stays visible as "tested — did not hold", and
  leaves the totals and the ranking (a failed test is evidence, not deletion);
- inconclusive or waiting: nothing changes.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal
from typing import Optional

from . import dashboard, offline_tests, pricing
from .db import app_dsn, connect, tenant_tx

#: What can be tested, and how. Grows with EX-2 (offline) and EX-3 (live).
SIMULATABLE = ("duplicate_calls", "prompt_caching")
#: Tested offline, on the customer's machine (EX-2, offline_tests.py).
OFFLINE = ("model_rightsizing",)
#: Freshness limits a customer can choose for a response cache, in seconds.
#: The same four the SDK counts at (hook.SIMULATION_LIMITS).
TTL_CHOICES = {60: "1 minute", 600: "10 minutes", 3600: "1 hour", 86400: "24 hours"}
_TTL_SUFFIX = {60: "1m", 600: "10m", 3600: "1h", 86400: "24h"}
#: Cache lifetimes a provider sells.
CACHE_TTLS = {"5m": "5 minutes", "1h": "1 hour"}
#: The same, as the word before "cache".
_CACHE_ADJ = {"5m": "5-minute", "1h": "1-hour"}
#: Calls a simulation needs before it decides anything. A handful of calls
#: agreeing is not evidence about a month of traffic.
MIN_CALLS = 200
#: Below this a saving is noise, the same floor the detectors use.
MIN_SAVING = Decimal("1")


class ExperimentError(ValueError):
    """A test that cannot be started as asked (maps to HTTP 400)."""


# ---------------------------------------------------------------------------
# The simulations
# ---------------------------------------------------------------------------
def _simulate_repeats(conn, feature_id: str, period: dt.date, ttl: int, scoped_only: bool) -> dict:
    """What a response cache with this freshness limit would have served.

    Every limit is computed, not just the chosen one, so the customer can see
    the trade-off they are choosing along. Each hit is priced at the tokens it
    actually used, at list price — the same basis as the detector.
    """
    rows = conn.execute(
        """
        SELECT provider, model, scope_kind, calls,
               hits_1m, hits_10m, hits_1h, hits_24h,
               hit_tokens_in_1m, hit_tokens_in_10m, hit_tokens_in_1h, hit_tokens_in_24h,
               hit_tokens_out_1m, hit_tokens_out_10m, hit_tokens_out_1h, hit_tokens_out_24h,
               evicted_1m, evicted_10m, evicted_1h, evicted_24h
        FROM usage_simulation
        WHERE feature_id = %s AND period = %s
        """,
        (feature_id, period),
    ).fetchall()
    calls = sum(int(r[3]) for r in rows)
    unscoped_calls = sum(int(r[3]) for r in rows if r[2] == "unscoped")
    ladder = []
    for i, (seconds, label) in enumerate(TTL_CHOICES.items()):
        hits = 0
        saving = Decimal("0")
        evicted = 0
        for r in rows:
            if scoped_only and r[2] != "explicit":
                continue
            hits += int(r[4 + i])
            saving += pricing.price(r[1] or "", int(r[8 + i]), int(r[12 + i]), r[0])
            evicted += int(r[16 + i])
        ladder.append(
            {
                "ttl_seconds": seconds,
                "label": label,
                "hits": hits,
                "hit_rate": round(hits / calls, 4) if calls else 0.0,
                "monthly_saving": float(round(saving, 2)),
                # The SDK forgot request shapes that were still open at this
                # limit, so it could only count AT LEAST this many.
                "lower_bound": evicted > 0,
            }
        )
    chosen = next(step for step in ladder if step["ttl_seconds"] == ttl)
    result = {
        "kind": "repeats",
        "calls": calls,
        "unscoped_calls": unscoped_calls,
        "scoped_only": scoped_only,
        "ttl_seconds": ttl,
        "ladder": ladder,
        "monthly_saving": chosen["monthly_saving"],
        "hits": chosen["hits"],
        "hit_rate": chosen["hit_rate"],
    }
    label = TTL_CHOICES[ttl]
    if calls < MIN_CALLS:
        return {
            **result,
            "outcome": None,
            "reason": (
                f"{calls:,} of the {MIN_CALLS:,} calls needed have been counted this month. "
                "The test finishes on its own once there are enough."
            ),
        }
    rate = f"{chosen['hit_rate'] * 100:.1f}%"
    scope = " within a named customer or cache scope" if scoped_only else ""
    if Decimal(str(chosen["monthly_saving"])) >= MIN_SAVING:
        reason = (
            f"A cache keeping answers for up to {label} would have served "
            f"{chosen['hits']:,} of {calls:,} calls{scope} ({rate}), about "
            f"${chosen['monthly_saving']:,.2f} this month at list price."
        )
        if chosen["lower_bound"]:
            reason += " That is a minimum: the SDK had to forget some requests early."
        if not scoped_only and unscoped_calls:
            reason += (
                " Some of those repeats had no customer or cache scope, so whether "
                "their answers could safely be shared is not known."
            )
        return {**result, "outcome": "passed", "reason": reason}
    return {
        **result,
        "outcome": "failed",
        "reason": (
            f"A cache keeping answers for up to {label} would have served only "
            f"{chosen['hits']:,} of {calls:,} calls{scope} ({rate}) — under $1 this month. "
            "At this freshness limit, caching is not worth building."
        ),
    }


def _prefix_rows(conn, feature_id: str, period: dt.date) -> list:
    """The rows the caching detector reads, in its order (see _prefix_opportunity)."""
    return conn.execute(
        """
        SELECT provider, model, fingerprint, call_count, prefix_tokens, cached_count,
               write_calls, cache_windows, prefix_tokens_sum, prefix_tokens_n,
               cache_windows_1h
        FROM usage_signal
        WHERE feature_id = %s AND period = %s AND signal_kind = 'prefix'
        """,
        (feature_id, period),
    ).fetchall()


def _simulate_caching(conn, feature_id: str, period: dt.date, cache_ttl: str) -> dict:
    """The caching saving at each lifetime, priced the way the detector prices it.

    Only providers that sell a 1-hour cache change between the two; the rest are
    the same in both columns, because there is no lifetime to choose there.
    """
    from . import optimize_measured  # imported here: optimize_measured imports this module

    rows = _prefix_rows(conn, feature_id, period)
    calls = sum(int(r[3]) for r in rows)
    choosable = [r for r in rows if pricing.has_1h_cache_tier(r[0])]
    lifetimes = []
    for ttl, label in CACHE_TTLS.items():
        found = optimize_measured._prefix_opportunity(rows, lifetime=ttl)
        lifetimes.append(
            {
                "cache_ttl": ttl,
                "label": label,
                "monthly_saving": found["savings"] if found else 0.0,
                "cache_writes": found["cache_writes"] if found else None,
                "savings_type": found["savings_type"] if found else None,
                "confidence": found["confidence"] if found else None,
            }
        )
    chosen = next(step for step in lifetimes if step["cache_ttl"] == cache_ttl)
    result = {
        "kind": "caching",
        "calls": calls,
        "cache_ttl": cache_ttl,
        "lifetimes": lifetimes,
        "monthly_saving": chosen["monthly_saving"],
    }
    label = _CACHE_ADJ[cache_ttl]
    unknown_long = cache_ttl == "1h" and any(r[10] is None for r in choosable)
    if calls < MIN_CALLS or unknown_long:
        reason = (
            f"{calls:,} of the {MIN_CALLS:,} calls needed have been counted this month."
            if calls < MIN_CALLS
            else (
                "Some of these calls were reported by an SDK too old to count 1-hour cache "
                "writes. Upgrade it (Python or Node SDK 2.4 or later) and the test finishes "
                "on its own as new calls arrive."
            )
        )
        return {**result, "outcome": None, "reason": reason}
    if Decimal(str(chosen["monthly_saving"])) >= MIN_SAVING:
        other = next(step for step in lifetimes if step["cache_ttl"] != cache_ttl)
        comparison = (
            f", against ${other['monthly_saving']:,.2f} with the "
            f"{_CACHE_ADJ[other['cache_ttl']]} cache"
            if choosable
            else ""
        )
        return {
            **result,
            "outcome": "passed",
            "reason": (
                f"With a {label} cache, caching would have saved about "
                f"${chosen['monthly_saving']:,.2f} this month after paying for "
                f"{chosen['cache_writes'] or 0:,} cache writes{comparison}."
            ),
        }
    return {
        **result,
        "outcome": "failed",
        "reason": (
            f"With a {label} cache, writing the cache would cost as much as or more than "
            "the reads would save — the calls are too far apart for it to stay warm."
        ),
    }


def _simulate(conn, lever: str, feature_id: str, period: dt.date, setting: dict) -> dict:
    if lever == "duplicate_calls":
        return _simulate_repeats(
            conn, feature_id, period, setting["ttl_seconds"], setting["scoped_only"]
        )
    return _simulate_caching(conn, feature_id, period, setting["cache_ttl"])


# ---------------------------------------------------------------------------
# Experiments
# ---------------------------------------------------------------------------
_COLUMNS = (
    "id, feature_id, lever, mode, runs_at, status, outcome, outcome_reason, ttl_seconds, "
    "scoped_only, cache_ttl, period, baseline_monthly, baseline_savings_type, "
    "baseline_confidence, result, created_by, created_at, completed_at, cancelled_at, "
    "provider, control_model, candidate_model, loss_margin, min_cases, results_source, "
    "judge_model, test_cost"
)


def _setting(lever: str, ttl_seconds, scoped_only, cache_ttl) -> dict:
    """The customer's choice, checked against what this lever can be tested on."""
    if lever not in SIMULATABLE + OFFLINE:
        raise ExperimentError("This recommendation cannot be tested yet.")
    if lever == "duplicate_calls":
        if ttl_seconds not in TTL_CHOICES:
            raise ExperimentError("Choose how old a cached answer may be.")
        if cache_ttl is not None:
            raise ExperimentError("A cache lifetime applies to prompt caching only.")
        return {"ttl_seconds": int(ttl_seconds), "scoped_only": bool(scoped_only)}
    if cache_ttl not in CACHE_TTLS:
        raise ExperimentError("Choose a cache lifetime.")
    if ttl_seconds is not None or scoped_only is not None:
        raise ExperimentError("A freshness limit applies to repeated requests only.")
    return {"cache_ttl": cache_ttl}


def _setting_of(row: dict) -> dict:
    if row["lever"] == "model_rightsizing":
        return {
            "provider": row["provider"],
            "control_model": row["control_model"],
            "candidate_model": row["candidate_model"],
            "loss_margin": float(row["loss_margin"]),
            "min_cases": row["min_cases"],
        }
    if row["lever"] == "duplicate_calls":
        return {"ttl_seconds": row["ttl_seconds"], "scoped_only": row["scoped_only"]}
    return {"cache_ttl": row["cache_ttl"]}


def setting_label(lever: str, setting: dict) -> str:
    """The choice in words, for the screen and the recommendation card."""
    if lever == "model_rightsizing":
        text = f"{setting['candidate_model']} in place of {setting['control_model']}"
        if offline_tests.relaxed(setting):
            text += f", under a loosened rule ({offline_tests.rule_words(setting)})"
        return text
    if lever == "duplicate_calls":
        text = f"answers reused for up to {TTL_CHOICES[setting['ttl_seconds']]}"
        if setting["scoped_only"]:
            text += ", within a customer or cache scope only"
        return text
    return f"a {_CACHE_ADJ[setting['cache_ttl']]} prompt cache"


def _to_dict(row) -> dict:
    keys = [k.strip() for k in _COLUMNS.split(",")]
    d = dict(zip(keys, row))
    out = {
        "id": str(d["id"]),
        "feature_id": str(d["feature_id"]),
        "lever": d["lever"],
        "mode": d["mode"],
        "runs_at": d["runs_at"],
        "status": d["status"],
        "outcome": d["outcome"],
        "outcome_reason": d["outcome_reason"],
        "setting": _setting_of(d),
        "period": d["period"].isoformat(),
        "baseline": (
            None
            if d["baseline_monthly"] is None
            else {
                "monthly": float(d["baseline_monthly"]),
                "savings_type": d["baseline_savings_type"],
                "confidence": d["baseline_confidence"],
            }
        ),
        "result": d["result"],
        "created_by": d["created_by"],
        "created_at": d["created_at"].isoformat(),
        "completed_at": d["completed_at"].isoformat() if d["completed_at"] else None,
        "cancelled_at": d["cancelled_at"].isoformat() if d["cancelled_at"] else None,
        "results_source": d["results_source"],
        "judge_model": d["judge_model"],
        # What the run cost, priced by Meter. Already on the provider's bill.
        "test_cost": float(d["test_cost"]) if d["test_cost"] is not None else None,
    }
    out["setting_label"] = setting_label(out["lever"], out["setting"])
    return out


def _fetch(conn, experiment_id: str):
    # A malformed id is a missing experiment, not a 500 — and it is checked
    # here rather than caught from the query, because a failed statement would
    # abort the transaction that carries the tenant.
    try:
        uuid.UUID(str(experiment_id))
    except ValueError:
        return None
    return conn.execute(
        f"SELECT {_COLUMNS} FROM experiment WHERE id = %s",  # noqa: S608 - constant
        (experiment_id,),
    ).fetchone()


def _baseline(conn, feature_id: str, period: dt.date, lever: str) -> Optional[dict]:
    """The recommendation as the customer saw it when they started the test."""
    from . import optimize_measured

    found = optimize_measured._feature_opportunities(conn, feature_id, period, annotate=False)
    for o in found["opportunities"]:
        if o["lever"] == lever:
            return o
    return None


def _apply_result(conn, experiment_id: str, outcome_result: dict, period: dt.date) -> None:
    """Record a simulation's answer: complete if it decided, else keep waiting."""
    outcome = outcome_result.pop("outcome")
    reason = outcome_result.pop("reason")
    conn.execute(
        """
        UPDATE experiment
           SET status = CASE WHEN %s::text IS NULL THEN 'waiting_for_data' ELSE 'completed' END,
               outcome = %s,
               outcome_reason = %s,
               period = %s,
               result = %s::jsonb,
               completed_at = CASE WHEN %s::text IS NULL THEN NULL ELSE now() END
         WHERE id = %s
        """,
        (outcome, outcome, reason, period, json.dumps(outcome_result), outcome, experiment_id),
    )


def create(
    tenant_id: str,
    feature_id: str,
    lever: str,
    actor: str,
    *,
    ttl_seconds: Optional[int] = None,
    scoped_only: Optional[bool] = None,
    cache_ttl: Optional[str] = None,
    control_model: Optional[str] = None,
    candidate_model: Optional[str] = None,
    loss_margin: Optional[float] = None,
    min_cases: Optional[int] = None,
) -> Optional[dict]:
    """Start a test of one recommendation. Returns None if the feature is not found.

    A test still waiting on the same recommendation is cancelled, not left
    running beside this one: the customer has changed their mind about the
    setting, and two answers to two questions would sit on one card. A
    cancelled offline run's token stops working with it.

    An offline test (model right-sizing) comes back with `token`: the one
    credential its run holds, shown this once and never again.
    """
    offline = lever in OFFLINE
    if offline:
        if any(v is not None for v in (ttl_seconds, scoped_only, cache_ttl)):
            raise ExperimentError("A freshness limit or cache lifetime does not apply here.")
    else:
        if any(v is not None for v in (control_model, candidate_model, loss_margin, min_cases)):
            raise ExperimentError("Models and a decision rule apply to model right-sizing only.")
        setting = _setting(lever, ttl_seconds, scoped_only, cache_ttl)
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        if conn.execute("SELECT 1 FROM feature WHERE id = %s", (feature_id,)).fetchone() is None:
            return None
        period = dashboard._resolve_period(conn, None)
        if offline:
            try:
                setting = offline_tests.validate_setting(
                    conn, feature_id, period, control_model, candidate_model, loss_margin,
                    min_cases,
                )
            except offline_tests.OfflineTestError as exc:
                raise ExperimentError(str(exc)) from exc
        baseline = _baseline(conn, feature_id, period, lever)
        _cancel_waiting(conn, "feature_id = %s AND lever = %s", (feature_id, lever))
        row = conn.execute(
            """
            INSERT INTO experiment
                (tenant_id, feature_id, lever, mode, status, ttl_seconds, scoped_only,
                 cache_ttl, provider, control_model, candidate_model, loss_margin, min_cases,
                 period, baseline_monthly, baseline_savings_type, baseline_confidence,
                 created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                tenant_id,
                feature_id,
                lever,
                "offline" if offline else "simulate",
                "waiting_for_results" if offline else "waiting_for_data",
                setting.get("ttl_seconds"),
                setting.get("scoped_only"),
                setting.get("cache_ttl"),
                setting.get("provider"),
                setting.get("control_model"),
                setting.get("candidate_model"),
                setting.get("loss_margin"),
                setting.get("min_cases"),
                period,
                baseline["projected_monthly_savings"] if baseline else None,
                baseline["savings_type"] if baseline else None,
                baseline["confidence"] if baseline else None,
                actor,
            ),
        ).fetchone()
        experiment_id = str(row[0])
        if offline:
            token = offline_tests.issue_token(conn, tenant_id, experiment_id)
            return {**_to_dict(_fetch(conn, experiment_id)), "token": token}
        _apply_result(
            conn, experiment_id, _simulate(conn, lever, feature_id, period, setting), period
        )
        return _to_dict(_fetch(conn, experiment_id))


def _cancel_waiting(conn, where: str, params: tuple) -> None:
    """Cancel tests still waiting, and revoke any run token they held.

    `where` is one of this module's own fixed clauses, never caller input.
    """
    waiting = "status IN ('waiting_for_data', 'waiting_for_results')"
    conn.execute(
        f"DELETE FROM experiment_token WHERE experiment_id IN "  # noqa: S608 - constants
        f"(SELECT id FROM experiment WHERE {where} AND {waiting})",
        params,
    )
    conn.execute(
        f"UPDATE experiment SET status = 'cancelled', cancelled_at = now() "  # noqa: S608
        f"WHERE {where} AND {waiting}",
        params,
    )


def get(tenant_id: str, experiment_id: str) -> Optional[dict]:
    """One experiment. A test still waiting for data is checked again first."""
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        row = _fetch(conn, experiment_id)
        if row is None:
            return None
        current = _to_dict(row)
        if current["status"] == "waiting_for_data":
            period = dashboard._resolve_period(conn, None)
            _apply_result(
                conn,
                experiment_id,
                _simulate(
                    conn, current["lever"], current["feature_id"], period, current["setting"]
                ),
                period,
            )
            current = _to_dict(_fetch(conn, experiment_id))
        if current["status"] == "waiting_for_results":
            # When the run's token stops working, so the page can say so.
            expiry = conn.execute(
                "SELECT expires_at FROM experiment_token WHERE experiment_id = %s",
                (experiment_id,),
            ).fetchone()
            current["run_expires_at"] = expiry[0].isoformat() if expiry else None
        name = conn.execute(
            "SELECT name FROM feature WHERE id = %s", (current["feature_id"],)
        ).fetchone()
        current["feature_name"] = name[0] if name else None
        current["history"] = [
            _to_dict(r)
            for r in conn.execute(
                f"""
                SELECT {_COLUMNS} FROM experiment
                 WHERE feature_id = %s AND lever = %s AND id <> %s
                 ORDER BY created_at DESC LIMIT 10
                """,  # noqa: S608 - constant
                (current["feature_id"], current["lever"], experiment_id),
            ).fetchall()
        ]
        return current


def cancel(tenant_id: str, experiment_id: str) -> Optional[dict]:
    """Stop a test that is still waiting. A finished one is history and stays."""
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        if _fetch(conn, experiment_id) is None:
            return None
        _cancel_waiting(conn, "id = %s", (experiment_id,))
        return _to_dict(_fetch(conn, experiment_id))


# ---------------------------------------------------------------------------
# Feeding results back into the recommendations
# ---------------------------------------------------------------------------
def testable(opportunity: dict) -> bool:
    """Whether "Test this" applies to this recommendation."""
    if opportunity["lever"] == "duplicate_calls":
        return True
    if opportunity["lever"] == "model_rightsizing":
        # Where the runner can call the model being replaced.
        return any(
            pricing._vendor_of(t.get("from_model") or "") in offline_tests.RUNNABLE
            for t in opportunity["trail"]
        )
    if opportunity["lever"] == "prompt_caching":
        # Only where there is a lifetime to choose. On a provider that caches
        # automatically the test would only repeat the recommendation.
        return any(pricing.has_1h_cache_tier(t.get("provider")) for t in opportunity["trail"])
    return False


def annotate(conn, feature_id: str, period: dt.date, unified: list) -> list:
    """Layer each recommendation's latest test onto it. Returns the ones whose
    figure changed, so the caller can bound them by the bill again.

    The figure is recomputed for `period` under the customer's setting. When
    this period has too little data to say, the recommendation keeps the
    detector's figure and still shows what the test found when it ran.
    """
    latest = {
        r[2]: _to_dict(r)
        for r in conn.execute(
            f"""
            SELECT DISTINCT ON (lever) {_COLUMNS} FROM experiment
             WHERE feature_id = %s
               AND status IN ('completed', 'waiting_for_data', 'waiting_for_results')
             ORDER BY lever, created_at DESC
            """,  # noqa: S608 - constant
            (feature_id,),
        ).fetchall()
    }
    changed = []
    for o in unified:
        o["testable"] = testable(o)
        o["validation"] = "untested"
        o["test_failed"] = False
        exp = latest.get(o["lever"])
        o["experiment"] = (
            None
            if exp is None
            else {
                "id": exp["id"],
                "status": exp["status"],
                "outcome": exp["outcome"],
                "setting_label": exp["setting_label"],
                "tested_on": (exp["completed_at"] or exp["created_at"])[:10],
            }
        )
        if exp is None or exp["status"] != "completed":
            continue
        if exp["mode"] == "offline":
            o["experiment"]["note"] = exp["outcome_reason"]
            if offline_tests.layer(conn, o, exp, feature_id, period):
                changed.append(o)
            continue
        now = _simulate(conn, o["lever"], feature_id, period, exp["setting"])
        outcome = now["outcome"] or exp["outcome"]
        o["experiment"]["note"] = now["reason"] if now["outcome"] else exp["outcome_reason"]
        if outcome == "failed":
            o["validation"] = "failed"
            o["test_failed"] = True
            continue
        if outcome == "inconclusive":
            o["validation"] = "inconclusive"
            continue
        o["validation"] = "simulated"
        if now["outcome"] != "passed":
            continue  # no data this period to recompute from: keep the detector's figure
        _replace_figure(o, exp, now)
        changed.append(o)
    return changed


def _replace_figure(o: dict, exp: dict, now: dict) -> None:
    from . import optimize_measured

    saving = float(now["monthly_saving"])
    o["projected_monthly_savings"] = round(saving, 2)
    o["projected_annual_savings"] = round(saving * 12, 2)
    if o["lever"] == "prompt_caching":
        chosen = next(s for s in now["lifetimes"] if s["cache_ttl"] == exp["setting"]["cache_ttl"])
        # Priced exactly as the detector prices the default lifetime, so its
        # own rules for "measured" versus "ceiling" carry over unchanged.
        o["savings_type"] = chosen["savings_type"] or o["savings_type"]
        o["confidence"] = chosen["confidence"] or o["confidence"]
        if exp["setting"]["cache_ttl"] == "1h":
            o["fix"] = (
                "Enable prompt caching with the 1-hour lifetime (cache_control with "
                'ttl "1h" on the static block).'
            )
    else:
        label = TTL_CHOICES[exp["setting"]["ttl_seconds"]]
        o["fix"] = f"Cache responses for identical requests for up to {label}" + (
            ", keyed within each customer or cache scope." if exp["setting"]["scoped_only"] else "."
        )
    # The result sentence names the setting itself, so it is not said twice.
    o["evidence"] = f"Simulated — {now['reason']}"
    o["priority_score"] = optimize_measured._priority(
        o["projected_monthly_savings"], o["confidence"], o["engineering_effort"]
    )
