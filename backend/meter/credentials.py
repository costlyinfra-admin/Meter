"""Per-tenant connector credentials — encrypted at rest.

Secrets are encrypted (crypto.encrypt) before insert; only ciphertext is stored.
All access goes through the app role with tenant context, so RLS guarantees a
tenant can only ever touch its own credentials.

In M2 these are stored but not yet *used* — the connectors that consume them
(GitHub in M3, providers in M4) come later. The wizard's "Connect" step is a
shell with empty states; this is the storage capability behind it.
"""

from __future__ import annotations

from typing import Optional

from typing_extensions import TypedDict  # pydantic needs this on Python < 3.12

from .crypto import decrypt, encrypt
from .db import app_dsn, connect, tenant_tx

#: Connectors offered in the onboarding wizard. category drives how they're
#: grouped in the UI; "build_activity" feeds build cost, "inference" feeds run cost,
#: "infrastructure" feeds cloud cost.
KNOWN_CONNECTORS = [
    # "features": powers feature discovery (the spine). The same credential also
    # serves the Copilot seat sync on the build side; category is presentational.
    {"type": "github", "name": "GitHub", "category": "features"},
    {"type": "anthropic", "name": "Anthropic", "category": "inference"},
    {"type": "openai", "name": "OpenAI", "category": "inference"},
    {"type": "google", "name": "Google Gemini", "category": "inference"},
    # Hosted open-source aggregators (per-token billing, OpenAI-compatible).
    {"type": "openrouter", "name": "OpenRouter", "category": "inference"},
    {"type": "together", "name": "Together AI", "category": "inference"},
    {"type": "fireworks", "name": "Fireworks AI", "category": "inference"},
    # Cloud-cost connectors: spend lives in the cloud bill, read via its cost API.
    {"type": "bedrock", "name": "Amazon Bedrock (AWS cost)", "category": "inference"},
    {"type": "azure", "name": "Azure OpenAI (Azure cost)", "category": "inference"},
    # Gateways/proxies that aggregate spend across many providers.
    {"type": "litellm", "name": "LiteLLM (gateway)", "category": "inference"},
    {"type": "vercel", "name": "Vercel AI Gateway", "category": "inference"},
    # Compute platform (GPU time) and an audio-model provider.
    {"type": "modal", "name": "Modal (compute)", "category": "inference"},
    {"type": "elevenlabs", "name": "ElevenLabs (audio)", "category": "inference"},
    # Single-vendor inference APIs (OpenAI-compatible; priced per token).
    {"type": "groq", "name": "Groq", "category": "inference"},
    {"type": "mistral", "name": "Mistral AI", "category": "inference"},
    {"type": "xai", "name": "xAI (Grok)", "category": "inference"},
    {"type": "perplexity", "name": "Perplexity", "category": "inference"},
    {"type": "cohere", "name": "Cohere", "category": "inference"},
    {"type": "replicate", "name": "Replicate", "category": "inference"},
    # More gateways/proxies that aggregate spend across providers.
    {"type": "portkey", "name": "Portkey (gateway)", "category": "inference"},
    {"type": "helicone", "name": "Helicone (gateway)", "category": "inference"},
    # Cloud infrastructure: the whole bill, not just its model services. Ingested
    # by infrastructure.py, which classifies each line item and hands Bedrock's
    # dollars to the connector above rather than counting them twice.
    {"type": "aws", "name": "Amazon Web Services", "category": "infrastructure"},
    # "azure_cloud", not "azure": the latter is the Azure OpenAI connector above,
    # a different credential reading a narrow slice of the same Azure bill.
    {"type": "azure_cloud", "name": "Microsoft Azure", "category": "infrastructure"},
    {"type": "gcp", "name": "Google Cloud Platform", "category": "infrastructure"},
    # Managed platforms that publish a real billed figure (see infra_providers.py).
    {"type": "digitalocean", "name": "DigitalOcean", "category": "infrastructure"},
    {"type": "mongodb_atlas", "name": "MongoDB Atlas", "category": "infrastructure"},
    {"type": "cloudflare", "name": "Cloudflare", "category": "infrastructure"},
    {"type": "snowflake", "name": "Snowflake", "category": "infrastructure"},
    # "vercel_cloud", not "vercel": the latter is the AI Gateway connector above.
    {"type": "vercel_cloud", "name": "Vercel", "category": "infrastructure"},
    {"type": "cursor", "name": "Cursor for Teams", "category": "build_activity"},
    # Identity provider for SSO/SCIM seat rosters (Cursor, Tabnine, Cody, …).
    {"type": "okta", "name": "Okta (SSO seats)", "category": "build_activity"},
    {"type": "entra", "name": "Microsoft Entra ID (SSO seats)", "category": "build_activity"},
]
_KNOWN_TYPES = {c["type"] for c in KNOWN_CONNECTORS}


class ConnectorStatus(TypedDict):
    type: str
    name: str
    category: str
    connected: bool
    #: When the stored secret was last written, or None when there is none. The
    #: secret itself is never exposed — this is the only fact about it that
    #: leaves the server.
    credential_set_at: Optional[str]


def save_credential(
    tenant_id: str, connector_type: str, secret: str, label: Optional[str] = None
) -> None:
    """Encrypt and store a connector secret, replacing any the tenant already had.

    Saving the same connector twice is how a token is rotated, and rotation is
    usually a response to a token being leaked or over-scoped. Keeping the old
    ciphertext would defeat that: the compromised secret would stay in the
    database, decryptable with the same key, for as long as the tenant existed.
    So the previous rows go, in the same transaction that writes the new one —
    a connector has exactly one credential, which is what every reader here has
    always assumed by taking the newest row.
    """
    if connector_type not in _KNOWN_TYPES:
        raise ValueError(f"Unknown connector type: {connector_type}")
    if not secret or not secret.strip():
        raise ValueError("A credential cannot be empty.")
    ciphertext = encrypt(secret)
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        new_id = conn.execute(
            """
            INSERT INTO connector_credential (tenant_id, connector_type, label, ciphertext)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (tenant_id, connector_type, label, ciphertext),
        ).fetchone()[0]
        conn.execute(
            "DELETE FROM connector_credential WHERE connector_type = %s AND id <> %s",
            (connector_type, new_id),
        )


def get_secret(tenant_id: str, connector_type: str) -> Optional[str]:
    """Decrypt and return the most recently stored secret for a connector."""
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        row = conn.execute(
            """
            SELECT ciphertext FROM connector_credential
            WHERE connector_type = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (connector_type,),
        ).fetchone()
    if row is None:
        return None
    return decrypt(row[0])


def connector_statuses(tenant_id: str) -> list[ConnectorStatus]:
    """Every known connector, with whether the tenant has connected it and when.

    `credential_set_at` is the only thing this says about a stored secret. It is
    what makes rotation checkable — "this key was set fourteen months ago" is
    actionable, and it reveals nothing. The secret itself is never returned by
    any route, not masked and not partially: the plaintext exists only inside a
    sync, and there is no read path back to the browser.
    """
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        rows = conn.execute(
            "SELECT connector_type, MAX(created_at) FROM connector_credential "
            "GROUP BY connector_type"
        ).fetchall()
    set_at = {r[0]: r[1] for r in rows}
    return [
        {
            "type": c["type"],
            "name": c["name"],
            "category": c["category"],
            "connected": c["type"] in set_at,
            "credential_set_at": (set_at[c["type"]].isoformat() if set_at.get(c["type"]) else None),
        }
        for c in KNOWN_CONNECTORS
    ]
