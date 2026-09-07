"""The feature switch and tolerance settings.

Off by default, per tenant. An installation that never opens this page has no
reconciliation navigation, no reachable routes and no work being done — the
row does not even exist until someone turns it on.

METER_RECONCILIATION=off is an operator kill switch on top: it disables the
module for every tenant without touching a single stored row, so turning it
back on restores exactly what was there.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Optional

from .common import record_audit, tenant_conn

#: Conservative defaults: a dollar, or half a percent. Both are applied.
DEFAULT_TOLERANCE_ABS = Decimal("1.000000")
DEFAULT_TOLERANCE_PCT = Decimal("0.500")

MAX_TOLERANCE_ABS = Decimal("100000")
MAX_TOLERANCE_PCT = Decimal("100")


class ReconciliationError(ValueError):
    """Invalid reconciliation input (maps to HTTP 400)."""


def globally_enabled() -> bool:
    """The operator's kill switch. On unless explicitly turned off."""
    return os.environ.get("METER_RECONCILIATION", "on").strip().lower() not in (
        "0",
        "off",
        "false",
        "no",
    )


def settings(tenant_id: str) -> dict:
    """This tenant's switch and tolerances, with defaults for a tenant that has
    never configured it. Never raises: a settings read must not be able to break
    a page that merely asks whether to show a nav item."""
    if not globally_enabled():
        return _defaults(enabled=False, available=False)
    try:
        with tenant_conn(tenant_id) as conn:
            row = conn.execute(
                "SELECT enabled, tolerance_abs, tolerance_pct, updated_at, updated_by "
                "FROM recon_settings"
            ).fetchone()
    except Exception:
        # A module that cannot read its own settings must still not take down
        # the shell that asked. It reports itself unavailable.
        return _defaults(enabled=False, available=False)
    if row is None:
        return _defaults(enabled=False, available=True)
    return {
        "available": True,
        "enabled": bool(row[0]),
        "tolerance_abs": float(row[1]),
        "tolerance_pct": float(row[2]),
        "updated_at": row[3].isoformat() if row[3] else None,
        "updated_by": row[4],
    }


def _defaults(*, enabled: bool, available: bool) -> dict:
    return {
        "available": available,
        "enabled": enabled,
        "tolerance_abs": float(DEFAULT_TOLERANCE_ABS),
        "tolerance_pct": float(DEFAULT_TOLERANCE_PCT),
        "updated_at": None,
        "updated_by": None,
    }


def is_enabled(tenant_id: str) -> bool:
    return bool(settings(tenant_id)["enabled"])


def tolerances(tenant_id: str) -> tuple:
    current = settings(tenant_id)
    return Decimal(str(current["tolerance_abs"])), Decimal(str(current["tolerance_pct"]))


def update(
    tenant_id: str,
    *,
    enabled: Optional[bool] = None,
    tolerance_abs: Optional[Decimal] = None,
    tolerance_pct: Optional[Decimal] = None,
    actor: Optional[str] = None,
) -> dict:
    """Turn the module on or off, or change its tolerances.

    Switching it off hides the module and stops all work; it deletes nothing, so
    the imports and runs are all still there when it is switched back on.
    """
    if not globally_enabled():
        raise ReconciliationError("Reconciliation is disabled for this installation.")
    if tolerance_abs is not None and not (0 <= tolerance_abs <= MAX_TOLERANCE_ABS):
        raise ReconciliationError(f"Absolute tolerance must be between 0 and {MAX_TOLERANCE_ABS}.")
    if tolerance_pct is not None and not (0 <= tolerance_pct <= MAX_TOLERANCE_PCT):
        raise ReconciliationError("Percentage tolerance must be between 0 and 100.")

    with tenant_conn(tenant_id) as conn:
        conn.execute(
            """
            INSERT INTO recon_settings (tenant_id, enabled, tolerance_abs, tolerance_pct,
                                        updated_by)
            VALUES (%s, COALESCE(%s, false), COALESCE(%s, %s), COALESCE(%s, %s), %s)
            ON CONFLICT (tenant_id) DO UPDATE SET
                enabled       = COALESCE(%s, recon_settings.enabled),
                tolerance_abs = COALESCE(%s, recon_settings.tolerance_abs),
                tolerance_pct = COALESCE(%s, recon_settings.tolerance_pct),
                updated_at    = now(),
                updated_by    = %s
            """,
            (
                tenant_id,
                enabled,
                tolerance_abs,
                DEFAULT_TOLERANCE_ABS,
                tolerance_pct,
                DEFAULT_TOLERANCE_PCT,
                actor,
                enabled,
                tolerance_abs,
                tolerance_pct,
                actor,
            ),
        )
        record_audit(
            conn,
            tenant_id,
            "settings_changed",
            actor=actor,
            detail={
                "enabled": enabled,
                "tolerance_abs": str(tolerance_abs) if tolerance_abs is not None else None,
                "tolerance_pct": str(tolerance_pct) if tolerance_pct is not None else None,
            },
        )
    return settings(tenant_id)
