"""The read-only boundary onto the cost data Meter already tracks.

This is the ONLY place the reconciliation module touches existing cost tables,
and every statement in it is a SELECT. Nothing here updates, annotates,
reclassifies or deletes a tracked row, and no existing query depends on
anything in this module.

Why inference_cost_daily rather than inference_cost: the daily table carries the
dimensions a statement can actually be matched on — the day, the model, the
provider workspace and the API key — where the monthly rollup carries only the
month. Summing a month of daily rows equals the monthly row (the existing
rollup test enforces that), so reading the finer table does not read a
different number, only a more detailed one.

Connector spend only (``cost_api``/``cost_api_est``). Hook-metered spend is a
second observation of the same calls and would double-count against a bill, and
self-hosted spend is not on a provider statement at all.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Optional

from .common import ZERO

#: The sources that represent what the provider will bill for.
BILLABLE_SOURCES = ("cost_api", "cost_api_est")


_AGGREGATE_SQL = """
    SELECT day, model, api_key_id, api_key_ref, workspace_id, currency,
           SUM(amount)                                              AS amount,
           SUM(COALESCE(tokens_in, 0) + COALESCE(tokens_out, 0))    AS tokens,
           COUNT(*)                                                 AS row_count
      FROM inference_cost_daily
     WHERE provider = %s AND day >= %s AND day <= %s
       AND source = ANY(%s)
       AND (%s::text IS NULL OR COALESCE(workspace_id, '') = %s)
     GROUP BY day, model, api_key_id, api_key_ref, workspace_id, currency
"""


def _rows(conn, provider: str, start: dt.date, end: dt.date, account: Optional[str]):
    return conn.execute(
        _AGGREGATE_SQL, (provider, start, end, list(BILLABLE_SOURCES), account, account)
    ).fetchall()


def aggregates(
    conn, provider: str, start: dt.date, end: dt.date, account: Optional[str] = None
) -> list[dict]:
    """Tracked spend for a provider and period, grouped on the dimensions a
    provider statement can be matched against. Read-only."""
    out = []
    for row in _rows(conn, provider, start, end, account):
        out.append(
            {
                "day": row[0],
                "model": row[1],
                "api_key_id": row[2],
                "api_key_ref": row[3],
                "workspace_id": row[4],
                "currency": row[5] or "USD",
                "amount": Decimal(str(row[6] or 0)),
                "tokens": int(row[7] or 0),
                "rows": int(row[8] or 0),
            }
        )
    return out


def total(
    conn, provider: str, start: dt.date, end: dt.date, account: Optional[str] = None
) -> Decimal:
    """The single number the provider's usage subtotal is compared against."""
    return sum((a["amount"] for a in aggregates(conn, provider, start, end, account)), ZERO)


def currencies(conn, provider: str, start: dt.date, end: dt.date) -> list[str]:
    """Distinct currencies in the tracked data, so a mismatch can be reported
    rather than silently converted."""
    rows = conn.execute(
        "SELECT DISTINCT COALESCE(currency, 'USD') FROM inference_cost_daily "
        "WHERE provider = %s AND day >= %s AND day <= %s AND source = ANY(%s)",
        (provider, start, end, list(BILLABLE_SOURCES)),
    ).fetchall()
    return sorted(r[0] for r in rows)


def has_any_data(conn, provider: str, start: dt.date, end: dt.date) -> bool:
    """Whether Meter tracked anything at all for this provider and period.
    Nothing tracked is 'incomplete data', not 'the provider billed you for
    everything' — a distinction the run status depends on."""
    row = conn.execute(
        "SELECT 1 FROM inference_cost_daily WHERE provider = %s AND day >= %s AND day <= %s "
        "AND source = ANY(%s) LIMIT 1",
        (provider, start, end, list(BILLABLE_SOURCES)),
    ).fetchone()
    return row is not None
