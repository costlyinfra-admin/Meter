"""Internal admin-portal services.

Cross-tenant reads go through the RLS-exempt owner connection (`admin_dsn`); every
per-customer detail reuses the existing per-tenant services (`optimize_measured`,
`credentials`, `inference`, `discovery`) — no business logic is duplicated. Access
is gated to admin users at the API layer (see `is_admin`); nothing here is reachable
by a normal tenant, and connector secrets are never returned (only encrypted at rest
via the existing `credentials`/`crypto` utilities).
"""

from __future__ import annotations

import datetime as dt
import os
from collections import defaultdict
from typing import Optional

from . import credentials, discovery, inference, infrastructure, optimize_measured
from .db import admin_dsn, connect
from .github import GitHubClient
from .providers import make_cost_client, month_start

# Connectors the portal can Test/Sync today (opt to start small, per the brief).
_ACTIONABLE_CONNECTORS = {"github", "anthropic"}


# --------------------------------------------------------------------------
# Access control (env allowlist — no schema change, reuses existing auth)
# --------------------------------------------------------------------------
def admin_emails() -> set[str]:
    raw = os.environ.get("METER_ADMIN_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def is_admin(email: str) -> bool:
    return email.strip().lower() in admin_emails()


# --------------------------------------------------------------------------
# Cross-tenant read model
# --------------------------------------------------------------------------
def _latest_spend_by_tenant(conn) -> dict[str, float]:
    """Each tenant's most-recent monthly inference spend (the current run rate)."""
    rows = conn.execute(
        "SELECT tenant_id, period, COALESCE(SUM(amount), 0) "
        "FROM inference_cost GROUP BY tenant_id, period"
    ).fetchall()
    latest_period: dict[str, dt.date] = {}
    spend: dict[str, float] = {}
    for tenant_id, period, amount in rows:
        tid = str(tenant_id)
        if tid not in latest_period or period > latest_period[tid]:
            latest_period[tid] = period
            spend[tid] = float(amount)
    return spend


def customers() -> list[dict]:
    """One row per tenant: company, status, connectors, last sync, spend, and the
    Copilot rollup (opportunities + verified savings) reused from copilot_overview."""
    with connect(admin_dsn()) as conn:
        tenants = conn.execute("SELECT id, name, created_at FROM tenant ORDER BY name").fetchall()
        connector_rows = conn.execute(
            "SELECT tenant_id, connector_type FROM connector_credential"
        ).fetchall()
        sync_rows = conn.execute(
            "SELECT tenant_id, MAX(finished_at) FROM admin_sync_log "
            "WHERE status = 'success' GROUP BY tenant_id"
        ).fetchall()
        spend = _latest_spend_by_tenant(conn)

    connectors: dict[str, set] = defaultdict(set)
    for tenant_id, ctype in connector_rows:
        connectors[str(tenant_id)].add(ctype)
    last_sync = {str(t): fin for t, fin in sync_rows}

    out = []
    for tid, name, created_at in tenants:
        tid = str(tid)
        overview = optimize_measured.copilot_overview(tid)
        provs = sorted(connectors.get(tid, set()))
        out.append(
            {
                "tenant_id": tid,
                "company": name,
                "created_at": created_at.isoformat() if created_at else None,
                "status": "connected" if provs else "pending",
                "connected_providers": provs,
                "last_sync": last_sync[tid].isoformat() if last_sync.get(tid) else None,
                "monthly_spend": round(spend.get(tid, 0.0), 2),
                "opportunities": sum(lever["count"] for lever in overview["by_lever"]),
                "verified_savings": overview["verified_monthly_savings"],
            }
        )
    return out


def overview() -> dict:
    """Portal dashboard KPIs — aggregated straight from the customer rollup."""
    custs = customers()
    connected = [c for c in custs if c["status"] == "connected"]
    return {
        "total_customers": len(custs),
        "connected_customers": len(connected),
        "pending_connections": len(custs) - len(connected),
        "total_ai_spend": round(sum(c["monthly_spend"] for c in custs), 2),
        "total_opportunities": sum(c["opportunities"] for c in custs),
        "total_verified_savings": round(sum(c["verified_savings"] for c in custs) * 12, 2),
    }


def company_name(tenant_id: str) -> Optional[str]:
    with connect(admin_dsn()) as conn:
        row = conn.execute("SELECT name FROM tenant WHERE id = %s", (tenant_id,)).fetchone()
    return row[0] if row else None


def customer_detail(tenant_id: str) -> Optional[dict]:
    """Full support view for one customer. Returns None if the tenant doesn't exist."""
    with connect(admin_dsn()) as conn:
        tenant = conn.execute(
            "SELECT id, name, created_at FROM tenant WHERE id = %s", (tenant_id,)
        ).fetchone()
        if tenant is None:
            return None
        users = conn.execute(
            "SELECT email FROM app_user WHERE tenant_id = %s ORDER BY created_at", (tenant_id,)
        ).fetchall()
        # Repositories reached, parsed from the GitHub-sourced feature signals.
        repo_rows = conn.execute(
            "SELECT DISTINCT split_part(external_ref, '#', 1) FROM feature_signal "
            "WHERE tenant_id = %s AND source = 'github' AND external_ref LIKE '%%/%%'",
            (tenant_id,),
        ).fetchall()
        actions = conn.execute(
            "SELECT lever, applied_on, projected_monthly, created_at FROM optimization_action "
            "WHERE tenant_id = %s ORDER BY created_at DESC LIMIT 10",
            (tenant_id,),
        ).fetchall()
        syncs = _sync_rows(conn, tenant_id=tenant_id, limit=10)
        errors = _sync_rows(conn, tenant_id=tenant_id, status="error", limit=10)

    repos = sorted({r[0] for r in repo_rows if r[0] and "/" in r[0] and "*" not in r[0]})
    return {
        "tenant_id": str(tenant[0]),
        "company": tenant[1],
        "created_at": tenant[2].isoformat() if tenant[2] else None,
        "users": [u[0] for u in users],
        "connectors": credentials.connector_statuses(tenant_id),
        "repositories": repos,
        "optimization_runs": [
            {
                "lever": lever,
                "applied_on": applied_on.isoformat(),
                "projected_monthly": float(projected),
                "created_at": created.isoformat(),
            }
            for lever, applied_on, projected, created in actions
        ],
        "recent_syncs": syncs,
        "recent_errors": errors,
    }


# --------------------------------------------------------------------------
# Connector operations (Test / Sync / Disconnect) — reuse existing services
# --------------------------------------------------------------------------
def _log_sync(
    tenant_id: str,
    connector_type: str,
    action: str,
    status: str,
    *,
    records: Optional[int] = None,
    error: Optional[str] = None,
) -> dict:
    with connect(admin_dsn(), autocommit=True) as conn:
        row = conn.execute(
            """
            INSERT INTO admin_sync_log
                (tenant_id, connector_type, action, finished_at, records_imported,
                 status, error_message)
            VALUES (%s, %s, %s, now(), %s, %s, %s)
            RETURNING id, started_at, finished_at
            """,
            (tenant_id, connector_type, action, records, status, (error or None)),
        ).fetchone()
    return {
        "status": status,
        "records_imported": records,
        "error_message": error,
        "started_at": row[1].isoformat(),
        "finished_at": row[2].isoformat(),
    }


def _credential_label(tenant_id: str, connector_type: str) -> Optional[str]:
    with connect(admin_dsn()) as conn:
        row = conn.execute(
            "SELECT label FROM connector_credential WHERE tenant_id = %s AND connector_type = %s "
            "ORDER BY created_at DESC LIMIT 1",
            (tenant_id, connector_type),
        ).fetchone()
    return row[0] if row and row[0] else None


def test_connection(tenant_id: str, connector_type: str) -> dict:
    """Validate the stored credential against the provider. Logs the outcome."""
    secret = credentials.get_secret(tenant_id, connector_type)
    if not secret:
        return _log_sync(tenant_id, connector_type, "test", "error", error="No credential stored.")
    try:
        if connector_type == "github":
            with GitHubClient(secret) as gh:
                repos = gh._list_accessible_repos()
            return _log_sync(tenant_id, connector_type, "test", "success", records=len(repos))
        if connector_type in infrastructure.LIVE_PROVIDERS:
            # A one-day window: enough for AWS to accept or reject the credential,
            # without paying for a full bill read just to test it.
            today = dt.date.today()
            with infrastructure.make_client(connector_type, secret) as client:
                items = client.fetch_items(today - dt.timedelta(days=1), today)
            return _log_sync(tenant_id, connector_type, "test", "success", records=len(items))
        with make_cost_client(connector_type, secret) as client:
            client.fetch_costs(month_start(dt.date.today()))
        return _log_sync(tenant_id, connector_type, "test", "success")
    except Exception as exc:  # provider/network/auth error — never crash the portal
        return _log_sync(tenant_id, connector_type, "test", "error", error=str(exc)[:500])


def sync_now(tenant_id: str, connector_type: str) -> dict:
    """Run a real ingest for the connector and log records imported."""
    secret = credentials.get_secret(tenant_id, connector_type)
    if not secret:
        return _log_sync(tenant_id, connector_type, "sync", "error", error="No credential stored.")
    try:
        if connector_type == "github":
            owner = _credential_label(tenant_id, "github")
            if not owner:
                return _log_sync(
                    tenant_id,
                    connector_type,
                    "sync",
                    "error",
                    error="Set the GitHub org/owner as the credential label to sync.",
                )
            result = discovery.run_discovery(tenant_id, owner, secret)
            records = int(result.get("proposals", 0))
        elif connector_type in infrastructure.LIVE_PROVIDERS:
            # Infrastructure connectors read a cloud bill, not a model provider's
            # cost API, so they must not go down the inference path below.
            result = infrastructure.run_infra_sync(tenant_id, connector_type, secret)
            records = int(result.get("items", 0))
        else:
            # Manual sync backfills a year of history (not just the current month).
            result = inference.run_inference_backfill(tenant_id, connector_type, secret, months=12)
            records = int(result.get("rows", 0))
        return _log_sync(tenant_id, connector_type, "sync", "success", records=records)
    except Exception as exc:
        return _log_sync(tenant_id, connector_type, "sync", "error", error=str(exc)[:500])


def disconnect(tenant_id: str, connector_type: str) -> None:
    """Remove a tenant's stored credential for a connector (owner connection)."""
    with connect(admin_dsn(), autocommit=True) as conn:
        conn.execute(
            "DELETE FROM connector_credential WHERE tenant_id = %s AND connector_type = %s",
            (tenant_id, connector_type),
        )


# --------------------------------------------------------------------------
# Sync history & errors
# --------------------------------------------------------------------------
def _sync_rows(conn, *, tenant_id: Optional[str] = None, status: Optional[str] = None, limit=100):
    clauses, params = [], []
    if tenant_id:
        clauses.append("l.tenant_id = %s")
        params.append(tenant_id)
    if status:
        clauses.append("l.status = %s")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT l.tenant_id, t.name, l.connector_type, l.action, l.started_at, l.finished_at,
               l.records_imported, l.status, l.error_message
        FROM admin_sync_log l JOIN tenant t ON t.id = l.tenant_id
        {where}
        ORDER BY l.started_at DESC LIMIT %s
        """,
        tuple(params),
    ).fetchall()
    out = []
    for tid, name, ctype, action, started, finished, records, status_, error in rows:
        duration_ms = round((finished - started).total_seconds() * 1000) if finished else None
        out.append(
            {
                "tenant_id": str(tid),
                "company": name,
                "connector_type": ctype,
                "action": action,
                "started_at": started.isoformat(),
                "finished_at": finished.isoformat() if finished else None,
                "duration_ms": duration_ms,
                "records_imported": records,
                "status": status_,
                "error_message": error,
            }
        )
    return out


def sync_history(limit: int = 100) -> list[dict]:
    with connect(admin_dsn()) as conn:
        return _sync_rows(conn, limit=limit)


def errors(limit: int = 100) -> list[dict]:
    with connect(admin_dsn()) as conn:
        return _sync_rows(conn, status="error", limit=limit)
