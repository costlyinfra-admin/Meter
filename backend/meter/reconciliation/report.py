"""CSV export of a reconciliation run.

Every string that reaches the file goes through ``neutralise`` first. An export
is opened in a spreadsheet by definition, and a provider's model name or an
error message that begins with '=' would otherwise be executed on open — with
the file's own contents as the argument. The stored copy keeps the provider's
original text; only the copy that leaves does not.

The export is built from one run, read inside that tenant's transaction, so it
cannot contain another tenant's data.
"""

from __future__ import annotations

import csv
import io

from .engine import run_detail
from .imports import neutralise


def _row(writer, *cells) -> None:
    writer.writerow([neutralise(str(c)) if isinstance(c, str) else c for c in cells])


def export_csv(tenant_id: str, run_id: str) -> tuple:
    """(filename, csv text) for one run. Raises if the run is not this tenant's."""
    run = run_detail(tenant_id, run_id)
    out = io.StringIO()
    writer = csv.writer(out)

    _row(writer, "Meter reconciliation report")
    _row(writer, "Run", run["id"])
    _row(writer, "Provider", run["provider"])
    _row(writer, "Provider account", run["provider_account"] or "")
    _row(writer, "Period", f"{run['period_start']} to {run['period_end']}")
    _row(writer, "Currency", run["currency"])
    _row(writer, "Status", run["status"])
    _row(writer, "Reconciled at", run["completed_at"] or run["created_at"])
    _row(writer, "Tolerance (absolute)", run["tolerance_abs"])
    _row(writer, "Tolerance (percent)", run["tolerance_pct"])
    writer.writerow([])

    _row(writer, "Financial category", "Amount")
    _row(writer, "Provider usage subtotal", run["provider_usage"])
    _row(writer, "Provider credits and discounts", run["provider_credits"])
    _row(writer, "Provider tax", run["provider_tax"])
    _row(writer, "Provider fees and adjustments", run["provider_fees"])
    _row(writer, "Provider invoice total", run["provider_total"])
    _row(writer, "Meter tracked usage", run["tracked_usage"])
    _row(writer, "Usage difference", run["usage_difference"])
    _row(writer, "Usage difference %", run["usage_difference_pct"])
    writer.writerow([])

    _row(
        writer,
        "Classification",
        "Strategy",
        "Confidence",
        "Provider amount",
        "Meter amount",
        "Difference",
        "Difference %",
        "Explanation",
        "Evidence",
        "Dimensions",
    )
    for match in run["matches"]:
        _row(
            writer,
            match["classification"],
            match["strategy"],
            match["confidence"],
            match["provider_amount"],
            match["tracked_amount"],
            match["difference"],
            match["difference_pct"],
            match["explanation"],
            " | ".join(str(e) for e in (match["evidence"] or [])),
            "; ".join(f"{k}={v}" for k, v in (match["dimensions"] or {}).items()),
        )

    filename = f"reconciliation-{run['provider']}-{run['period_start']}-{run['id'][:8]}.csv"
    return filename, out.getvalue()
