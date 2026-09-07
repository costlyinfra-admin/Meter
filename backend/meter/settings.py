"""Tenant (organization) settings — the administrative Settings page.

Everything here is tenant-scoped: every user in a tenant reads and writes the same
values (they live on the `tenant` row). Access goes through the admin/owner
connection filtered by the caller's own ``tenant_id`` — the same trusted-identity
pattern auth uses — so a tenant can only ever touch its own organization.

Validation is strict and centralized: invalid values raise ``SettingsError`` (a
``ValueError``) which the API maps to HTTP 400.
"""

from __future__ import annotations

from .db import admin_dsn, connect

MAX_ORG_NAME = 200

#: Reporting currency. USD-only today; the column/list is where more would go.
SUPPORTED_CURRENCIES = ("USD",)

#: How customer identifiers will be stored once customer attribution ships.
CUSTOMER_ID_STORAGE = ("names", "aliases", "hashed")

#: Retention windows. Enforcement is deferred (see the migration); this is the
#: persisted intent only.
DATA_RETENTION = ("30d", "90d", "1y", "indefinite")

#: Curated set of IANA time zones offered in Settings. A closed list keeps
#: validation deterministic and dependency-free (no tzdata requirement) while
#: staying a standard IANA value.
SUPPORTED_TIMEZONES = (
    "UTC",
    "America/Los_Angeles",
    "America/Denver",
    "America/Chicago",
    "America/New_York",
    "America/Sao_Paulo",
    "Europe/London",
    "Europe/Paris",
    "Europe/Berlin",
    "Europe/Madrid",
    "Africa/Johannesburg",
    "Asia/Dubai",
    "Asia/Kolkata",
    "Asia/Singapore",
    "Asia/Shanghai",
    "Asia/Tokyo",
    "Australia/Sydney",
    "Pacific/Auckland",
)


class SettingsError(ValueError):
    """Invalid settings input (maps to HTTP 400)."""


def get_settings(tenant_id: str) -> dict:
    """The tenant's organization + privacy settings (with safe defaults applied)."""
    with connect(admin_dsn()) as conn:
        row = conn.execute(
            """
            SELECT name, timezone, currency, customer_id_storage, store_prompts, data_retention
            FROM tenant WHERE id = %s
            """,
            (tenant_id,),
        ).fetchone()
    if row is None:
        raise SettingsError("Organization not found.")
    return {
        "org_name": row[0],
        "timezone": row[1],
        "currency": row[2],
        "customer_id_storage": row[3],
        "store_prompts": row[4],
        "data_retention": row[5],
    }


def update_settings(tenant_id: str, changes: dict) -> dict:
    """Validate and persist only the fields present in ``changes``; return the row.

    A missing key leaves that setting untouched (partial update). Invalid values
    raise ``SettingsError`` and nothing is written.
    """
    columns: list[str] = []
    params: list = []

    if "org_name" in changes:
        name = (changes["org_name"] or "").strip()
        if not name:
            raise SettingsError("Organization name cannot be empty.")
        if len(name) > MAX_ORG_NAME:
            raise SettingsError(f"Organization name must be at most {MAX_ORG_NAME} characters.")
        columns.append("name = %s")
        params.append(name)

    if "timezone" in changes:
        tz = (changes["timezone"] or "").strip()
        if tz not in SUPPORTED_TIMEZONES:
            raise SettingsError(f"Unsupported time zone: {tz!r}.")
        columns.append("timezone = %s")
        params.append(tz)

    if "currency" in changes:
        currency = (changes["currency"] or "").strip().upper()
        if currency not in SUPPORTED_CURRENCIES:
            raise SettingsError(f"Unsupported currency: {currency!r}.")
        columns.append("currency = %s")
        params.append(currency)

    if "customer_id_storage" in changes:
        mode = changes["customer_id_storage"]
        if mode not in CUSTOMER_ID_STORAGE:
            raise SettingsError(f"Invalid customer identifier mode: {mode!r}.")
        columns.append("customer_id_storage = %s")
        params.append(mode)

    if "store_prompts" in changes:
        store = changes["store_prompts"]
        if not isinstance(store, bool):
            raise SettingsError("store_prompts must be true or false.")
        columns.append("store_prompts = %s")
        params.append(store)

    if "data_retention" in changes:
        retention = changes["data_retention"]
        if retention not in DATA_RETENTION:
            raise SettingsError(f"Invalid data retention window: {retention!r}.")
        columns.append("data_retention = %s")
        params.append(retention)

    if columns:
        params.append(tenant_id)
        with connect(admin_dsn()) as conn, conn.transaction():
            conn.execute(f"UPDATE tenant SET {', '.join(columns)} WHERE id = %s", params)  # noqa: S608
    return get_settings(tenant_id)
