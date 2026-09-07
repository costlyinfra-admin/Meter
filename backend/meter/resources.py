"""User-controlled resource classification — the shared model for every source.

A "resource" is a provider sub-resource that spend can attribute to: an Anthropic
API key, an OpenAI project, a Bedrock account, a gateway route, a build-cost seat,
etc. Each is identified stably by (tenant_id, provider, resource_type, resource_id)
and carries a MANUAL classification:

    production | development | internal | ignore | unclassified

No naming convention ever sets a classification — the user does. Newly discovered
resources default to ``unclassified``; a sync registers/refreshes their names and
hierarchy but must NEVER overwrite a classification a person chose.
"""

from __future__ import annotations

from typing import Optional

from .db import app_dsn, connect, tenant_tx

CLASSIFICATIONS = ("production", "development", "internal", "ignore", "unclassified")
DEFAULT_CLASSIFICATION = "unclassified"

#: Classifications excluded from normal reporting/optimization totals.
EXCLUDED_FROM_REPORTING = ("ignore",)


class ResourceError(ValueError):
    """Invalid resource-classification input (maps to HTTP 400)."""


def register_resources(tenant_id: str, provider: str, resources: list[dict]) -> None:
    """Register resources discovered by a sync (idempotent, classification-preserving).

    Each item: {resource_type, resource_id, resource_name?, parent_resource_id?}.
    New resources are inserted as ``unclassified``; existing rows have their name /
    parent / last_seen refreshed but their ``classification`` is left untouched, so a
    sync can never clobber a manual choice — and a resource that disappears and later
    reappears keeps its mapping.
    """
    rows = [r for r in resources if r.get("resource_type") and r.get("resource_id")]
    if not rows:
        return
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        for r in rows:
            conn.execute(
                """
                INSERT INTO resource_classification
                    (tenant_id, provider, resource_type, resource_id,
                     resource_name, parent_resource_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, provider, resource_type, resource_id)
                DO UPDATE SET
                    resource_name = EXCLUDED.resource_name,
                    parent_resource_id = EXCLUDED.parent_resource_id,
                    last_seen = now()
                """,
                (
                    tenant_id,
                    provider,
                    r["resource_type"],
                    r["resource_id"],
                    r.get("resource_name"),
                    r.get("parent_resource_id"),
                ),
            )


def get_classifications(tenant_id: str, provider: str) -> dict[tuple[str, str], str]:
    """Map ``(resource_type, resource_id) -> classification`` for a provider."""
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        rows = conn.execute(
            """
            SELECT resource_type, resource_id, classification
            FROM resource_classification WHERE provider = %s
            """,
            (provider,),
        ).fetchall()
    return {(rt, rid): cls for rt, rid, cls in rows}


def list_resources(tenant_id: str, provider: str) -> list[dict]:
    """All registered resources for a provider (config only, no cost)."""
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        rows = conn.execute(
            """
            SELECT resource_type, resource_id, resource_name, parent_resource_id, classification
            FROM resource_classification WHERE provider = %s
            ORDER BY resource_type, resource_name NULLS LAST, resource_id
            """,
            (provider,),
        ).fetchall()
    return [
        {
            "resource_type": rt,
            "resource_id": rid,
            "resource_name": name,
            "parent_resource_id": parent,
            "classification": cls,
        }
        for rt, rid, name, parent, cls in rows
    ]


def set_classification(
    tenant_id: str,
    provider: str,
    resource_type: str,
    resource_id: str,
    classification: str,
    *,
    resource_name: Optional[str] = None,
    updated_by: Optional[str] = None,
) -> dict:
    """Set a resource's classification (the manual source of truth). Upserts the row."""
    if classification not in CLASSIFICATIONS:
        raise ResourceError(f"Invalid classification: {classification!r}.")
    if not resource_type or not resource_id:
        raise ResourceError("resource_type and resource_id are required.")
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        conn.execute(
            """
            INSERT INTO resource_classification
                (tenant_id, provider, resource_type, resource_id, resource_name,
                 classification, updated_by, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (tenant_id, provider, resource_type, resource_id)
            DO UPDATE SET
                classification = EXCLUDED.classification,
                resource_name = COALESCE(EXCLUDED.resource_name,
                                         resource_classification.resource_name),
                updated_by = EXCLUDED.updated_by,
                updated_at = now()
            """,
            (
                tenant_id,
                provider,
                resource_type,
                resource_id,
                resource_name,
                classification,
                updated_by,
            ),
        )
        # Re-stamp the environment on ALREADY-persisted cost rows for this resource,
        # across every month — otherwise the classification only shows on the
        # Cost Sources table (which reads config live) but the Overview trend keeps
        # bucketing the spend by its stale ingest-time snapshot until the next sync.
        # A key row is keyed by api_key_id; a workspace-only (no-key) row by
        # workspace_id — matching how ingest stamped each.
        if resource_type == "api_key":
            where, ident = "api_key_id = %s", resource_id
        elif resource_type == "workspace":
            where, ident = "workspace_id = %s AND api_key_id IS NULL", resource_id
        else:
            where = None
        if where is not None:
            for table in ("inference_cost", "inference_cost_daily"):
                conn.execute(
                    f"UPDATE {table} SET environment = %s WHERE provider = %s AND {where}",  # noqa: S608
                    (classification, provider, ident),
                )
    return {
        "provider": provider,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "classification": classification,
    }
