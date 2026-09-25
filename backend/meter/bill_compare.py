"""What the provider billed, against what Meter metered.

The Reconciliation screen's primary path. It answers one question per connected
provider — does the bill agree with what the SDK measured — from data Meter
already syncs, with no upload and no new provider integration.

**Two observations of the same spend.** A cost connector reads the provider's
own billing API and stores it as `source='cost_api'` (or `cost_api_est`, for a
month the provider has not closed). The metering SDK reports per-call usage,
priced and stored as `source='hook'`. The first is authoritative on dollars
(invariant 5); the second is the only one that knows which feature and which
customer a call belonged to. Where they disagree, something is unattributed.

**Computed on read, and it writes nothing.** `hook.reconcile()` stores a
per-provider snapshot in `bill_reconciliation` on each nightly ingest; this does
not read it, because a snapshot is stale the moment a sync lands and carries
neither freshness nor any breakdown. Every statement here is a SELECT, so
reconciling cannot alter a tracked number — which is a property of the code
rather than a promise about it.

**Deliberately outside `meter/reconciliation/`.** That package is a sealed,
opt-in module that owns its `recon_*` tables and compares an *uploaded
statement* against connector spend. This compares *connector spend* against
*metered spend* — different sides, different data, always available. Putting it
there would break the seal its docstring promises; the screen shows both, and
the statement import remains the fallback for what this cannot reach.

**What it will not claim.** Per-day and per-account variance are impossible and
are not offered: `inference_cost_daily` carries connector rows only, and hook
rows carry no workspace or API key, so only one side of those exists. Model is
the one dimension both sides record, so it is the one breakdown with a real
variance in it. Tax, credits and refunds are on none of these APIs at all —
`CostRecord` has no field for them — so they are not shown either. A provider
statement has them, which is what the import path is still for.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Optional

from . import credentials
from .db import app_dsn, connect, tenant_tx
from .providers import month_start

#: Agreement within this many dollars is agreement. Same figure hook.reconcile
#: uses, so this screen and the stored snapshot cannot disagree about a status.
TOLERANCE = Decimal("0.50")

#: ...and within this share of the bill, for a provider large enough that fifty
#: cents is noise. A month billed at $40,000 does not have a problem because it
#: is eighty cents out.
TOLERANCE_PCT = Decimal("0.5")

#: What the provider's billing API reported. `cost_api_est` is Anthropic's
#: not-yet-billed estimate for the open month — real spend, not yet invoiced,
#: and surfaced as such rather than left out.
BILLED_SOURCES = ("cost_api", "cost_api_est")

#: What the metering SDK measured.
METERED_SOURCE = "hook"

# --- statuses ---------------------------------------------------------------
MATCHED = "matched"
VARIANCE = "variance"
#: The bill is here and nothing is metered against it. Not a variance: a
#: variance implies two measurements disagreeing, and this is one measurement.
#: The fix is installing the SDK, not investigating a discrepancy.
NO_METERED_DATA = "no_metered_data"
#: A provider Meter can read a bill from, that this tenant has not connected —
#: or has connected without any billing data arriving for the period.
BILLING_ACCESS_REQUIRED = "billing_access_required"
#: Metered spend for something with no billing API to check it against: a
#: self-hosted model, or a provider outside the connector list.
NOT_SUPPORTED = "not_supported"

#: Connectors that read a provider's billing API. Taken from the connector
#: registry rather than restated, so a provider added there appears here.
_BILLABLE = {
    c["type"]: c["name"] for c in credentials.KNOWN_CONNECTORS if c["category"] == "inference"
}


def _money(value) -> Decimal:
    return Decimal(str(value or 0))


def _variance(billed: Decimal, metered: Decimal) -> tuple:
    """(absolute, percent-of-bill). Percent is None when there is no bill to be
    a percentage of — a number over zero is not a percentage."""
    difference = billed - metered
    pct = (abs(difference) / billed * 100) if billed > 0 else None
    return difference, pct


def _status(connected: bool, supported: bool, billed: Decimal, metered: Decimal, pct) -> str:
    if not supported:
        return NOT_SUPPORTED
    if billed <= 0:
        return BILLING_ACCESS_REQUIRED
    if metered <= 0:
        return NO_METERED_DATA
    difference = abs(billed - metered)
    if difference <= TOLERANCE or (pct is not None and pct <= TOLERANCE_PCT):
        return MATCHED
    return VARIANCE


def _resolve_period(conn, period: Optional[dt.date]) -> dt.date:
    """The month asked for, or the latest with any inference cost in it."""
    if period is not None:
        return month_start(period)
    row = conn.execute("SELECT max(period) FROM inference_cost").fetchone()
    return row[0] if row and row[0] else month_start(dt.date.today())


def compare(tenant_id: str, period: Optional[dt.date] = None) -> dict:
    """Every provider worth a row, and how its bill compares to what was metered."""
    connected = {c["type"] for c in credentials.connector_statuses(tenant_id) if c["connected"]}
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        start = _resolve_period(conn, period)
        rows = conn.execute(
            """
            SELECT provider,
                   SUM(amount) FILTER (WHERE source = ANY(%s))        AS billed,
                   SUM(amount) FILTER (WHERE source = %s)             AS metered,
                   bool_or(source = 'cost_api_est')                   AS estimated,
                   max(updated_at) FILTER (WHERE source = ANY(%s))    AS billed_at,
                   min(currency)                                      AS currency,
                   count(DISTINCT currency)                           AS currencies
              FROM inference_cost
             WHERE period = %s
             GROUP BY provider
            """,
            (list(BILLED_SOURCES), METERED_SOURCE, list(BILLED_SOURCES), start),
        ).fetchall()

    seen = {r[0] for r in rows}
    out = []
    for provider, billed, metered, estimated, billed_at, currency, currencies in rows:
        out.append(
            _row(
                provider,
                _money(billed),
                _money(metered),
                provider in connected,
                estimated,
                billed_at,
                currency,
                currencies,
            )
        )
    # A connected provider with nothing this month still belongs on the screen:
    # "connected, no billing data yet" is the state a customer most needs to see.
    for provider in sorted(connected - seen):
        if provider in _BILLABLE:
            out.append(_row(provider, Decimal(0), Decimal(0), True, False, None, "USD", 1))

    out.sort(key=lambda r: (-(r["provider_reported"] or 0), r["provider"]))
    return {
        "period": start.isoformat(),
        "providers": out,
        "tolerance": {"absolute": float(TOLERANCE), "percent": float(TOLERANCE_PCT)},
    }


def _row(provider, billed, metered, connected, estimated, billed_at, currency, currencies) -> dict:
    supported = provider in _BILLABLE
    difference, pct = _variance(billed, metered)
    return {
        "provider": provider,
        "name": _BILLABLE.get(provider, provider),
        "connected": connected,
        "supported": supported,
        "status": _status(connected, supported, billed, metered, pct),
        "provider_reported": float(billed),
        "tracked": float(metered),
        "variance": float(difference),
        "variance_pct": float(pct) if pct is not None else None,
        # True while the provider has not closed the month: the bill can still
        # move, so a variance here is not yet a discrepancy.
        "estimated": bool(estimated),
        "billing_updated_at": billed_at.isoformat() if billed_at else None,
        "currency": currency or "USD",
        # Summing across currencies would produce a number meaning nothing.
        "mixed_currency": (currencies or 1) > 1,
    }


def breakdown(tenant_id: str, provider: str, period: Optional[dt.date] = None) -> dict:
    """Where a provider's variance sits, on the dimensions both sides record.

    Model only. The provider's own daily and per-workspace detail is returned
    alongside it — useful for seeing when a gap opened — but labelled for what
    it is, because Meter's metered rows carry neither and a column of blanks
    beside them would read as zero rather than as absent.
    """
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        start = _resolve_period(conn, period)
        by_model = conn.execute(
            """
            SELECT COALESCE(model, '—'),
                   COALESCE(SUM(amount) FILTER (WHERE source = ANY(%s)), 0),
                   COALESCE(SUM(amount) FILTER (WHERE source = %s), 0)
              FROM inference_cost
             WHERE period = %s AND provider = %s
             GROUP BY 1 ORDER BY 2 DESC LIMIT 100
            """,
            (list(BILLED_SOURCES), METERED_SOURCE, start, provider),
        ).fetchall()

        # Provider side only: inference_cost_daily holds connector rows, and the
        # hook writes no daily row to compare against.
        by_day = conn.execute(
            """
            SELECT day, COALESCE(SUM(amount), 0)
              FROM inference_cost_daily
             WHERE provider = %s AND day >= %s AND day < (%s::date + INTERVAL '1 month')
               AND source = ANY(%s)
             GROUP BY day ORDER BY day
            """,
            (provider, start, start, list(BILLED_SOURCES)),
        ).fetchall()

        by_account = conn.execute(
            """
            SELECT COALESCE(workspace_name, workspace_id, api_key_name, api_key_ref, '—'),
                   COALESCE(SUM(amount), 0)
              FROM inference_cost
             WHERE period = %s AND provider = %s AND source = ANY(%s)
             GROUP BY 1 ORDER BY 2 DESC LIMIT 50
            """,
            (start, provider, list(BILLED_SOURCES)),
        ).fetchall()

    models = []
    for model, billed, metered in by_model:
        billed, metered = _money(billed), _money(metered)
        difference, pct = _variance(billed, metered)
        models.append(
            {
                "model": model,
                "provider_reported": float(billed),
                "tracked": float(metered),
                "variance": float(difference),
                "variance_pct": float(pct) if pct is not None else None,
            }
        )
    return {
        "period": start.isoformat(),
        "provider": provider,
        "by_model": models,
        "provider_only": {
            # Named so nothing downstream can mistake these for a comparison.
            "by_day": [{"day": d.isoformat(), "provider_reported": float(a)} for d, a in by_day],
            "by_account": [{"account": k, "provider_reported": float(a)} for k, a in by_account],
        },
    }
