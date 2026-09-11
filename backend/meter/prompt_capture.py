"""Consented prompt capture — the store behind Prompt Optimization (PO-1).

This is the only place in Meter that holds prompt text, and it holds it only on
terms the customer agreed to. See docs/prompt-optimization-spec.md.

What this module guarantees:

**Nothing without both keys.** A sample is stored only while the organization's
consent stands (current consent text, and the model it named is still the model
that would see prompts) AND the sample's feature has been switched on. Anything
else is refused before a byte of content is decrypted, encrypted or written.

**Content is never readable at rest.** Templates and samples are encrypted under
a per-tenant data key; that key is stored only wrapped by the app key. Listing
reads identities and numbers, never ciphertext. Showing content is a separate
call that writes an audit row.

**Withdrawal destroys, it does not hide.** Withdrawing consent deletes every
template, sample and feature switch, then the data key itself, so ciphertext that
outlives the delete (a backup) cannot be read.

**Short-lived.** Samples go 30 days after they arrive; templates 30 days after
they were last seen.

**Content never leaves through a side door.** Nothing here logs a payload, and
every error message names a field or a rule, never a value.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import uuid
from typing import Any, Optional
from urllib.parse import urlparse

from cryptography.fernet import Fernet

from . import crypto, discovery_llm
from .db import admin_dsn, app_dsn, connect, tenant_tx

#: The consent text's version. Change it whenever the words on the consent screen
#: change in substance: every organization's capture then pauses until someone
#: agrees to the new text.
CONSENT_VERSION = "2026-09-11"

RETENTION_DAYS = 30
#: A sample larger than this is refused, not truncated: a truncated prompt would
#: later be evaluated as a different prompt.
MAX_SAMPLE_BYTES = 64 * 1024
#: Per feature and prompt, per UTC day. The SDK samples far below this; the
#: server enforces it whatever a client does.
MAX_SAMPLES_PER_DAY = 50
#: Kept per prompt version; the oldest go first.
MAX_SAMPLES_PER_VERSION = 200

MAX_ID = 200
MAX_VERSION = 60
MAX_PROVIDER = 60
MAX_MODEL = 120
MAX_MESSAGES = 50
ROLES = ("system", "user", "assistant")

#: Display names for Meter's own endpoint, recognised by its host.
_PROVIDER_NAMES = {
    "groq": "Groq",
    "openai": "OpenAI",
    "openrouter": "OpenRouter",
    "together": "Together AI",
    "fireworks": "Fireworks AI",
    "mistral": "Mistral AI",
    "xai": "xAI",
    "deepinfra": "DeepInfra",
    "ollama": "Ollama (self-hosted)",
}


class PromptCaptureError(ValueError):
    """Invalid input to a consent or capture call (maps to HTTP 400)."""


class CaptureRefused(Exception):
    """A sample that must not be stored. Carries a stable reason and an HTTP status.

    Reasons: no_consent, consent_outdated, disclosure_changed, feature_not_enabled
    (403); too_large (413); daily_cap (429).
    """

    def __init__(self, reason: str, status: int, message: str):
        super().__init__(message)
        self.reason = reason
        self.status = status


# ---------------------------------------------------------------------------
# Disclosure: which model would see prompts
# ---------------------------------------------------------------------------
def _provider_from_url(base_url: str) -> str:
    host = (urlparse(base_url).hostname or "").lower()
    for key, url in discovery_llm.PROVIDER_BASE_URLS.items():
        known = (urlparse(url).hostname or "").lower()
        if known and host == known:
            return _PROVIDER_NAMES.get(key, key)
    return host or "Custom endpoint"


def disclosure(tenant_id: str) -> Optional[dict]:
    """The provider and model that would see this organization's prompts, or None.

    The organization's own model when it has configured and enabled one, otherwise
    Meter's. None when neither exists: consent cannot name who would see prompts,
    so it cannot be given.
    """
    own = discovery_llm.status(tenant_id)
    if own.get("configured") and own.get("enabled"):
        key = own.get("provider") or "custom"
        name = _PROVIDER_NAMES.get(key) or (
            _provider_from_url(own.get("base_url") or "") if key == "custom" else key
        )
        return {"source": "byok", "provider": name, "model": own.get("model") or ""}
    env = discovery_llm.env_llm_config()
    if env is None:
        return None
    return {"source": "meter", "provider": _provider_from_url(env.base_url), "model": env.model}


def _same_disclosure(a: Optional[dict], b: Optional[dict]) -> bool:
    if not a or not b:
        return False
    return all(a.get(k) == b.get(k) for k in ("source", "provider", "model"))


# ---------------------------------------------------------------------------
# Envelope encryption
# ---------------------------------------------------------------------------
def _data_key(conn, *, create: bool) -> Optional[bytes]:
    """The tenant's data key (inside the caller's tenant transaction), or None."""
    row = conn.execute("SELECT wrapped_key FROM prompt_data_key").fetchone()
    if row is not None:
        return crypto.decrypt(row[0]).encode()
    if not create:
        return None
    key = Fernet.generate_key()
    tenant = conn.execute("SELECT current_setting('app.current_tenant')").fetchone()[0]
    conn.execute(
        "INSERT INTO prompt_data_key (tenant_id, wrapped_key) VALUES (%s, %s)",
        (tenant, crypto.encrypt(key.decode())),
    )
    return key


def _seal(key: bytes, value: Any) -> bytes:
    return Fernet(key).encrypt(json.dumps(value, separators=(",", ":")).encode())


def _open(key: bytes, token: bytes) -> Any:
    return json.loads(Fernet(key).decrypt(bytes(token)).decode())


def _digest(key: bytes, text: str) -> str:
    """A keyed digest: equal texts match within the tenant, guesses do not."""
    return hmac.new(key, text.encode(), hashlib.sha256).hexdigest()


def _audit(
    conn,
    tenant_id: str,
    event: str,
    actor: Optional[str],
    *,
    feature_id: Optional[str] = None,
    detail: Optional[dict] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO prompt_audit (tenant_id, event, actor, feature_id, detail)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (tenant_id, event, actor, feature_id, json.dumps(detail or {})),
    )


# ---------------------------------------------------------------------------
# Consent
# ---------------------------------------------------------------------------
def consent_status(tenant_id: str) -> dict:
    """Where consent stands, what it named, and which features capture.

    Returns identities and counts only. `capturing` is the one answer that matters
    to the SDK and the UI alike: true only when capture is actually open.
    """
    current = disclosure(tenant_id)
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        row = conn.execute(
            """
            SELECT consent_version, granted_by, granted_at,
                   disclosed_source, disclosed_provider, disclosed_model
            FROM prompt_capture_consent
            """
        ).fetchone()
        features = conn.execute(
            """
            SELECT f.id, f.name, pcf.enabled_at IS NOT NULL, pcf.enabled_by, pcf.enabled_at,
                   (SELECT count(*) FROM prompt_sample s WHERE s.feature_id = f.id)
            FROM feature f
            LEFT JOIN prompt_capture_feature pcf ON pcf.feature_id = f.id
            WHERE f.status <> 'archived' OR pcf.feature_id IS NOT NULL
            ORDER BY f.name
            """
        ).fetchall()

    granted = None
    if row is not None:
        granted = {
            "consent_version": row[0],
            "granted_by": row[1],
            "granted_at": row[2].isoformat(),
            "disclosed": {"source": row[3], "provider": row[4], "model": row[5]},
        }
    outdated = bool(granted) and granted["consent_version"] != CONSENT_VERSION
    changed = bool(granted) and not _same_disclosure(granted["disclosed"], current)
    return {
        "consent": granted,
        "current_version": CONSENT_VERSION,
        "current_disclosure": current,
        "version_outdated": outdated,
        "disclosure_changed": changed,
        "capturing": bool(granted) and not outdated and not changed,
        "retention_days": RETENTION_DAYS,
        "features": [
            {
                "feature_id": str(f[0]),
                "name": f[1],
                "enabled": bool(f[2]),
                "enabled_by": f[3],
                "enabled_at": f[4].isoformat() if f[4] else None,
                "samples": int(f[5]),
            }
            for f in features
        ],
    }


def grant_consent(
    tenant_id: str, actor: str, *, accepted_version: str, accepted_disclosure: dict
) -> dict:
    """Record organization consent to the text and disclosure the person saw.

    Both must match what is current. A screen left open while the consent text
    changed, or while someone pointed the organization's model elsewhere, must not
    turn into consent to something the person never read. Verifying the person's
    password is the caller's job (the API does it before calling this).
    """
    if accepted_version != CONSENT_VERSION:
        raise PromptCaptureError(
            "The consent text has changed since this page was loaded. Reload and review it."
        )
    current = disclosure(tenant_id)
    if current is None:
        raise PromptCaptureError(
            "No model is available to optimize prompts, so consent cannot name one."
        )
    if not _same_disclosure(accepted_disclosure, current):
        raise PromptCaptureError(
            "The model that would see prompts has changed since this page was loaded. "
            "Reload and review it."
        )
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        conn.execute(
            """
            INSERT INTO prompt_capture_consent
                (tenant_id, consent_version, granted_by, disclosed_source,
                 disclosed_provider, disclosed_model)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id) DO UPDATE SET
                consent_version = EXCLUDED.consent_version,
                granted_by = EXCLUDED.granted_by,
                granted_at = now(),
                disclosed_source = EXCLUDED.disclosed_source,
                disclosed_provider = EXCLUDED.disclosed_provider,
                disclosed_model = EXCLUDED.disclosed_model
            """,
            (
                tenant_id,
                CONSENT_VERSION,
                actor,
                current["source"],
                current["provider"],
                current["model"],
            ),
        )
        _data_key(conn, create=True)
        _audit(
            conn,
            tenant_id,
            "consent_granted",
            actor,
            detail={"consent_version": CONSENT_VERSION, **current},
        )
    return consent_status(tenant_id)


def withdraw_consent(tenant_id: str, actor: str) -> dict:
    """Stop capture everywhere and destroy what it collected, key included."""
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        samples = conn.execute("DELETE FROM prompt_sample RETURNING id").rowcount
        templates = conn.execute("DELETE FROM prompt_template RETURNING id").rowcount
        features = conn.execute("DELETE FROM prompt_capture_feature RETURNING feature_id").rowcount
        had = conn.execute("DELETE FROM prompt_capture_consent RETURNING tenant_id").rowcount
        keys = conn.execute("DELETE FROM prompt_data_key RETURNING tenant_id").rowcount
        if had or samples or templates or features:
            _audit(
                conn,
                tenant_id,
                "consent_withdrawn",
                actor,
                detail={
                    "samples_deleted": samples,
                    "templates_deleted": templates,
                    "features_disabled": features,
                },
            )
        if keys:
            _audit(conn, tenant_id, "data_key_destroyed", actor)
    return consent_status(tenant_id)


def set_feature(tenant_id: str, feature_id: str, enabled: bool, actor: str) -> dict:
    """Switch capture on or off for one feature. Off deletes that feature's content."""
    feature_id = _uuid_text(feature_id, "feature_id")
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        if conn.execute("SELECT 1 FROM feature WHERE id = %s", (feature_id,)).fetchone() is None:
            raise PromptCaptureError("Feature not found.")
        if enabled:
            if conn.execute("SELECT 1 FROM prompt_capture_consent").fetchone() is None:
                raise PromptCaptureError(
                    "Turn on prompt optimization for the organization before choosing features."
                )
            inserted = conn.execute(
                """
                INSERT INTO prompt_capture_feature (tenant_id, feature_id, enabled_by)
                VALUES (%s, %s, %s) ON CONFLICT DO NOTHING RETURNING feature_id
                """,
                (tenant_id, feature_id, actor),
            ).fetchone()
            if inserted:
                _audit(conn, tenant_id, "feature_enabled", actor, feature_id=feature_id)
        else:
            samples = conn.execute(
                "DELETE FROM prompt_sample WHERE feature_id = %s RETURNING id", (feature_id,)
            ).rowcount
            templates = conn.execute(
                "DELETE FROM prompt_template WHERE feature_id = %s RETURNING id", (feature_id,)
            ).rowcount
            removed = conn.execute(
                "DELETE FROM prompt_capture_feature WHERE feature_id = %s RETURNING feature_id",
                (feature_id,),
            ).rowcount
            if removed or samples or templates:
                _audit(
                    conn,
                    tenant_id,
                    "feature_disabled",
                    actor,
                    feature_id=feature_id,
                    detail={"samples_deleted": samples, "templates_deleted": templates},
                )
    return consent_status(tenant_id)


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------
def _uuid_text(value: Any, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise PromptCaptureError(f"{field} must be a feature id.") from exc


def _text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PromptCaptureError(f"{field} is required.")
    if len(value) > limit:
        raise PromptCaptureError(f"{field} must be {limit} characters or fewer.")
    return value.strip()


def _count(value: Any, field: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PromptCaptureError(f"{field} must be a whole number.")
    return value


_SAMPLE_FIELDS = frozenset(
    {
        "feature_id",
        "prompt_id",
        "prompt_version",
        "provider",
        "model",
        "template",
        "input",
        "output",
        "parameters",
        "tokens_in",
        "tokens_out",
        "latency_ms",
        "captured_at",
    }
)
_PARAMETER_FIELDS = frozenset({"temperature", "max_tokens", "top_p", "response_format"})


def _validate_sample(payload: Any, now: dt.datetime) -> dict:
    """A sample reduced to exactly what may be stored, or PromptCaptureError.

    Strict, unlike the trace path's quiet allowlist: this channel carries content,
    so an unknown field — a tool call, an image, a document — is refused rather
    than dropped, and the sender learns why.
    """
    if not isinstance(payload, dict):
        raise PromptCaptureError("A sample must be an object.")
    unknown = set(payload) - _SAMPLE_FIELDS
    if unknown:
        raise PromptCaptureError(
            "Unsupported sample fields: "
            + ", ".join(sorted(unknown))
            + ". Samples carry text messages and output only, never tool calls, "
            "tool results, images or documents."
        )
    template = payload.get("template")
    if not isinstance(template, str):
        raise PromptCaptureError("template must be text.")
    output = payload.get("output")
    if not isinstance(output, str):
        raise PromptCaptureError("output must be text.")
    messages = payload.get("input")
    if not isinstance(messages, list) or len(messages) > MAX_MESSAGES:
        raise PromptCaptureError(f"input must be a list of at most {MAX_MESSAGES} messages.")
    clean_messages = []
    for message in messages:
        if not isinstance(message, dict) or set(message) - {"role", "text"}:
            raise PromptCaptureError("Each input message must be {role, text}.")
        if message.get("role") not in ROLES:
            raise PromptCaptureError(f"A message role must be one of: {', '.join(ROLES)}.")
        if not isinstance(message.get("text"), str):
            raise PromptCaptureError("A message's text must be text.")
        clean_messages.append({"role": message["role"], "text": message["text"]})
    parameters = payload.get("parameters") or {}
    if not isinstance(parameters, dict) or set(parameters) - _PARAMETER_FIELDS:
        raise PromptCaptureError(
            "parameters may only hold: " + ", ".join(sorted(_PARAMETER_FIELDS)) + "."
        )
    for name, value in parameters.items():
        if not isinstance(value, (int, float, str)) or isinstance(value, bool):
            raise PromptCaptureError(f"parameters.{name} must be a number or text.")

    captured = payload.get("captured_at")
    when = now
    if isinstance(captured, str):
        try:
            parsed = dt.datetime.fromisoformat(captured.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            if now - dt.timedelta(days=RETENTION_DAYS) <= parsed <= now + dt.timedelta(minutes=5):
                when = parsed
        except ValueError:
            pass

    return {
        "feature_id": _uuid_text(payload.get("feature_id"), "feature_id"),
        "prompt_id": _text(payload.get("prompt_id"), "prompt_id", MAX_ID),
        "prompt_version": _text(payload.get("prompt_version"), "prompt_version", MAX_VERSION),
        "provider": _text(payload.get("provider"), "provider", MAX_PROVIDER).lower(),
        "model": _text(payload.get("model"), "model", MAX_MODEL),
        "template": template,
        "content": {"input": clean_messages, "output": output, "parameters": parameters},
        "tokens_in": _count(payload.get("tokens_in"), "tokens_in"),
        "tokens_out": _count(payload.get("tokens_out"), "tokens_out"),
        "latency_ms": _count(payload.get("latency_ms"), "latency_ms"),
        "captured_at": when,
    }


def _gate(conn, tenant_id: str, feature_id: str) -> None:
    """Refuse unless capture is open for this feature right now."""
    row = conn.execute(
        """
        SELECT consent_version, disclosed_source, disclosed_provider, disclosed_model
        FROM prompt_capture_consent
        """
    ).fetchone()
    if row is None:
        raise CaptureRefused(
            "no_consent", 403, "Prompt optimization is not turned on for this organization."
        )
    if row[0] != CONSENT_VERSION:
        raise CaptureRefused(
            "consent_outdated", 403, "Capture is paused until the updated consent is accepted."
        )
    if (
        conn.execute(
            "SELECT 1 FROM prompt_capture_feature WHERE feature_id = %s", (feature_id,)
        ).fetchone()
        is None
    ):
        raise CaptureRefused(
            "feature_not_enabled", 403, "Prompt capture is not turned on for this feature."
        )


def capture_open(tenant_id: str, feature_id: str) -> dict:
    """Whether a sample for this feature would be accepted now. For the SDK (PO-2)."""
    feature_id = _uuid_text(feature_id, "feature_id")
    status = consent_status(tenant_id)
    if status["consent"] and status["disclosure_changed"]:
        return {"open": False, "reason": "disclosure_changed"}
    try:
        with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
            _gate(conn, tenant_id, feature_id)
    except CaptureRefused as refused:
        return {"open": False, "reason": refused.reason}
    return {"open": True, "reason": None}


def store_sample(tenant_id: str, payload: Any, now: Optional[dt.datetime] = None) -> dict:
    """Validate, gate, cap, encrypt and store one sample. Returns its id.

    Order matters: validation first (no database), then the consent gate (nothing
    decrypted or created for a refused sample), then size and caps, then content.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    sample = _validate_sample(payload, now)
    size = len(sample["template"].encode()) + len(
        json.dumps(sample["content"], separators=(",", ":")).encode()
    )
    if size > MAX_SAMPLE_BYTES:
        raise CaptureRefused(
            "too_large",
            413,
            f"A sample may be at most {MAX_SAMPLE_BYTES // 1024} KB; "
            "larger samples are dropped, not truncated.",
        )

    current = disclosure(tenant_id)
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        _gate(conn, tenant_id, sample["feature_id"])
        disclosed = conn.execute(
            "SELECT disclosed_source, disclosed_provider, disclosed_model "
            "FROM prompt_capture_consent"
        ).fetchone()
        named = {"source": disclosed[0], "provider": disclosed[1], "model": disclosed[2]}
        if not _same_disclosure(named, current):
            raise CaptureRefused(
                "disclosure_changed",
                403,
                "Capture is paused: the model that would see prompts changed.",
            )

        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today = conn.execute(
            """
            SELECT count(*) FROM prompt_sample
            WHERE feature_id = %s AND prompt_id = %s AND received_at >= %s
            """,
            (sample["feature_id"], sample["prompt_id"], day_start),
        ).fetchone()[0]
        if today >= MAX_SAMPLES_PER_DAY:
            raise CaptureRefused(
                "daily_cap", 429, f"At most {MAX_SAMPLES_PER_DAY} samples a day per prompt."
            )

        key = _data_key(conn, create=False)
        if key is None:  # consent without a key cannot happen; refuse rather than guess
            raise CaptureRefused(
                "no_consent", 403, "Prompt optimization is not turned on for this organization."
            )

        template_id = conn.execute(
            """
            INSERT INTO prompt_template
                (tenant_id, feature_id, prompt_id, prompt_version, template_digest,
                 ciphertext, size_bytes)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, feature_id, prompt_id, prompt_version, template_digest)
            DO UPDATE SET last_seen_at = now()
            RETURNING id
            """,
            (
                tenant_id,
                sample["feature_id"],
                sample["prompt_id"],
                sample["prompt_version"],
                _digest(key, sample["template"]),
                _seal(key, sample["template"]),
                len(sample["template"].encode()),
            ),
        ).fetchone()[0]

        sample_id = conn.execute(
            """
            INSERT INTO prompt_sample
                (tenant_id, feature_id, template_id, prompt_id, prompt_version, provider,
                 model, ciphertext, size_bytes, tokens_in, tokens_out, latency_ms, captured_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                tenant_id,
                sample["feature_id"],
                template_id,
                sample["prompt_id"],
                sample["prompt_version"],
                sample["provider"],
                sample["model"],
                _seal(key, sample["content"]),
                size,
                sample["tokens_in"],
                sample["tokens_out"],
                sample["latency_ms"],
                sample["captured_at"],
            ),
        ).fetchone()[0]

        # Keep the newest MAX_SAMPLES_PER_VERSION for this version; older ones go.
        conn.execute(
            """
            DELETE FROM prompt_sample WHERE id IN (
                SELECT id FROM prompt_sample
                WHERE feature_id = %s AND prompt_id = %s AND prompt_version = %s
                ORDER BY received_at DESC, id DESC
                OFFSET %s
            )
            """,
            (
                sample["feature_id"],
                sample["prompt_id"],
                sample["prompt_version"],
                MAX_SAMPLES_PER_VERSION,
            ),
        )
    return {"stored": True, "sample_id": str(sample_id)}


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def list_samples(
    tenant_id: str, *, feature_id: Optional[str] = None, limit: int = 100
) -> list[dict]:
    """Samples as identities and numbers. Never content, never ciphertext."""
    limit = max(1, min(int(limit), 500))
    clause, params = "", []
    if feature_id:
        clause, params = "WHERE s.feature_id = %s", [_uuid_text(feature_id, "feature_id")]
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        rows = conn.execute(
            f"""
            SELECT s.id, s.feature_id, f.name, s.prompt_id, s.prompt_version, s.provider,
                   s.model, s.size_bytes, s.tokens_in, s.tokens_out, s.latency_ms,
                   s.captured_at, s.received_at
            FROM prompt_sample s JOIN feature f ON f.id = s.feature_id
            {clause}
            ORDER BY s.received_at DESC
            LIMIT %s
            """,  # noqa: S608 - clause is one of two fixed strings, values are parameters
            (*params, limit),
        ).fetchall()
    return [
        {
            "sample_id": str(r[0]),
            "feature_id": str(r[1]),
            "feature_name": r[2],
            "prompt_id": r[3],
            "prompt_version": r[4],
            "provider": r[5],
            "model": r[6],
            "size_bytes": r[7],
            "tokens_in": r[8],
            "tokens_out": r[9],
            "latency_ms": r[10],
            "captured_at": r[11].isoformat(),
            "received_at": r[12].isoformat(),
        }
        for r in rows
    ]


def show_sample(tenant_id: str, sample_id: str, actor: str) -> Optional[dict]:
    """One sample's content, decrypted, with an audit row for the viewing."""
    sample_id = _uuid_text(sample_id, "sample_id")
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        row = conn.execute(
            """
            SELECT s.feature_id, s.prompt_id, s.prompt_version, s.ciphertext, t.ciphertext
            FROM prompt_sample s JOIN prompt_template t ON t.id = s.template_id
            WHERE s.id = %s
            """,
            (sample_id,),
        ).fetchone()
        if row is None:
            return None
        key = _data_key(conn, create=False)
        if key is None:
            return None
        content = _open(key, row[3])
        template = _open(key, row[4])
        _audit(
            conn,
            tenant_id,
            "sample_content_viewed",
            actor,
            feature_id=str(row[0]),
            detail={"sample_id": sample_id, "prompt_id": row[1], "prompt_version": row[2]},
        )
    return {"sample_id": sample_id, "template": template, **content}


def audit_log(tenant_id: str, limit: int = 100) -> list[dict]:
    limit = max(1, min(int(limit), 500))
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        rows = conn.execute(
            """
            SELECT a.event, a.actor, a.feature_id, f.name, a.detail, a.created_at
            FROM prompt_audit a LEFT JOIN feature f ON f.id = a.feature_id
            ORDER BY a.created_at DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
    return [
        {
            "event": r[0],
            "actor": r[1],
            "feature_id": str(r[2]) if r[2] else None,
            "feature_name": r[3],
            "detail": r[4],
            "created_at": r[5].isoformat(),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------
def purge_expired(tenant_id: str, now: Optional[dt.datetime] = None) -> dict:
    """Delete samples older than the retention window, and templates unseen for as long."""
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=RETENTION_DAYS)
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        samples = conn.execute(
            "DELETE FROM prompt_sample WHERE received_at < %s RETURNING id", (cutoff,)
        ).rowcount
        templates = conn.execute(
            """
            DELETE FROM prompt_template t
            WHERE t.last_seen_at < %s
              AND NOT EXISTS (SELECT 1 FROM prompt_sample s WHERE s.template_id = t.id)
            RETURNING id
            """,
            (cutoff,),
        ).rowcount
        if samples or templates:
            _audit(
                conn,
                tenant_id,
                "samples_purged",
                None,
                detail={
                    "samples_deleted": samples,
                    "templates_deleted": templates,
                    "cutoff": cutoff.isoformat(),
                },
            )
    return {"samples_deleted": samples, "templates_deleted": templates}


def purge_all_tenants(now: Optional[dt.datetime] = None) -> list[dict]:
    """Retention sweep across every tenant. Cron entry point."""
    with connect(admin_dsn()) as conn:
        tenants = [str(r[0]) for r in conn.execute("SELECT id FROM tenant")]
    out = []
    for tenant_id in tenants:
        result = purge_expired(tenant_id, now)
        if result["samples_deleted"] or result["templates_deleted"]:
            out.append({"tenant_id": tenant_id, **result})
    return out


if __name__ == "__main__":
    swept = purge_all_tenants()
    print(f"Purged expired prompt samples for {len(swept)} tenants.")
    for entry in swept:
        print(" ", entry)
