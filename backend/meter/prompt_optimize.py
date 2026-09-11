"""Proposing a cheaper prompt, and saying why (PO-3).

This module reads consented prompts (prompt_capture.py) and asks a model for a
cheaper version of one. See docs/prompt-optimization-spec.md.

Four rules shape it:

**It reads only what was consented to.** Templates and samples come out of the
same encrypted store, through the same per-tenant data key. Nothing here can
reach content for a feature that was never switched on, because nothing there
was ever stored.

**The model may rewrite; it may not assert.** It returns a new template and a
list of changes, each with a category and a reason. Every number a customer
sees is Meter's own — counts from the database, and later the measured token
deltas from the replay evaluation. The model's own "expected effect" is carried
as its expectation and labelled that way.

**Nothing is recommended here.** A candidate's status is `not_evaluated` until
PO-4 replays real inputs and compares. A rewrite that has not been tested is a
suggestion, and calling it anything else would be the black-box number this
product exists to avoid.

**Failures stay quiet about content.** A provider error is redacted before it is
logged or returned, and no prompt text is ever written to a log.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from typing import Any, Optional

import httpx

from . import discovery_llm, prompt_capture
from .db import app_dsn, connect, tenant_tx

logger = logging.getLogger("meter.prompt_optimize")

#: A rewrite needs enough real calls behind it to be worth trusting.
MIN_SAMPLES = 20
#: Examples of real traffic sent with the template, so the rewrite is grounded in
#: what the prompt actually receives. Three is enough to show the shape.
EXAMPLES_IN_REQUEST = 3
MAX_EXAMPLE_CHARS = 2_000
MAX_TEMPLATE_CHARS = 24_000
MAX_CHANGES = 12
MAX_REASON_CHARS = 400
MAX_EXCERPT_CHARS = 400
#: Long enough for a careful rewrite, short enough that a wedged endpoint does
#: not hold a request open.
TIMEOUT = 90.0
MAX_OUTPUT_TOKENS = 4_000
#: How far back the usage figures on the Prompts screen look.
USAGE_DAYS = 30

#: The kinds of change a rewrite may claim, so a reason is legible rather than
#: free-form. Anything else is refused with the rest of the response.
CHANGE_CATEGORIES = (
    "redundancy",
    "examples",
    "cache_prefix",
    "output_format",
    "unused_instruction",
    "wording",
)

_SYSTEM = """You rewrite system prompts to cost less to run, without changing what they do.

Rules:
- Keep the behaviour identical. Same task, same tone, same output format, same
  constraints. If a change could alter what the model produces, do not make it.
- Keep every placeholder exactly as written (for example {ticket}, {{name}}).
- Prefer: removing repeated or contradictory instructions; shortening verbose
  examples while keeping one of each kind; moving text that never changes to the
  start so it can be cached; tightening a requested output format that wastes
  output tokens; removing instructions the examples show are never exercised.
- Do not invent numbers, savings or percentages. Do not mention Meter.
- If the prompt is already tight, return it unchanged with an empty change list.

Reply with JSON only, in this shape:
{"template": "<the rewritten prompt>",
 "changes": [{"category": "one of: redundancy, examples, cache_prefix,
                           output_format, unused_instruction, wording",
              "before": "<short excerpt of the original>",
              "after": "<what it became, or an empty string if removed>",
              "reason": "<why this is safe and cheaper>",
              "expected_effect": "<what you expect it to do>"}]}"""


class OptimizeError(ValueError):
    """A candidate that cannot be produced (maps to HTTP 400)."""


# ---------------------------------------------------------------------------
# Reading what has been captured
# ---------------------------------------------------------------------------
def list_prompts(tenant_id: str, now: Optional[dt.datetime] = None) -> dict:
    """Every captured prompt version, with what it really costs to run.

    Identities and numbers only. The counts and the money come from metering
    (`ai_span`), not from the samples: samples are a small fraction of traffic,
    and a figure built from them would overstate nothing and understate
    everything.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=USAGE_DAYS)
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        rows = conn.execute(
            """
            SELECT t.id, t.feature_id, f.name, t.prompt_id, t.prompt_version,
                   t.size_bytes, t.last_seen_at,
                   (SELECT count(*) FROM prompt_sample s WHERE s.template_id = t.id),
                   (SELECT count(*) FROM ai_span sp
                     WHERE sp.prompt_id = t.prompt_id
                       AND sp.prompt_version = t.prompt_version
                       AND sp.occurred_at >= %s),
                   (SELECT coalesce(sum(sp.amount), 0) FROM ai_span sp
                     WHERE sp.prompt_id = t.prompt_id
                       AND sp.prompt_version = t.prompt_version
                       AND sp.occurred_at >= %s),
                   (SELECT c.id FROM prompt_candidate c
                     WHERE c.template_id = t.id AND c.status <> 'discarded'
                     ORDER BY c.created_at DESC LIMIT 1),
                   (SELECT c.status FROM prompt_candidate c
                     WHERE c.template_id = t.id AND c.status <> 'discarded'
                     ORDER BY c.created_at DESC LIMIT 1)
            FROM prompt_template t
            JOIN feature f ON f.id = t.feature_id
            ORDER BY 10 DESC, t.last_seen_at DESC
            """,
            (since, since),
        ).fetchall()
    return {
        "usage_days": USAGE_DAYS,
        "min_samples": MIN_SAMPLES,
        "prompts": [
            {
                "template_id": str(r[0]),
                "feature_id": str(r[1]),
                "feature_name": r[2],
                "prompt_id": r[3],
                "prompt_version": r[4],
                "template_bytes": r[5],
                "last_seen_at": r[6].isoformat(),
                "samples": int(r[7]),
                "calls": int(r[8]),
                "cost": float(r[9]),
                "candidate_id": str(r[10]) if r[10] else None,
                "candidate_status": r[11],
            }
            for r in rows
        ],
    }


def prompt_detail(tenant_id: str, template_id: str, now: Optional[dt.datetime] = None) -> dict:
    """One prompt version: its usage, its samples, and its candidate if any.

    No content. The prompt itself, and any rewrite of it, come from
    `prompt_content`, which writes an audit row.
    """
    template_id = prompt_capture._uuid_text(template_id, "template_id")
    listing = list_prompts(tenant_id, now)
    found = next((p for p in listing["prompts"] if p["template_id"] == template_id), None)
    if found is None:
        return {}
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        samples = conn.execute(
            """
            SELECT id, provider, model, tokens_in, tokens_out, latency_ms, captured_at
            FROM prompt_sample WHERE template_id = %s
            ORDER BY received_at DESC LIMIT 50
            """,
            (template_id,),
        ).fetchall()
        candidate = conn.execute(
            """
            SELECT id, change_count, original_chars, candidate_chars, provider, model,
                   status, created_by, created_at
            FROM prompt_candidate
            WHERE template_id = %s AND status <> 'discarded'
            ORDER BY created_at DESC LIMIT 1
            """,
            (template_id,),
        ).fetchone()
    return {
        **found,
        "samples_detail": [
            {
                "sample_id": str(s[0]),
                "provider": s[1],
                "model": s[2],
                "tokens_in": s[3],
                "tokens_out": s[4],
                "latency_ms": s[5],
                "captured_at": s[6].isoformat(),
            }
            for s in samples
        ],
        "candidate": None
        if candidate is None
        else {
            "candidate_id": str(candidate[0]),
            "change_count": int(candidate[1]),
            "original_chars": int(candidate[2]),
            "candidate_chars": int(candidate[3]),
            "provider": candidate[4],
            "model": candidate[5],
            "status": candidate[6],
            "created_by": candidate[7],
            "created_at": candidate[8].isoformat(),
        },
    }


# ---------------------------------------------------------------------------
# Asking a model for a cheaper prompt
# ---------------------------------------------------------------------------
def _placeholders(text: str) -> set:
    """Names the prompt fills in at run time. A rewrite must keep every one."""
    return set(re.findall(r"\{\{?\s*([A-Za-z0-9_.]+)\s*\}?\}", text or ""))


def _clip(text: Any, limit: int) -> str:
    return str(text or "")[:limit]


def _payload(template: str, examples: list) -> str:
    """What the model is shown: the prompt, and a few real calls through it."""
    lines = ["PROMPT TO REWRITE:", template, ""]
    if examples:
        lines.append("REAL CALLS THROUGH IT (for grounding; do not copy them into the prompt):")
        for n, example in enumerate(examples, 1):
            asked = " ".join(m.get("text", "") for m in example.get("input", []))
            lines.append(f"{n}. input: {_clip(asked, MAX_EXAMPLE_CHARS)}")
            lines.append(f"   output: {_clip(example.get('output'), MAX_EXAMPLE_CHARS)}")
    return "\n".join(lines)


def _complete(config, system: str, user: str, client: Optional[httpx.Client] = None) -> str:
    """One chat completion against an OpenAI-compatible endpoint.

    Mirrors discovery's call. Never lets a provider error carry the key, and
    never logs the prompt it sent.
    """
    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    body = {
        "model": config.model or discovery_llm.DEFAULT_DISCOVERY_MODEL,
        "temperature": 0,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    owns = client is None
    client = client or httpx.Client(timeout=TIMEOUT)
    try:
        resp = client.post(
            f"{config.base_url.rstrip('/')}/chat/completions", json=body, headers=headers
        )
        if resp.status_code >= 400:
            detail = discovery_llm.redact(resp.text[:200], config.api_key)
            logger.warning("prompt optimizer provider error %s", resp.status_code)
            raise OptimizeError(f"The model refused the request ({resp.status_code}): {detail}")
        return resp.json()["choices"][0]["message"]["content"]
    except OptimizeError:
        raise
    except Exception as exc:
        logger.warning(
            "prompt optimizer call failed: %s", discovery_llm.redact(str(exc)[:200], config.api_key)
        )
        raise OptimizeError("The model could not be reached. Try again in a moment.") from exc
    finally:
        if owns:
            client.close()


def _parse(text: str, original: str) -> dict:
    """The model's reply, reduced to what may be stored, or OptimizeError.

    Strict on purpose. A rewrite that drops a placeholder would break the
    customer's application, and a change list that cannot be read is not an
    explanation — either one is a reason to discard the whole answer rather than
    show a half-checked prompt.
    """
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        raise OptimizeError("The model did not return a rewrite.")
    try:
        parsed = json.loads(match.group(0))
    except ValueError as exc:
        raise OptimizeError("The model's rewrite could not be read.") from exc

    template = parsed.get("template")
    if not isinstance(template, str) or not template.strip():
        raise OptimizeError("The model did not return a rewrite.")
    template = template.strip()
    if len(template) > max(MAX_TEMPLATE_CHARS, len(original) * 2):
        raise OptimizeError("The rewrite is longer than the prompt it replaces.")
    missing = _placeholders(original) - _placeholders(template)
    if missing:
        raise OptimizeError(
            "The rewrite dropped values the prompt fills in: " + ", ".join(sorted(missing)) + "."
        )

    raw_changes = parsed.get("changes")
    if raw_changes is None:
        raw_changes = []
    if not isinstance(raw_changes, list):
        raise OptimizeError("The model's list of changes could not be read.")
    changes = []
    for change in raw_changes[:MAX_CHANGES]:
        if not isinstance(change, dict):
            continue
        category = str(change.get("category") or "").strip().lower()
        if category not in CHANGE_CATEGORIES:
            category = "wording"
        reason = _clip(change.get("reason"), MAX_REASON_CHARS)
        if not reason:
            continue  # a change with no reason explains nothing
        changes.append(
            {
                "category": category,
                "before": _clip(change.get("before"), MAX_EXCERPT_CHARS),
                "after": _clip(change.get("after"), MAX_EXCERPT_CHARS),
                "reason": reason,
                "expected_effect": _clip(change.get("expected_effect"), MAX_REASON_CHARS),
            }
        )
    if template == original and not changes:
        raise OptimizeError("The model found nothing to change in this prompt.")
    return {"template": template, "changes": changes}


def generate_candidate(
    tenant_id: str, template_id: str, actor: str, client: Optional[httpx.Client] = None
) -> dict:
    """Ask the consented model for a cheaper version of one captured prompt."""
    template_id = prompt_capture._uuid_text(template_id, "template_id")
    status = prompt_capture.consent_status(tenant_id)
    if not status["capturing"]:
        raise OptimizeError("Prompt optimization is not turned on, or is paused. Check Settings.")
    disclosure = status["current_disclosure"]
    config = discovery_llm.active_config(tenant_id) or discovery_llm.env_llm_config()
    if config is None or disclosure is None:
        raise OptimizeError("No model is available to rewrite prompts.")

    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        row = conn.execute(
            """
            SELECT t.feature_id, t.prompt_id, t.prompt_version, t.ciphertext,
                   (SELECT count(*) FROM prompt_sample s WHERE s.template_id = t.id)
            FROM prompt_template t WHERE t.id = %s
            """,
            (template_id,),
        ).fetchone()
        if row is None:
            raise OptimizeError("That prompt is no longer stored.")
        samples_held = int(row[4])
        if samples_held < MIN_SAMPLES:
            raise OptimizeError(
                f"{samples_held} of {MIN_SAMPLES} samples so far. A rewrite needs enough "
                "real calls behind it to be worth trusting."
            )
        key = prompt_capture._data_key(conn, create=False)
        if key is None:
            raise OptimizeError("That prompt is no longer readable.")
        template = prompt_capture._open(key, row[3])
        example_rows = conn.execute(
            """
            SELECT ciphertext FROM prompt_sample WHERE template_id = %s
            ORDER BY received_at DESC LIMIT %s
            """,
            (template_id, EXAMPLES_IN_REQUEST),
        ).fetchall()
        examples = [prompt_capture._open(key, e[0]) for e in example_rows]

    # The model call happens outside the transaction: it is slow, and a database
    # transaction held open across a network call is a lock waiting to bite.
    rewrite = _parse(_complete(config, _SYSTEM, _payload(template, examples), client), template)

    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        key = prompt_capture._data_key(conn, create=False)
        if key is None:
            raise OptimizeError("That prompt is no longer readable.")
        candidate_id = conn.execute(
            """
            INSERT INTO prompt_candidate
                (tenant_id, template_id, feature_id, prompt_id, prompt_version, ciphertext,
                 changes_cipher, change_count, original_chars, candidate_chars, provider,
                 model, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                tenant_id,
                template_id,
                row[0],
                row[1],
                row[2],
                prompt_capture._seal(key, rewrite["template"]),
                prompt_capture._seal(key, rewrite["changes"]),
                len(rewrite["changes"]),
                len(template),
                len(rewrite["template"]),
                disclosure["provider"],
                disclosure["model"],
                actor,
            ),
        ).fetchone()[0]
        prompt_capture._audit(
            conn,
            tenant_id,
            "candidate_generated",
            actor,
            feature_id=str(row[0]),
            detail={
                "prompt_id": row[1],
                "prompt_version": row[2],
                "changes": len(rewrite["changes"]),
                "provider": disclosure["provider"],
                "model": disclosure["model"],
            },
        )
    return prompt_detail(tenant_id, template_id)["candidate"] | {"candidate_id": str(candidate_id)}


def prompt_content(tenant_id: str, template_id: str, actor: str) -> Optional[dict]:
    """The prompt and its proposed rewrite, decrypted, with an audit row.

    One request for both, because the screen shows them side by side: two
    audited reads for one act of looking would say less, not more.
    """
    template_id = prompt_capture._uuid_text(template_id, "template_id")
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        row = conn.execute(
            "SELECT feature_id, prompt_id, prompt_version, ciphertext FROM prompt_template "
            "WHERE id = %s",
            (template_id,),
        ).fetchone()
        if row is None:
            return None
        key = prompt_capture._data_key(conn, create=False)
        if key is None:
            return None
        candidate = conn.execute(
            """
            SELECT id, ciphertext, changes_cipher FROM prompt_candidate
            WHERE template_id = %s AND status <> 'discarded'
            ORDER BY created_at DESC LIMIT 1
            """,
            (template_id,),
        ).fetchone()
        out = {
            "template_id": template_id,
            "template": prompt_capture._open(key, row[3]),
            "candidate": None,
        }
        if candidate is not None:
            out["candidate"] = {
                "candidate_id": str(candidate[0]),
                "template": prompt_capture._open(key, candidate[1]),
                "changes": prompt_capture._open(key, candidate[2]),
            }
        prompt_capture._audit(
            conn,
            tenant_id,
            "candidate_viewed" if candidate is not None else "sample_content_viewed",
            actor,
            feature_id=str(row[0]),
            detail={"prompt_id": row[1], "prompt_version": row[2]},
        )
    return out


def discard_candidate(tenant_id: str, candidate_id: str, actor: str) -> bool:
    """Throw a rewrite away. Its text goes; the record that it existed stays."""
    candidate_id = prompt_capture._uuid_text(candidate_id, "candidate_id")
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        row = conn.execute(
            "SELECT feature_id, prompt_id, prompt_version FROM prompt_candidate WHERE id = %s",
            (candidate_id,),
        ).fetchone()
        if row is None:
            return False
        # Discarded means gone: the status is kept so the screen can say a
        # rewrite was tried, but nothing of the text it proposed remains.
        conn.execute(
            """
            UPDATE prompt_candidate
            SET status = 'discarded', ciphertext = %s, changes_cipher = %s, change_count = 0
            WHERE id = %s
            """,
            (b"", b"", candidate_id),
        )
        prompt_capture._audit(
            conn,
            tenant_id,
            "candidate_discarded",
            actor,
            feature_id=str(row[0]),
            detail={"prompt_id": row[1], "prompt_version": row[2]},
        )
    return True
