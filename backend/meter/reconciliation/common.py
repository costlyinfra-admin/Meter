"""Shared plumbing for the reconciliation module: money, connections, audit.

Money is Decimal from the moment it is parsed to the moment it is written, and
the database columns are numeric — no float ever touches an amount. Floats
would introduce differences of exactly the size this module exists to explain,
which would make it worse than useless.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Optional

from ..db import app_dsn, connect, tenant_tx

#: Statement precision. Provider invoices carry unit prices and tax lines with
#: more decimals than the 4 used for tracked spend; rounding on the way in would
#: manufacture discrepancies.
CENTS = Decimal("0.000001")
#: What a person is shown and what a comparison is decided on.
MONEY = Decimal("0.01")

ZERO = Decimal("0")


def money(value: Any, default: Optional[Decimal] = ZERO) -> Optional[Decimal]:
    """Parse an amount from a statement cell. None when it is not a number.

    Accepts the shapes billing exports actually use: "$1,234.56", "(12.30)" for
    a negative, a trailing minus, and an empty cell.
    """
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    text = str(value).strip()
    if not text:
        return default
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    text = text.replace(",", "").replace("$", "").replace("USD", "").strip()
    if text.endswith("-"):
        negative, text = True, text[:-1]
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return default
    return -amount if negative else amount


def quantize(value: Decimal, exp: Decimal = CENTS) -> Decimal:
    """Round half-up, the convention for money — not banker's rounding."""
    return value.quantize(exp, rounding=ROUND_HALF_UP)


def pct(difference: Decimal, base: Decimal) -> Optional[Decimal]:
    """Difference as a percentage of the base. None when the base is zero —
    a percentage of nothing is not infinity, it is undefined, and saying so is
    more honest than showing a number."""
    if base == 0:
        return None
    return quantize(difference / base * 100, Decimal("0.0001"))


@contextmanager
def tenant_conn(tenant_id: str) -> Iterator:
    """A connection scoped to one tenant. Every query in this module uses it, so
    row-level security answers the isolation question rather than a WHERE clause
    somebody might forget."""
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id) as scoped:
        yield scoped


def record_audit(
    conn,
    tenant_id: str,
    event: str,
    *,
    actor: Optional[str] = None,
    import_id: Optional[str] = None,
    run_id: Optional[str] = None,
    detail: Optional[dict] = None,
) -> None:
    """Append one audit event. Never the file's contents — only what happened."""
    conn.execute(
        "INSERT INTO recon_audit (tenant_id, event, actor, import_id, run_id, detail) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (tenant_id, event, actor, import_id, run_id, json.dumps(detail or {})),
    )


def iso(value: Optional[dt.date]) -> Optional[str]:
    return value.isoformat() if value else None


def as_float(value: Optional[Decimal]) -> Optional[float]:
    """Decimal -> float, at the API boundary only.

    JSON has no decimal type, so a number has to become a float on the way out.
    Every calculation is finished by this point; nothing is computed from the
    result of this function.
    """
    return None if value is None else float(value)
