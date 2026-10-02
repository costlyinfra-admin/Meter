"""Model tests Meter runs itself, on calls it was allowed to capture (EX-4).

The default home for a model test is the customer's machine (offline_tests.py):
their cases, their keys, numbers back. But an organization that has already
agreed to let Meter capture a feature's prompts (Prompt Optimization) has
given Meter both halves of a test — real inputs, and the system prompt they
ran with — so Meter can run the test itself, with no runner to install:

- replay captured calls to the current model on the current model and on the
  cheaper one, with the organization's own evaluation key (the same write-only
  key a prompt rewrite is tested with);
- check both answers the same deterministic way, and judge them with the model
  the organization's consent named, asked both ways round;
- keep the same numbers-only rows a customer-side run sends (experiment_case),
  and decide by the same rule. The answers themselves are not kept.

It spends the customer's money, so it is estimated first from the tokens the
captured calls used, held to the same monthly cap as prompt-rewrite tests, and
written to the audit log. It runs as a job (jobs.py), so a restart does not
lose it. A run that stops part-way decides nothing and says why.

What it does not do: test a model on a different provider. A captured call
records which client made it, not which host served it, and model names differ
between hosts, so Meter cannot yet replay one faithfully elsewhere.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from decimal import Decimal
from typing import Optional

import httpx

from . import dashboard, discovery_llm, jobs, offline_tests, pricing, prompt_capture, prompt_eval
from .db import app_dsn, connect, tenant_tx

logger = logging.getLogger("meter.hosted_tests")

#: Captured calls replayed in one test. Each is two model calls and two judge
#: calls; this keeps a test inside a few dollars on most models.
MAX_CASES = 100
#: Answers that mean the key cannot make the calls at all. The run stops,
#: rather than counting every case as the cheaper model's failure.
KEY_REFUSED = (401, 403)
#: What the judge's name may look like when it is stored.
_MODEL_NAME = re.compile(r"^[A-Za-z0-9._:/@-]{1,120}$")


class HostedTestError(ValueError):
    """A hosted test that cannot start, in words (maps to HTTP 400)."""


# ---------------------------------------------------------------------------
# Whether Meter can run this test, and what it would cost
# ---------------------------------------------------------------------------
def _samples(conn, feature_id: str, provider: str, model: str) -> list:
    """(id, tokens_in, tokens_out) of the newest captured calls to `model`."""
    return conn.execute(
        """
        SELECT id, tokens_in, tokens_out FROM prompt_sample
         WHERE feature_id = %s AND provider = %s AND model = %s
         ORDER BY received_at DESC LIMIT %s
        """,
        (feature_id, provider, model, MAX_CASES),
    ).fetchall()


def _estimate(samples: list, provider: str, control: str, candidate: str) -> Decimal:
    """Both models on every case, assuming the cheaper one writes as much."""
    return sum(
        (
            pricing.price(control, int(tin or 0), int(tout or 0), provider)
            + pricing.price(candidate, int(tin or 0), int(tout or 0), provider)
            for _id, tin, tout in samples
        ),
        Decimal("0"),
    )


def _consent(tenant_id: str) -> dict:
    """Where prompt-capture consent stands. Its own connection: read it before
    opening the caller's transaction."""
    return prompt_capture.consent_status(tenant_id)


def options(conn, tenant_id: str, feature_id: str, controls: list, consent: dict) -> dict:
    """What a hosted test of each model would replay and cost, in `conn`.

    `consent` is _consent(tenant_id), read by the caller before its
    transaction. A test needs capture open under terms that name this use
    (prompt_capture.MODEL_TESTS_SINCE): agreeing to capture was not agreeing
    to this, until the terms said so.
    """
    enabled = (
        conn.execute(
            "SELECT 1 FROM prompt_capture_feature WHERE feature_id = %s", (feature_id,)
        ).fetchone()
        is not None
    )
    keys = {p: prompt_eval._read_key(conn, p) is not None for p in prompt_eval.PROVIDERS}
    by_control = {}
    for c in controls:
        samples = _samples(conn, feature_id, c["provider"], c["model"]) if enabled else []
        reason = None
        if not consent["capturing"]:
            reason = "not_capturing"
        elif not consent["model_tests"]:
            reason = "terms_not_agreed"
        elif not enabled:
            reason = "feature_not_enabled"
        elif not samples:
            reason = "no_samples"
        elif not keys.get(c["provider"]):
            reason = "no_key"
        by_control[c["model"]] = {
            "reason": reason,
            "cases": len(samples),
            "has_key": bool(keys.get(c["provider"])),
            "estimates": {
                cand["model"]: round(
                    float(_estimate(samples, c["provider"], c["model"], cand["model"])), 4
                )
                for cand in c["candidates"]
            },
        }
    return {
        "capturing": bool(consent["capturing"]),
        "model_tests": bool(consent["model_tests"]),
        "feature_enabled": enabled,
        "max_cases": MAX_CASES,
        "spent_this_month": round(float(jobs.spent_this_month(conn)), 4),
        "monthly_cap": float(prompt_eval.MONTHLY_CAP),
        "by_control": by_control,
    }


_REASONS = {
    "not_capturing": (
        "Meter can run this test only on calls it was allowed to capture. Turn on prompt "
        "optimization in Settings, or run the test on your side."
    ),
    "terms_not_agreed": (
        "Prompt optimization's terms now cover model tests run by Meter. Agree to the updated "
        "terms in Settings to let Meter run this one, or run it on your side."
    ),
    "feature_not_enabled": (
        "Prompt capture is not turned on for this feature, so Meter holds no calls to replay."
    ),
    "no_samples": "Meter holds no captured calls to {model} on this feature yet.",
    "no_key": (
        "Add an evaluation key for {provider} in Settings → Prompt optimization: the test "
        "makes real model calls on your account."
    ),
}


def plan(conn, tenant_id: str, feature_id: str, setting: dict, consent: dict) -> dict:
    """Check a hosted test can run as set, and say what it would replay."""
    control, candidate = setting["control_model"], setting["candidate_model"]
    provider = setting["provider"]
    opts = options(
        conn,
        tenant_id,
        feature_id,
        [{"model": control, "provider": provider, "candidates": [{"model": candidate}]}],
        consent,
    )
    mine = opts["by_control"][control]
    if mine["reason"]:
        raise HostedTestError(
            _REASONS[mine["reason"]].format(model=control, provider=provider.title())
        )
    if mine["cases"] < setting["min_cases"]:
        raise HostedTestError(
            f"Meter holds {mine['cases']} captured calls to {control} on this feature; this "
            f"test needs {setting['min_cases']}. Lower the number of cases needed, wait for "
            "more traffic, or run the test on your side."
        )
    if not (pricing.is_priced(control, provider) and pricing.is_priced(candidate, provider)):
        raise HostedTestError(
            "Meter has no rates for one of these models, so it cannot price the test."
        )
    cost = Decimal(str(mine["estimates"][candidate]))
    if Decimal(str(opts["spent_this_month"])) + cost > prompt_eval.MONTHLY_CAP:
        raise HostedTestError(
            f"This test would take the organization past its monthly cap of "
            f"${prompt_eval.MONTHLY_CAP:,.0f} for tests Meter runs (about ${cost:,.2f}, with "
            f"${opts['spent_this_month']:,.2f} spent so far this month)."
        )
    sample_ids = [str(s[0]) for s in _samples(conn, feature_id, provider, control)]
    return {"sample_ids": sample_ids, "estimate": cost}


def begin(
    conn, tenant_id: str, experiment_id: str, setting: dict, planned: dict, actor: str
) -> str:
    """Write the run down and log who started it. Returns the job to start."""
    job_id = jobs.enqueue(
        conn, tenant_id, "hosted_test", experiment_id, planned["sample_ids"], planned["estimate"]
    )
    prompt_capture._audit(
        conn,
        tenant_id,
        "hosted_test_started",
        actor,
        detail={
            "experiment_id": experiment_id,
            "cases": len(planned["sample_ids"]),
            "provider": setting["provider"],
            "control_model": setting["control_model"],
            "candidate_model": setting["candidate_model"],
            "estimate": float(planned["estimate"]),
        },
    )
    return job_id


# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------
def case_hash(experiment_id: str, sample_id: str) -> str:
    """A case's name: its sample, hashed with the test, so a resumed run knows
    what it has done and no two tests' cases can be matched up."""
    return hashlib.sha256(f"{experiment_id}:{sample_id}".encode()).hexdigest()


def _load(tenant_id: str, job: dict) -> Optional[dict]:
    if not _consent(tenant_id)["model_tests"]:
        raise HostedTestError("Prompt optimization was turned off or paused during the run.")
    experiment_id = job["experiment_id"]
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        row = conn.execute(
            """
            SELECT provider, control_model, candidate_model FROM experiment
             WHERE id = %s AND status = 'running' AND runs_at = 'meter'
            """,
            (experiment_id,),
        ).fetchone()
        if row is None:
            return None
        provider, control, candidate = row
        key = prompt_capture._data_key(conn, create=False)
        api_key = prompt_eval._read_key(conn, provider)
        if key is None or api_key is None:
            raise HostedTestError("The captured calls or the evaluation key were removed.")
        done = {
            r[0]
            for r in conn.execute(
                "SELECT case_hash FROM experiment_case WHERE experiment_id = %s",
                (experiment_id,),
            ).fetchall()
        }
        remaining = [s for s in job["sample_ids"] if case_hash(experiment_id, s) not in done]
        held = {
            str(r[0]): (r[1], r[2])
            for r in conn.execute(
                """
                SELECT s.id, s.ciphertext, t.ciphertext
                  FROM prompt_sample s JOIN prompt_template t ON t.id = s.template_id
                 WHERE s.id = ANY(%s::uuid[])
                """,
                (remaining,),
            ).fetchall()
        }
        cases = []
        for s in remaining:
            if s not in held:
                continue  # purged since the run was planned: skipped, not invented
            sample = prompt_capture._open(key, held[s][0])
            cases.append(
                {
                    "sample_id": s,
                    "system": prompt_capture._open(key, held[s][1]),
                    "input": sample.get("input") or [],
                    "parameters": sample.get("parameters") or {},
                }
            )
    return {
        "provider": provider,
        "control": control,
        "candidate": candidate,
        "api_key": api_key,
        "cases": cases,
    }


def _call(client, provider: str, model: str, api_key: str, case: dict) -> dict:
    """One replay. A provider error is the call's failure, not the run's —
    except a refused key, which would fail every call alike."""
    began = time.perf_counter()
    try:
        out = prompt_eval._replay(
            client, provider, model, api_key, case["system"], case["input"], case["parameters"]
        )
        return {**out, "error": False}
    except prompt_eval.ReplayRefused as exc:
        if exc.status in KEY_REFUSED:
            raise
    except httpx.HTTPError:
        pass
    return {
        "text": "",
        "tokens_in": 0,
        "tokens_out": 0,
        "latency_ms": int((time.perf_counter() - began) * 1000),
        "error": True,
    }


def _numbers(call: dict) -> dict:
    return {k: call[k] for k in ("tokens_in", "tokens_out", "latency_ms", "error")}


def _judge_name(config) -> Optional[str]:
    name = getattr(config, "model", None) or None
    return name if name and _MODEL_NAME.match(name) else None


def _run_job(tenant_id: str, job: dict, client, judge_client, keep_going) -> str:
    """Replay every case not yet kept, then decide. Never raises (jobs.py)."""
    experiment_id = job["experiment_id"]
    owns = client is None
    client = client or httpx.Client()
    judge_client = judge_client or client
    secret = ""
    judge_config = prompt_eval._judge_config(tenant_id)
    try:
        work = _load(tenant_id, job)
        if work is None:
            return "failed"  # cancelled, or finished, while nobody held it
        secret = work["api_key"]
        for case in work["cases"]:
            control = _call(client, work["provider"], work["control"], secret, case)
            candidate = _call(client, work["provider"], work["candidate"], secret, case)
            checks, verdict = 0, "unjudged"
            if not (control["error"] or candidate["error"]):
                checks = len(prompt_eval.failed_checks(control["text"], candidate["text"]))
                if judge_config is not None and not checks:
                    try:
                        verdict = prompt_eval.judge(
                            judge_client,
                            judge_config,
                            prompt_eval._messages_text(case["input"]),
                            control["text"],
                            candidate["text"],
                        )["verdict"]
                    except Exception:  # a judge that cannot answer costs a verdict, not the run
                        verdict = "unjudged"
            row = {
                "case": case_hash(experiment_id, case["sample_id"]),
                "control": _numbers(control),
                "candidate": _numbers(candidate),
                "check_failures": checks,
                "verdict": verdict,
            }
            _keep(tenant_id, experiment_id, work, row)
            if not keep_going():
                return "paused"
    except Exception as exc:
        logger.warning("hosted test %s stopped", experiment_id)
        _conclude(
            tenant_id,
            experiment_id,
            judge_config,
            stopped=discovery_llm.redact(str(exc)[:300], secret),
        )
        return "failed"
    finally:
        if owns:
            client.close()
    _conclude(tenant_id, experiment_id, judge_config)
    return "done"


def _keep(tenant_id: str, experiment_id: str, work: dict, row: dict) -> None:
    """Store one case and count its cost the moment it is spent."""
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        if offline_tests.store_cases(conn, tenant_id, experiment_id, [row]):
            cost = offline_tests.call_cost(
                work["provider"], work["control"], row["control"]
            ) + offline_tests.call_cost(work["provider"], work["candidate"], row["candidate"])
            conn.execute(
                "UPDATE experiment SET test_cost = coalesce(test_cost, 0) + %s WHERE id = %s",
                (cost, experiment_id),
            )


def _stop_job(tenant_id: str, job: dict, why: str) -> None:
    _conclude(tenant_id, job["experiment_id"], prompt_eval._judge_config(tenant_id), stopped=why)


def _conclude(tenant_id: str, experiment_id: str, judge_config, stopped: Optional[str] = None):
    """Decide from every kept case, whichever process kept them."""
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        row = conn.execute(
            """
            SELECT feature_id, control_model FROM experiment
             WHERE id = %s AND status = 'running' AND runs_at = 'meter' FOR UPDATE
            """,
            (experiment_id,),
        ).fetchone()
        if row is None:
            return  # cancelled, or already decided
        cases = offline_tests.stored_cases(conn, experiment_id)
        period = dashboard._resolve_period(conn, None)
        done = offline_tests.conclude(
            conn,
            experiment_id,
            cases,
            source="meter",
            judge_model=_judge_name(judge_config),
            judge_usage=None,
            period=period,
            spend=offline_tests.control_spend(conn, str(row[0]), period, row[1]),
            stopped=stopped,
        )
        prompt_capture._audit(
            conn,
            tenant_id,
            "hosted_test_finished",
            None,
            detail={
                "experiment_id": experiment_id,
                "cases": len(cases),
                "outcome": done["outcome"],
                "stopped": bool(stopped),
            },
        )
