"""Proving a rewrite is not worse, before anyone is told to ship it (PO-4).

A candidate from prompt_optimize.py is a suggestion. This module turns it into a
decision by replaying real inputs through both the old prompt and the new one on
the customer's own model, measuring what each call cost and how long it took,
checking the answers, and judging their quality — then applying one rule.

Four things shape it:

**It runs on the customer's model, not Meter's.** A prompt for a feature on
Anthropic Sonnet has to be tested on Anthropic Sonnet; Meter's own model cannot
stand in for it. That needs a key which can make model calls, which is a
different power from the read-only cost-API credentials Meter already holds, so
it is added separately and stored separately.

**It spends the customer's money, so it asks first.** Every run is estimated
from the tokens the held samples actually used, priced from the price book, and
refused if it would take the organization past its monthly evaluation cap.

**Not worse, or not recommended.** A rewrite is recommended only when nothing
broke, quality held, and enough cases completed. Anything else is
`not_recommended`, with the failing cases visible. The judge may rank; it may
not conclude — the rule is Meter's, and the money is measured.

**The comparison is fair.** Each case is judged twice with the answers swapped,
so an order preference cannot decide a rewrite's fate; disagreement is a tie.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import threading
import time
from decimal import Decimal
from typing import Optional

import httpx

from . import crypto, discovery_llm, pricing, prompt_capture
from .db import app_dsn, connect, tenant_tx

logger = logging.getLogger("meter.prompt_eval")

#: Replayed inputs per run. Enough to see a difference, few enough to be cheap.
DEFAULT_CASES = 30
MAX_CASES = 50
#: Below this, a run can still happen but it cannot recommend: a handful of
#: cases agreeing is not evidence that a prompt is safe to ship.
MIN_CASES_FOR_DECISION = 20
#: A rewrite may lose this share of cases and still be recommended, as long as it
#: wins at least as often. Quality judging is noisy; a single unlucky case is not
#: a regression, and a pattern of them is.
LOSS_MARGIN = 0.10
#: What an organization may spend on evaluations in a calendar month.
MONTHLY_CAP = Decimal("25")
#: One replayed call. Generous for a slow model, bounded so a run cannot hang.
CALL_TIMEOUT = 60.0
JUDGE_TIMEOUT = 45.0
#: Cap on what a replay may generate, when the sample did not say.
DEFAULT_MAX_TOKENS = 1024
PROVIDERS = ("anthropic", "openai")

_REFUSALS = re.compile(
    r"\b(i (can(not|'t)|am unable to|won't)|as an ai\b|i'm sorry, but)", re.IGNORECASE
)

_JUDGE_SYSTEM = """You compare two answers to the same request and say which is better.

Judge only: does the answer do what was asked, correctly and completely, in the
requested form? Ignore length unless it breaks the request. Ignore style.

Reply with JSON only: {"winner": "A" | "B" | "tie", "reason": "<one short sentence>"}"""


class EvalError(ValueError):
    """An evaluation that cannot be started (maps to HTTP 400)."""


# ---------------------------------------------------------------------------
# The key that can replay a prompt
# ---------------------------------------------------------------------------
def set_eval_key(tenant_id: str, provider: str, api_key: str, actor: str) -> list:
    """Store a key that may make model calls. Write-only: no read path returns it."""
    provider = (provider or "").strip().lower()
    if provider not in PROVIDERS:
        raise EvalError(f"Evaluation keys are supported for: {', '.join(PROVIDERS)}.")
    key = (api_key or "").strip()
    if not key or len(key) > 4096:
        raise EvalError("An API key is required.")
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        conn.execute(
            """
            INSERT INTO prompt_eval_key (tenant_id, provider, ciphertext, added_by)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (tenant_id, provider) DO UPDATE
            SET ciphertext = EXCLUDED.ciphertext, added_by = EXCLUDED.added_by, added_at = now()
            """,
            (tenant_id, provider, crypto.encrypt(key), actor),
        )
        prompt_capture._audit(
            conn, tenant_id, "eval_key_added", actor, detail={"provider": provider}
        )
    return eval_keys(tenant_id)


def remove_eval_key(tenant_id: str, provider: str, actor: str) -> list:
    provider = (provider or "").strip().lower()
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        gone = conn.execute(
            "DELETE FROM prompt_eval_key WHERE provider = %s RETURNING provider", (provider,)
        ).rowcount
        if gone:
            prompt_capture._audit(
                conn, tenant_id, "eval_key_removed", actor, detail={"provider": provider}
            )
    return eval_keys(tenant_id)


def eval_keys(tenant_id: str) -> list:
    """Which providers can be replayed. `has_key` is the whole truth about each."""
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        rows = conn.execute(
            "SELECT provider, added_by, added_at FROM prompt_eval_key ORDER BY provider"
        ).fetchall()
    held = {r[0]: r for r in rows}
    return [
        {
            "provider": provider,
            "has_key": provider in held,
            "added_by": held[provider][1] if provider in held else None,
            "added_at": held[provider][2].isoformat() if provider in held else None,
        }
        for provider in PROVIDERS
    ]


def _read_key(conn, provider: str) -> Optional[str]:
    row = conn.execute(
        "SELECT ciphertext FROM prompt_eval_key WHERE provider = %s", (provider,)
    ).fetchone()
    return crypto.decrypt(row[0]) if row else None


# ---------------------------------------------------------------------------
# What a run would cost
# ---------------------------------------------------------------------------
def _month_start(now: dt.datetime) -> dt.datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def estimate(tenant_id: str, template_id: str, now: Optional[dt.datetime] = None) -> dict:
    """What testing this rewrite would cost, and whether it may go ahead.

    Priced from the tokens the held samples really used, because that is the
    traffic being replayed — not an average, and not a guess.
    """
    template_id = prompt_capture._uuid_text(template_id, "template_id")
    now = now or dt.datetime.now(dt.timezone.utc)
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        candidate = conn.execute(
            """
            SELECT id, status FROM prompt_candidate
            WHERE template_id = %s AND status <> 'discarded'
            ORDER BY created_at DESC LIMIT 1
            """,
            (template_id,),
        ).fetchone()
        samples = conn.execute(
            """
            SELECT provider, model, tokens_in, tokens_out FROM prompt_sample
            WHERE template_id = %s ORDER BY received_at DESC LIMIT %s
            """,
            (template_id, MAX_CASES),
        ).fetchall()
        spent = conn.execute(
            "SELECT coalesce(sum(spend), 0) FROM prompt_evaluation WHERE started_at >= %s",
            (_month_start(now),),
        ).fetchone()[0]
        has_key = bool(samples) and _read_key(conn, samples[0][0]) is not None

    if not samples:
        return {"can_run": False, "reason": "no_samples", "cases": 0}
    provider, model = samples[0][0], samples[0][1]
    cases = min(DEFAULT_CASES, len(samples))
    chosen = samples[:cases]
    # Two calls per case: the prompt as it is, and the rewrite.
    cost = sum(pricing.price(model, int(s[2] or 0), int(s[3] or 0), provider) * 2 for s in chosen)
    spent = Decimal(spent or 0)
    priced = pricing.is_priced(model, provider)
    reason = None
    if candidate is None:
        reason = "no_candidate"
    elif candidate[1] == "evaluating":
        reason = "already_running"
    elif not has_key:
        reason = "no_key"
    elif not priced:
        reason = "unpriced_model"
    elif spent + Decimal(cost) > MONTHLY_CAP:
        reason = "over_cap"
    return {
        "can_run": reason is None,
        "reason": reason,
        "candidate_id": str(candidate[0]) if candidate else None,
        "cases": cases,
        "provider": provider,
        "model": model,
        "priced": priced,
        "has_key": has_key,
        "cost_estimate": float(cost),
        "spent_this_month": float(spent),
        "monthly_cap": float(MONTHLY_CAP),
        "min_cases_for_decision": MIN_CASES_FOR_DECISION,
    }


# ---------------------------------------------------------------------------
# Replaying one call
# ---------------------------------------------------------------------------
def _messages_text(messages: list) -> str:
    return "\n\n".join(m.get("text", "") for m in messages if isinstance(m, dict))


def _replay(
    client: httpx.Client,
    provider: str,
    model: str,
    api_key: str,
    template: str,
    messages: list,
    parameters: dict,
) -> dict:
    """One model call: the prompt, the real input, the customer's own account.

    Each provider is called on its own API. Meter captures exactly two call
    shapes, so there are exactly two here, and neither depends on a
    compatibility layer that could change under us.
    """
    max_tokens = int(parameters.get("max_tokens") or DEFAULT_MAX_TOKENS)
    temperature = parameters.get("temperature")
    began = time.perf_counter()
    if provider == "anthropic":
        body: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "system": template,
            "messages": [
                {"role": m.get("role", "user"), "content": m.get("text", "")}
                for m in messages
                if m.get("role") in ("user", "assistant")
            ],
        }
        if isinstance(temperature, (int, float)):
            body["temperature"] = temperature
        resp = client.post(
            "https://api.anthropic.com/v1/messages",
            json=body,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            timeout=CALL_TIMEOUT,
        )
        payload = _ok(resp, api_key)
        text = "".join(
            block.get("text", "")
            for block in payload.get("content", [])
            if block.get("type") == "text"
        )
        usage = payload.get("usage") or {}
        tokens_in = int(usage.get("input_tokens") or 0)
        tokens_out = int(usage.get("output_tokens") or 0)
    else:
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": template}]
            + [
                {"role": m.get("role", "user"), "content": m.get("text", "")}
                for m in messages
                if m.get("role") in ("user", "assistant")
            ],
        }
        if isinstance(temperature, (int, float)):
            body["temperature"] = temperature
        resp = client.post(
            "https://api.openai.com/v1/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=CALL_TIMEOUT,
        )
        payload = _ok(resp, api_key)
        text = (payload.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        usage = payload.get("usage") or {}
        tokens_in = int(usage.get("prompt_tokens") or 0)
        tokens_out = int(usage.get("completion_tokens") or 0)
    return {
        "text": text,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "latency_ms": int((time.perf_counter() - began) * 1000),
        "cost": pricing.price(model, tokens_in, tokens_out, provider),
    }


def _ok(resp: httpx.Response, secret: str) -> dict:
    if resp.status_code >= 400:
        raise EvalError(
            f"The provider refused a replay ({resp.status_code}): "
            + discovery_llm.redact(resp.text[:200], secret)
        )
    return resp.json()


# ---------------------------------------------------------------------------
# Checking and judging one case
# ---------------------------------------------------------------------------
def _json_like(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped.startswith(("{", "[")):
        return False
    try:
        json.loads(stripped)
    except ValueError:
        return False
    return True


def failed_checks(before: str, after: str) -> list:
    """What broke, comparing the rewrite's answer against the original's.

    Deterministic, and about form rather than taste: a judge can be talked out
    of a preference, but invalid JSON where the caller parses JSON is a
    production incident.
    """
    broken = []
    if not (after or "").strip():
        broken.append("empty_answer")
    if _json_like(before) and not _json_like(after):
        broken.append("not_json")
    if _REFUSALS.search(after or "") and not _REFUSALS.search(before or ""):
        broken.append("refused")
    if before and len(after or "") > max(400, len(before) * 3):
        broken.append("much_longer")
    return broken


def _judge_once(client: httpx.Client, config, request: str, first: str, second: str) -> dict:
    body = {
        "model": config.model or discovery_llm.DEFAULT_DISCOVERY_MODEL,
        "temperature": 0,
        "max_tokens": 300,
        "messages": [
            {"role": "system", "content": _JUDGE_SYSTEM},
            {
                "role": "user",
                "content": f"REQUEST:\n{request}\n\nANSWER A:\n{first}\n\nANSWER B:\n{second}",
            },
        ],
    }
    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    resp = client.post(
        f"{config.base_url.rstrip('/')}/chat/completions",
        json=body,
        headers=headers,
        timeout=JUDGE_TIMEOUT,
    )
    if resp.status_code >= 400:
        raise EvalError("The judge could not be reached.")
    text = resp.json()["choices"][0]["message"]["content"]
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    parsed = json.loads(match.group(0)) if match else {}
    winner = str(parsed.get("winner") or "tie").strip().upper()
    return {
        "winner": winner if winner in ("A", "B") else "TIE",
        "reason": str(parsed.get("reason") or "")[:300],
    }


def judge(client: httpx.Client, config, request: str, before: str, after: str) -> dict:
    """Which answer is better, asked both ways round.

    A judge that prefers whichever answer it reads first would decide a rewrite's
    fate by the order of two strings. Asking twice and only counting agreement
    removes that: disagreement is a tie, which costs the rewrite nothing and
    wins it nothing.
    """
    first = _judge_once(client, config, request, before, after)
    second = _judge_once(client, config, request, after, before)
    # In the first ordering A is the original; in the second, A is the rewrite.
    first_says = {"A": "worse", "B": "better", "TIE": "same"}[first["winner"]]
    second_says = {"A": "better", "B": "worse", "TIE": "same"}[second["winner"]]
    verdict = first_says if first_says == second_says else "same"
    return {"verdict": verdict, "reason": first["reason"] or second["reason"]}


def decide(better: int, same: int, worse: int, checks: int, done: int) -> tuple:
    """The rule. Meter's, not the judge's.

    Recommended only when nothing broke, the rewrite did not lose more often
    than it won beyond a small margin, and enough cases finished to mean
    anything. Everything else is not recommended, and says which of the three
    it failed.
    """
    if checks:
        return "not_recommended", f"{checks} case(s) came back broken."
    if done < MIN_CASES_FOR_DECISION:
        return "not_recommended", (
            f"Only {done} case(s) completed; {MIN_CASES_FOR_DECISION} are needed to decide."
        )
    if worse > better + LOSS_MARGIN * done:
        return "not_recommended", f"Worse on {worse} of {done} cases, better on {better}."
    return "recommended", (
        f"No broken answers. Better on {better}, same on {same}, worse on {worse} of {done}."
    )


# ---------------------------------------------------------------------------
# Running an evaluation
# ---------------------------------------------------------------------------
_REASONS = {
    "no_samples": "There are no samples to replay.",
    "no_candidate": "There is no rewrite to test.",
    "already_running": "An evaluation of this rewrite is already running.",
    "no_key": "Add a key for this provider first: replaying a prompt makes real model calls.",
    "unpriced_model": "Meter has no rates for this model, so a saving could not be measured.",
    "over_cap": "This run would take the organization past its monthly evaluation cap.",
}


def start_evaluation(
    tenant_id: str,
    template_id: str,
    actor: str,
    *,
    client: Optional[httpx.Client] = None,
    judge_client: Optional[httpx.Client] = None,
    background: bool = True,
) -> dict:
    """Replay real inputs through both prompts. Returns the run, which polls.

    Everything that can refuse happens here, before a token is spent: consent,
    a rewrite to test, a key that can make the calls, a model Meter can price,
    and room under the monthly cap.
    """
    template_id = prompt_capture._uuid_text(template_id, "template_id")
    if not prompt_capture.consent_status(tenant_id)["capturing"]:
        raise EvalError("Prompt optimization is not turned on, or is paused. Check Settings.")
    plan = estimate(tenant_id, template_id)
    if not plan["can_run"]:
        raise EvalError(_REASONS.get(plan["reason"], "This rewrite cannot be tested yet."))

    judge_config = discovery_llm.active_config(tenant_id) or discovery_llm.env_llm_config()
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        key = prompt_capture._data_key(conn, create=False)
        api_key = _read_key(conn, plan["provider"])
        if key is None or api_key is None:
            raise EvalError("That prompt is no longer readable.")
        template_row = conn.execute(
            "SELECT ciphertext FROM prompt_template WHERE id = %s", (template_id,)
        ).fetchone()
        candidate_row = conn.execute(
            "SELECT ciphertext FROM prompt_candidate WHERE id = %s", (plan["candidate_id"],)
        ).fetchone()
        if template_row is None or candidate_row is None:
            raise EvalError("That rewrite is no longer stored.")
        sample_rows = conn.execute(
            """
            SELECT id, ciphertext FROM prompt_sample WHERE template_id = %s
            ORDER BY received_at DESC LIMIT %s
            """,
            (template_id, plan["cases"]),
        ).fetchall()
        evaluation_id = conn.execute(
            """
            INSERT INTO prompt_evaluation
                (tenant_id, candidate_id, template_id, cases_planned, provider, model,
                 judge_model, started_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (
                tenant_id,
                plan["candidate_id"],
                template_id,
                len(sample_rows),
                plan["provider"],
                plan["model"],
                (judge_config.model if judge_config else ""),
                actor,
            ),
        ).fetchone()[0]
        conn.execute(
            "UPDATE prompt_candidate SET status = 'evaluating' WHERE id = %s",
            (plan["candidate_id"],),
        )
        prompt_capture._audit(
            conn,
            tenant_id,
            "evaluation_started",
            actor,
            detail={
                "evaluation_id": str(evaluation_id),
                "cases": len(sample_rows),
                "provider": plan["provider"],
                "model": plan["model"],
                "estimate": plan["cost_estimate"],
            },
        )
        work = {
            "provider": plan["provider"],
            "model": plan["model"],
            "api_key": api_key,
            "candidate_id": str(plan["candidate_id"]),
            "template": prompt_capture._open(key, template_row[0]),
            "candidate": prompt_capture._open(key, candidate_row[0]),
            "cases": [
                {"sample_id": str(r[0]), **prompt_capture._open(key, r[1])} for r in sample_rows
            ],
        }

    def runner() -> None:
        _run_evaluation(tenant_id, str(evaluation_id), work, judge_config, client, judge_client)

    if background:
        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
    else:
        runner()
    return status(tenant_id, str(evaluation_id))


def _run_evaluation(
    tenant_id: str,
    evaluation_id: str,
    work: dict,
    judge_config,
    client: Optional[httpx.Client],
    judge_client: Optional[httpx.Client],
) -> None:
    """Replay every case, store what came back, then decide. Never raises."""
    owns = client is None
    client = client or httpx.Client()
    judge_client = judge_client or client
    totals = {"better": 0, "same": 0, "worse": 0, "checks": 0, "done": 0}
    sums = {k: 0 for k in ("tin_b", "tin_a", "tout_b", "tout_a", "lat_b", "lat_a")}
    money = {"before": Decimal("0"), "after": Decimal("0")}
    failure = ""
    try:
        for case in work["cases"]:
            messages = case.get("input") or []
            parameters = case.get("parameters") or {}
            before = _replay(
                client,
                work["provider"],
                work["model"],
                work["api_key"],
                work["template"],
                messages,
                parameters,
            )
            after = _replay(
                client,
                work["provider"],
                work["model"],
                work["api_key"],
                work["candidate"],
                messages,
                parameters,
            )
            broken = failed_checks(before["text"], after["text"])
            verdict, reason = "unjudged", ""
            if judge_config is not None and not broken:
                try:
                    judged = judge(
                        judge_client,
                        judge_config,
                        _messages_text(messages),
                        before["text"],
                        after["text"],
                    )
                    verdict, reason = judged["verdict"], judged["reason"]
                except Exception:  # a judge that cannot answer costs a verdict, not the run
                    verdict, reason = "unjudged", ""
            totals["done"] += 1
            totals["checks"] += 1 if broken else 0
            if verdict in totals:
                totals[verdict] += 1
            sums["tin_b"] += before["tokens_in"]
            sums["tin_a"] += after["tokens_in"]
            sums["tout_b"] += before["tokens_out"]
            sums["tout_a"] += after["tokens_out"]
            sums["lat_b"] += before["latency_ms"]
            sums["lat_a"] += after["latency_ms"]
            money["before"] += before["cost"]
            money["after"] += after["cost"]
            _store_case(
                tenant_id, evaluation_id, case, before, after, verdict, reason, broken, totals
            )
    except Exception as exc:
        failure = discovery_llm.redact(str(exc)[:300], work["api_key"])
        logger.warning("evaluation %s failed", evaluation_id)
    finally:
        if owns:
            client.close()
        _finish(tenant_id, evaluation_id, work, totals, sums, money, failure)


def _store_case(
    tenant_id: str,
    evaluation_id: str,
    case: dict,
    before: dict,
    after: dict,
    verdict: str,
    reason: str,
    broken: list,
    totals: dict,
) -> None:
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        key = prompt_capture._data_key(conn, create=False)
        if key is None:
            return  # consent withdrawn mid-run: nothing more may be written
        conn.execute(
            """
            INSERT INTO prompt_evaluation_case
                (tenant_id, evaluation_id, sample_id, before_cipher, after_cipher,
                 tokens_in_before, tokens_in_after, tokens_out_before, tokens_out_after,
                 latency_before_ms, latency_after_ms, cost_before, cost_after,
                 verdict, verdict_cipher, failed_checks)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                tenant_id,
                evaluation_id,
                case.get("sample_id"),
                prompt_capture._seal(key, before["text"]),
                prompt_capture._seal(key, after["text"]),
                before["tokens_in"],
                after["tokens_in"],
                before["tokens_out"],
                after["tokens_out"],
                before["latency_ms"],
                after["latency_ms"],
                before["cost"],
                after["cost"],
                verdict,
                prompt_capture._seal(key, reason),
                json.dumps(broken),
            ),
        )
        conn.execute(
            """
            UPDATE prompt_evaluation
            SET cases_done = %s, better = %s, same = %s, worse = %s, check_failures = %s
            WHERE id = %s
            """,
            (
                totals["done"],
                totals["better"],
                totals["same"],
                totals["worse"],
                totals["checks"],
                evaluation_id,
            ),
        )


def _finish(
    tenant_id: str,
    evaluation_id: str,
    work: dict,
    totals: dict,
    sums: dict,
    money: dict,
    failure: str,
) -> None:
    done = max(totals["done"], 1)
    decision, reason = (None, "")
    if not failure and totals["done"]:
        decision, reason = decide(
            totals["better"], totals["same"], totals["worse"], totals["checks"], totals["done"]
        )
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        conn.execute(
            """
            UPDATE prompt_evaluation
            SET status = %s, decision = %s, decision_reason = %s,
                cost_before = %s, cost_after = %s,
                tokens_in_before = %s, tokens_in_after = %s,
                tokens_out_before = %s, tokens_out_after = %s,
                latency_before_ms = %s, latency_after_ms = %s,
                spend = %s, error = %s, finished_at = now()
            WHERE id = %s
            """,
            (
                "failed" if failure else "completed",
                decision,
                reason,
                money["before"] / done,
                money["after"] / done,
                sums["tin_b"] // done,
                sums["tin_a"] // done,
                sums["tout_b"] // done,
                sums["tout_a"] // done,
                sums["lat_b"] // done,
                sums["lat_a"] // done,
                money["before"] + money["after"],
                failure,
                evaluation_id,
            ),
        )
        conn.execute(
            "UPDATE prompt_candidate SET status = %s WHERE id = %s",
            (decision or "not_evaluated", work["candidate_id"]),
        )
        prompt_capture._audit(
            conn,
            tenant_id,
            "evaluation_finished",
            None,
            detail={
                "evaluation_id": evaluation_id,
                "cases": totals["done"],
                "decision": decision,
                "failed": bool(failure),
            },
        )


# ---------------------------------------------------------------------------
# Reading a run
# ---------------------------------------------------------------------------
def status(
    tenant_id: str,
    evaluation_id: Optional[str] = None,
    template_id: Optional[str] = None,
    now: Optional[dt.datetime] = None,
) -> Optional[dict]:
    """One run: its progress, what it measured, and what it decided. No content."""
    now = now or dt.datetime.now(dt.timezone.utc)
    where, params = (
        ("e.id = %s", [prompt_capture._uuid_text(evaluation_id, "evaluation_id")])
        if evaluation_id
        else ("e.template_id = %s", [prompt_capture._uuid_text(template_id or "", "template_id")])
    )
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        row = conn.execute(
            f"""
            SELECT e.id, e.candidate_id, e.template_id, e.status, e.decision, e.decision_reason,
                   e.cases_planned, e.cases_done, e.better, e.same, e.worse, e.check_failures,
                   e.cost_before, e.cost_after, e.tokens_in_before, e.tokens_in_after,
                   e.tokens_out_before, e.tokens_out_after, e.latency_before_ms,
                   e.latency_after_ms, e.spend, e.provider, e.model, e.judge_model, e.error,
                   e.started_by, e.started_at, e.finished_at, t.prompt_id, t.prompt_version
            FROM prompt_evaluation e JOIN prompt_template t ON t.id = e.template_id
            WHERE {where}
            ORDER BY e.started_at DESC LIMIT 1
            """,  # noqa: S608 - `where` is one of two fixed strings; values are parameters
            params,
        ).fetchone()
        if row is None:
            return None
        cases = conn.execute(
            """
            SELECT id, verdict, failed_checks, tokens_in_before, tokens_in_after,
                   tokens_out_before, tokens_out_after, cost_before, cost_after
            FROM prompt_evaluation_case WHERE evaluation_id = %s ORDER BY created_at
            """,
            (row[0],),
        ).fetchall()
        calls = conn.execute(
            """
            SELECT count(*) FROM ai_span
            WHERE prompt_id = %s AND prompt_version = %s AND occurred_at >= %s
            """,
            (row[28], row[29], now - dt.timedelta(days=30)),
        ).fetchone()[0]

    saved_per_call = float(row[12]) - float(row[13])
    return {
        "evaluation_id": str(row[0]),
        "candidate_id": str(row[1]),
        "template_id": str(row[2]),
        "status": row[3],
        "decision": row[4],
        "decision_reason": row[5],
        "cases_planned": row[6],
        "cases_done": row[7],
        "better": row[8],
        "same": row[9],
        "worse": row[10],
        "check_failures": row[11],
        "cost_before": float(row[12]),
        "cost_after": float(row[13]),
        "tokens_in_before": row[14],
        "tokens_in_after": row[15],
        "tokens_out_before": row[16],
        "tokens_out_after": row[17],
        "latency_before_ms": row[18],
        "latency_after_ms": row[19],
        "spend": float(row[20]),
        "provider": row[21],
        "model": row[22],
        "judge_model": row[23],
        "error": row[24],
        "started_by": row[25],
        "started_at": row[26].isoformat(),
        "finished_at": row[27].isoformat() if row[27] else None,
        # Measured per call on the replay, carried to the traffic that prompt
        # version really sees. A ceiling: it holds only while the traffic looks
        # like the sample it was measured on.
        "calls_30d": int(calls),
        "projected_monthly_saving": round(saved_per_call * int(calls), 2)
        if row[4] == "recommended" and saved_per_call > 0
        else 0.0,
        "cases": [
            {
                "case_id": str(c[0]),
                "verdict": c[1],
                "failed_checks": c[2],
                "tokens_in_before": c[3],
                "tokens_in_after": c[4],
                "tokens_out_before": c[5],
                "tokens_out_after": c[6],
                "cost_before": float(c[7]),
                "cost_after": float(c[8]),
            }
            for c in cases
        ],
    }


def case_content(tenant_id: str, evaluation_id: str, actor: str) -> Optional[list]:
    """What each replayed case actually answered, decrypted, with an audit row."""
    evaluation_id = prompt_capture._uuid_text(evaluation_id, "evaluation_id")
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        run = conn.execute(
            "SELECT template_id FROM prompt_evaluation WHERE id = %s", (evaluation_id,)
        ).fetchone()
        if run is None:
            return None
        key = prompt_capture._data_key(conn, create=False)
        if key is None:
            return None
        rows = conn.execute(
            """
            SELECT id, before_cipher, after_cipher, verdict, verdict_cipher, failed_checks
            FROM prompt_evaluation_case WHERE evaluation_id = %s ORDER BY created_at
            """,
            (evaluation_id,),
        ).fetchall()
        prompt_capture._audit(
            conn,
            tenant_id,
            "evaluation_viewed",
            actor,
            detail={"evaluation_id": evaluation_id, "cases": len(rows)},
        )
    return [
        {
            "case_id": str(r[0]),
            "before": prompt_capture._open(key, r[1]),
            "after": prompt_capture._open(key, r[2]),
            "verdict": r[3],
            "reason": prompt_capture._open(key, r[4]),
            "failed_checks": r[5],
        }
        for r in rows
    ]
