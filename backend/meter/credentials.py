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

#: Categories whose sync reads EVERY stored credential rather than the newest.
#: Holding two keys is only useful where both are actually fetched, so this is
#: the list of places that do — and the server refuses a second credential
#: anywhere else, rather than accepting one and quietly never reading it.
#:
#: Each of these fetches from every credential BEFORE writing anything, because
#: all three ingest paths are idempotent by clearing a period and rewriting it:
#: persisting per account would have the second account's write delete the
#: first's. See run_inference_ingest, infrastructure.sync_window,
#: claudecode/cursorspend import, and seats.sync_idp_seats.
#:
#: `build_activity` covers Cursor and the SSO seat directories. GitHub is not
#: here: discovery reads one token and scans one owner, so a second would change
#: what is scanned rather than add to it — that is the multi-owner question,
#: not this one.
MULTI_CREDENTIAL_CATEGORIES = {"inference", "infrastructure", "build_activity"}
_MULTI_TYPES = {c["type"] for c in KNOWN_CONNECTORS if c["category"] in MULTI_CREDENTIAL_CATEGORIES}


def supports_multiple(connector_type: str) -> bool:
    """Whether this connector's sync reads more than one credential."""
    return connector_type in _MULTI_TYPES


class ConnectorStatus(TypedDict):
    type: str
    name: str
    category: str
    connected: bool
    #: When the stored secret was last written, or None when there is none. The
    #: secret itself is never exposed — this is the only fact about it that
    #: leaves the server.
    credential_set_at: Optional[str]
    #: How many credentials this connector holds. A tenant with two billing
    #: accounts under one provider has two.
    credential_count: int
    #: Whether a second credential would actually be read. False means saving
    #: one replaces the existing credential rather than adding to it.
    supports_multiple: bool


def save_credential(
    tenant_id: str,
    connector_type: str,
    secret: str,
    label: Optional[str] = None,
    credential_id: Optional[str] = None,
) -> str:
    """Store a connector secret. Returns the credential's id.

    Two operations, deliberately one function, because they differ only in
    whether an existing credential is named:

    * ``credential_id`` given — REPLACE that credential. The ciphertext is
      overwritten in place, so the previous secret is gone rather than kept in
      a superseded row. Rotation is usually a response to a secret being
      exposed, and that is exactly the secret worth not keeping.
    * ``credential_id`` omitted — ADD another credential for this connector. A
      tenant can hold several: two Anthropic organisations billed separately are
      one Meter connector with two keys, and collapsing them would mean seeing
      only whichever synced last.
    """
    if connector_type not in _KNOWN_TYPES:
        raise ValueError(f"Unknown connector type: {connector_type}")
    if not secret or not secret.strip():
        raise ValueError("A credential cannot be empty.")
    ciphertext = encrypt(secret)
    label = (label or "").strip()[:120] or None
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        if credential_id is None and not supports_multiple(connector_type):
            # This connector's sync reads one credential. Storing a second would
            # accept a key and then never fetch with it, which is worse than
            # saying so: the customer would believe both accounts were billed.
            existing = conn.execute(
                "SELECT id FROM connector_credential WHERE connector_type = %s ORDER BY created_at",
                (connector_type,),
            ).fetchall()
            if existing:
                credential_id = str(existing[0][0])
        if credential_id is not None:
            row = conn.execute(
                """
                UPDATE connector_credential
                SET ciphertext = %s,
                    label = COALESCE(%s, label),
                    updated_at = now()
                WHERE id = %s AND connector_type = %s
                RETURNING id
                """,
                (ciphertext, label, credential_id, connector_type),
            ).fetchone()
            if row is None:
                raise ValueError("That credential no longer exists.")
            return str(row[0])
        return str(
            conn.execute(
                """
                INSERT INTO connector_credential (tenant_id, connector_type, label, ciphertext)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (tenant_id, connector_type, label, ciphertext),
            ).fetchone()[0]
        )


def list_credentials(tenant_id: str, connector_type: str) -> list[dict]:
    """Every credential stored for a connector — identity and dates, never secrets.

    This is what lets a person tell two accounts apart well enough to replace
    the right one. The ciphertext is not selected here at all, so there is no
    path by which it could reach a response by accident.
    """
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        rows = conn.execute(
            """
            SELECT id, label, created_at, updated_at
            FROM connector_credential
            WHERE connector_type = %s
            ORDER BY created_at
            """,
            (connector_type,),
        ).fetchall()
    return [
        {
            "id": str(cid),
            "label": label,
            "created_at": created.isoformat() if created else None,
            "updated_at": updated.isoformat() if updated else None,
        }
        for cid, label, created, updated in rows
    ]


def delete_credential(tenant_id: str, connector_type: str, credential_id: str) -> bool:
    """Remove one credential. Returns whether it existed.

    Cost already attributed to it stays: what a month cost is a fact about the
    month, and removing the key Meter read it with does not un-spend the money.
    """
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        row = conn.execute(
            "DELETE FROM connector_credential WHERE id = %s AND connector_type = %s RETURNING id",
            (credential_id, connector_type),
        ).fetchone()
    return row is not None


def get_secrets(tenant_id: str, connector_type: str) -> list[tuple[str, str]]:
    """Every (credential_id, secret) for a connector, oldest first.

    The plural form. A sync that reads only the newest would silently bill a
    customer for one of their two accounts, so anything fetching real money
    should use this and fetch from all of them.
    """
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        rows = conn.execute(
            """
            SELECT id, ciphertext FROM connector_credential
            WHERE connector_type = %s
            ORDER BY created_at
            """,
            (connector_type,),
        ).fetchall()
    return [(str(cid), decrypt(bytes(blob))) for cid, blob in rows]


def get_secret(tenant_id: str, connector_type: str) -> Optional[str]:
    """The most recently stored secret for a connector, or None.

    Singular, and therefore only right where one credential is genuinely all
    that is meant — listing an org's repositories, say. Anything that reads
    billed cost wants `get_secrets`, because a tenant may have several accounts
    and reading one of them would understate their bill.
    """
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
            "SELECT connector_type, MAX(created_at), count(*) FROM connector_credential "
            "GROUP BY connector_type"
        ).fetchall()
    set_at = {r[0]: r[1] for r in rows}
    counts = {r[0]: int(r[2]) for r in rows}
    return [
        {
            "type": c["type"],
            "name": c["name"],
            "category": c["category"],
            "connected": c["type"] in set_at,
            "credential_set_at": (set_at[c["type"]].isoformat() if set_at.get(c["type"]) else None),
            # How many accounts are connected under this one source, so a tenant
            # billed through two organisations can see both are being read.
            "credential_count": counts.get(c["type"], 0),
            # Whether this connector's sync actually reads more than one key.
            "supports_multiple": supports_multiple(c["type"]),
        }
        for c in KNOWN_CONNECTORS
    ]
