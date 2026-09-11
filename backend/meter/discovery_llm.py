"""Bring-your-own LLM key for feature discovery (optional, per tenant).

Discovery clusters PR metadata with an LLM. By default that is Meter's own
server-side endpoint (METER_DISCOVERY_*). A tenant can point it at their own
account instead — their provider, their key, their model — and this module is
where that configuration lives.

Two rules shape everything here:

  * **The key never comes back out.** It is encrypted before it reaches the
    database (crypto.encrypt, as connector_credential does) and decrypted only to
    build the outbound request. No read path returns it, and `redact()` scrubs it
    from any provider error before that error is shown or logged.
  * **Absence changes nothing.** No configuration, or a disabled one, means
    `active_config()` returns None and discovery behaves exactly as it always has.

Storage goes through the app role inside a tenant transaction, so RLS guarantees
a tenant can only ever read or write its own row.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import httpx

from .crypto import decrypt, encrypt
from .db import app_dsn, connect, tenant_tx


@dataclass(frozen=True)
class LlmConfig:
    """Where discovery sends its clustering request.

    One shape for both sources — Meter's own endpoint read from env, and a
    tenant's own configuration (BYOK) read from the database — so there is a
    single OpenAI-compatible implementation rather than one per provider.
    """

    base_url: str
    api_key: str
    model: str


#: The model used when nothing else is configured. Groq retired
#: llama-3.3-70b-versatile for Free and Developer plans in August 2026, so a
#: default naming it would fail for exactly the accounts most likely to be
#: relying on a default at all.
DEFAULT_DISCOVERY_MODEL = "openai/gpt-oss-120b"


def env_llm_config() -> Optional[LlmConfig]:
    """Meter's own discovery endpoint, from env. None when unconfigured.

    METER_DISCOVERY_BASE_URL  e.g. https://api.groq.com/openai/v1
    METER_DISCOVERY_API_KEY   the provider key ("ollama" for local Ollama)
    METER_DISCOVERY_MODEL     e.g. openai/gpt-oss-120b
    """
    base = os.environ.get("METER_DISCOVERY_BASE_URL")
    if not base:
        return None
    return LlmConfig(
        base_url=base,
        api_key=os.environ.get("METER_DISCOVERY_API_KEY", ""),
        model=os.environ.get("METER_DISCOVERY_MODEL", DEFAULT_DISCOVERY_MODEL),
    )


#: OpenAI-compatible hosts we can prefill an endpoint for. The base URL is only a
#: starting point — it is stored per tenant and always editable, so a provider
#: changing its path (or a private deployment) needs no code change. Anything not
#: listed is configured as "custom" with its own base URL.
PROVIDER_BASE_URLS = {
    "groq": "https://api.groq.com/openai/v1",
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "together": "https://api.together.xyz/v1",
    "fireworks": "https://api.fireworks.ai/inference/v1",
    "mistral": "https://api.mistral.ai/v1",
    "xai": "https://api.x.ai/v1",
    "deepinfra": "https://api.deepinfra.com/v1/openai",
    "ollama": "http://localhost:11434/v1",
    "custom": "",
}

MAX_URL = 500
MAX_MODEL = 200
MAX_KEY = 4096

#: Long enough to cluster a real PR set, short enough that a wedged endpoint
#: doesn't hold a discovery run open indefinitely.
CLUSTER_TIMEOUT = 60.0
#: The connection test only asks for a token or two; it should feel instant.
TEST_TIMEOUT = 20.0


class ByokError(ValueError):
    """Invalid BYOK input (maps to HTTP 400)."""


def redact(text: str, secret: Optional[str]) -> str:
    """Remove a secret from text before it is shown or logged.

    Provider errors quote the request, and a base URL can carry a key in a query
    string. Cheap insurance for the one thing this module must never leak.
    """
    if not text:
        return ""
    if secret and len(secret) >= 8:
        text = text.replace(secret, "***")
    return text


def _validate(provider: str, base_url: str, model: str) -> tuple:
    if provider not in PROVIDER_BASE_URLS:
        raise ByokError(f"Unsupported provider: {provider!r}.")
    url = (base_url or PROVIDER_BASE_URLS[provider]).strip()
    if not url:
        raise ByokError("A base URL is required for this provider.")
    if not url.startswith(("http://", "https://")):
        raise ByokError("Base URL must start with http:// or https://.")
    if len(url) > MAX_URL:
        raise ByokError("Base URL is too long.")
    name = (model or "").strip()
    if not name:
        raise ByokError("A model is required.")
    if len(name) > MAX_MODEL:
        raise ByokError("Model name is too long.")
    return url, name


def save(
    tenant_id: str,
    *,
    provider: str,
    base_url: str = "",
    model: str,
    api_key: Optional[str] = None,
    enabled: bool = True,
    updated_by: Optional[str] = None,
) -> dict:
    """Create or replace the tenant's discovery LLM configuration.

    `api_key` may be omitted when updating an existing configuration — the stored
    key is kept — so the UI can edit the model or endpoint without the user
    re-entering a secret it never showed them.
    """
    url, name = _validate(provider, base_url, model)
    key = (api_key or "").strip()
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        existing = conn.execute("SELECT ciphertext FROM discovery_llm").fetchone()
        if key:
            ciphertext = encrypt(key)
        elif existing:
            ciphertext = existing[0]  # editing without re-entering the secret
        else:
            raise ByokError("An API key is required.")
        conn.execute(
            """
            INSERT INTO discovery_llm
                (tenant_id, provider, base_url, model, ciphertext, enabled, updated_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id) DO UPDATE
            SET provider = EXCLUDED.provider, base_url = EXCLUDED.base_url,
                model = EXCLUDED.model, ciphertext = EXCLUDED.ciphertext,
                enabled = EXCLUDED.enabled, updated_by = EXCLUDED.updated_by,
                updated_at = now()
            """,
            (tenant_id, provider, url, name, ciphertext, enabled, updated_by),
        )
    return status(tenant_id)


def status(tenant_id: str) -> dict:
    """The tenant's configuration WITHOUT the key — the only read the API exposes.

    `has_key` is the whole truth about the secret: not a prefix, not a suffix,
    not a length. There is no read path that returns any part of it.
    """
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        row = conn.execute(
            "SELECT provider, base_url, model, enabled, updated_at, updated_by FROM discovery_llm"
        ).fetchone()
    if row is None:
        return {"configured": False, "enabled": False, "has_key": False}
    return {
        "configured": True,
        "provider": row[0],
        "base_url": row[1],
        "model": row[2],
        "enabled": row[3],
        "has_key": True,
        "updated_at": row[4].isoformat() if row[4] else None,
        "updated_by": row[5],
    }


def _stored_config(tenant_id: str) -> Optional[tuple]:
    """(LlmConfig, enabled) for the tenant, or None when nothing is configured."""
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        row = conn.execute(
            "SELECT base_url, model, ciphertext, enabled FROM discovery_llm"
        ).fetchone()
    if row is None:
        return None
    return LlmConfig(
        base_url=row[0], model=row[1] or DEFAULT_DISCOVERY_MODEL, api_key=decrypt(row[2])
    ), row[3]


def active_config(tenant_id: str) -> Optional[LlmConfig]:
    """The config discovery should use, or None to leave existing behaviour alone.

    Never raises: a tenant whose stored key cannot be decrypted (a rotated
    APP_SECRET_KEY, say) falls back to Meter's endpoint rather than losing
    the ability to run discovery at all.
    """
    try:
        stored = _stored_config(tenant_id)
    except Exception:
        return None
    if stored is None:
        return None
    config, enabled = stored
    return config if enabled else None


def set_enabled(tenant_id: str, enabled: bool) -> dict:
    """Turn BYOK off (back to Meter's endpoint) without discarding it."""
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        conn.execute("UPDATE discovery_llm SET enabled = %s, updated_at = now()", (enabled,))
    return status(tenant_id)


def remove(tenant_id: str) -> dict:
    """Delete the configuration and its stored key outright."""
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        conn.execute("DELETE FROM discovery_llm")
    return status(tenant_id)


def test_connection(
    tenant_id: str,
    *,
    provider: Optional[str] = None,
    base_url: str = "",
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    client: Optional[httpx.Client] = None,
) -> dict:
    """Check a configuration against the provider. Never raises; never leaks the key.

    Tests what was typed when a key is supplied, so the user can verify BEFORE
    saving; otherwise tests what is stored. The probe is a real one-token
    completion, so it exercises the endpoint, the key AND the model name —
    a credential that is valid for a model the tenant cannot access still fails
    here, which is the point.
    """
    if api_key:
        url, name = _validate(provider or "custom", base_url, model or "")
        config = LlmConfig(base_url=url, model=name, api_key=api_key.strip())
    else:
        stored = _stored_config(tenant_id)
        if stored is None:
            return {"ok": False, "error": "No LLM configuration saved yet."}
        config = stored[0]

    owns = client is None
    client = client or httpx.Client(timeout=TEST_TIMEOUT)
    try:
        resp = client.post(
            f"{config.base_url.rstrip('/')}/chat/completions",
            json={
                "model": config.model,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            },
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.api_key}",
            },
        )
        if resp.status_code >= 400:
            detail = redact(resp.text[:300], config.api_key)
            return {
                "ok": False,
                "error": f"{resp.status_code}: {detail}" if detail else f"HTTP {resp.status_code}",
            }
        return {"ok": True, "model": config.model}
    except Exception as exc:
        return {"ok": False, "error": redact(str(exc)[:300], config.api_key)}
    finally:
        if owns:
            client.close()
