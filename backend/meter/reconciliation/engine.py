"""The comparison itself: match, tolerate, classify — and never fudge.

Three rules shape all of it.

**Usage is compared with usage.** The principal number is the provider's usage
subtotal against Meter's tracked usage cost. Tax, credits, discounts and
fees are carried through and reported, but they are never added to either side
of that comparison — an invoice total is not a usage figure, and calling the
difference "missing usage" would be wrong every month there is any tax at all.

**A match is explained or it is not a match.** Every comparison records the
strategy that produced it, and an aggregate comparison is labelled as one. The
totals are never nudged to agree.

**A cause is only confirmed when the data says so.** Anything inferred is
'possible', and anything unsupported is 'unknown' — with the evidence attached
either way, so a reader can disagree.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Optional

from . import tracked
from .common import ZERO, pct, quantize, record_audit, tenant_conn
from .flag import ReconciliationError

#: Classifications. Chosen to name a cause, not to describe a size.
PROVIDER_ONLY = "provider_usage_missing_from_meter"
METER_ONLY = "meter_usage_absent_from_statement"
PRICING_MISMATCH = "pricing_version_mismatch"
CURRENCY_MISMATCH = "currency_mismatch"
PERIOD_BOUNDARY = "billing_period_boundary"
DUPLICATE_ROW = "duplicate_provider_row"
UNKNOWN_MODEL = "unknown_model_mapping"
UNATTRIBUTED_ACCOUNT = "unattributed_provider_workspace"
UNSUPPORTED_LINE = "unsupported_line_item_type"
INCOMPLETE_PROVIDER = "incomplete_provider_export"
INCOMPLETE_TRACKED = "incomplete_meter_data"
UNEXPLAINED = "unexplained_difference"
MATCHED = "matched"


def _within(difference: Decimal, base: Decimal, abs_tol: Decimal, pct_tol: Decimal) -> bool:
    """Inside tolerance if EITHER bound forgives it. Absolute covers the small
    rounding on a small bill; percentage covers proportional drift on a large
    one. Requiring both would make the absolute bound meaningless at scale."""
    magnitude = abs(difference)
    if magnitude <= abs_tol:
        return True
    if base != 0 and (magnitude / abs(base) * 100) <= pct_tol:
        return True
    return False


def _line_items(conn, import_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, row_number, service_date, period_start, provider_account, api_key_ref, "
        "model, usage_category, quantity, usage_subtotal, credit, tax, fee, adjustment, "
        "billed_amount, currency, statement_id, line_item_id "
        "FROM recon_line_item WHERE import_id = %s AND mapping_status = 'ok' "
        "ORDER BY row_number",
        (import_id,),
    ).fetchall()
    return [
        {
            "id": str(r[0]),
            "row_number": r[1],
            "service_date": r[2] or r[3],
            "provider_account": r[4],
            "api_key_ref": r[5],
            "model": r[6],
            "usage_category": r[7],
            "quantity": r[8],
            "usage_subtotal": Decimal(str(r[9])),
            "credit": Decimal(str(r[10])),
            "tax": Decimal(str(r[11])),
            "fee": Decimal(str(r[12])),
            "adjustment": Decimal(str(r[13])),
            "billed_amount": Decimal(str(r[14])),
            "currency": r[15],
            "statement_id": r[16],
            "line_item_id": r[17],
        }
        for r in rows
    ]


def _norm(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def _tracked_index(rows: list[dict]) -> dict:
    """Tracked spend keyed for each matching strategy, most specific first."""
    by_key_date_model: dict = {}
    by_date_model: dict = {}
    for row in rows:
        account = _norm(row["workspace_id"])
        day = row["day"]
        model = _norm(row["model"])
        for key in (_norm(row["api_key_id"]), _norm(row["api_key_ref"])):
            if key:
                by_key_date_model.setdefault((account, key, day, model), []).append(row)
        by_date_model.setdefault((account, day, model), []).append(row)
    return {"key_date_model": by_key_date_model, "date_model": by_date_model}


def _consume(bucket: list[dict], used: set) -> tuple:
    """Take the unused tracked rows from a bucket, marking them used so one
    tracked row can never be counted against two statement lines."""
    amount, taken = ZERO, []
    for row in bucket:
        marker = id(row)
        if marker in used:
            continue
        used.add(marker)
        amount += row["amount"]
        taken.append(row)
    return amount, taken


def calculate(
    tenant_id: str,
    *,
    import_id: str,
    actor: Optional[str] = None,
    tolerance_abs: Optional[Decimal] = None,
    tolerance_pct: Optional[Decimal] = None,
) -> dict:
    """Reconcile one import against tracked spend, and store an immutable run.

    Recalculating never edits a previous run: it writes a new one. Failures are
    recorded on the run itself, so a failed calculation is a visible state in
    this module rather than an error anywhere else.
    """
    from .flag import tolerances  # local: keeps the module import graph a tree

    abs_tol, pct_tol = tolerances(tenant_id)
    if tolerance_abs is not None:
        abs_tol = tolerance_abs
    if tolerance_pct is not None:
        pct_tol = tolerance_pct

    with tenant_conn(tenant_id) as conn:
        header = conn.execute(
            "SELECT provider, provider_account, period_start, period_end, currency, status "
            "FROM recon_import WHERE id = %s",
            (import_id,),
        ).fetchone()
        if header is None:
            raise ReconciliationError("That import does not exist.")
        provider, account, start, end, currency, status = header
        if status != "committed":
            raise ReconciliationError(f"That import is {status}; import a current file first.")

        run_id = conn.execute(
            """
            INSERT INTO recon_run (tenant_id, import_id, provider, provider_account,
                                   period_start, period_end, currency, status,
                                   tolerance_abs, tolerance_pct, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s)
            RETURNING id
            """,
            (
                tenant_id,
                import_id,
                provider,
                account,
                start,
                end,
                currency,
                abs_tol,
                pct_tol,
                actor,
            ),
        ).fetchone()[0]

        try:
            outcome = _reconcile(
                conn,
                tenant_id,
                str(run_id),
                import_id,
                provider,
                account,
                start,
                end,
                currency,
                abs_tol,
                pct_tol,
            )
        except Exception as exc:  # contained: the run records the failure
            conn.execute(
                "UPDATE recon_run SET status = 'failed', completed_at = now(), "
                "failure_reason = %s WHERE id = %s",
                (str(exc)[:500], run_id),
            )
            record_audit(
                conn,
                tenant_id,
                "reconciliation_failed",
                actor=actor,
                import_id=import_id,
                run_id=str(run_id),
                detail={"reason": str(exc)[:200]},
            )
            return _run_row(conn, str(run_id))

        record_audit(
            conn,
            tenant_id,
            "reconciliation_completed",
            actor=actor,
            import_id=import_id,
            run_id=str(run_id),
            detail={"status": outcome["status"]},
        )
        return _run_row(conn, str(run_id))


def _reconcile(
    conn, tenant_id, run_id, import_id, provider, account, start, end, currency, abs_tol, pct_tol
) -> dict:
    items = _line_items(conn, import_id)
    if not items or start is None or end is None:
        raise ReconciliationError("The import has no usable dated rows.")

    # --- financial categories, kept apart -----------------------------------
    provider_usage = quantize(sum((i["usage_subtotal"] for i in items), ZERO))
    provider_credits = quantize(sum((i["credit"] for i in items), ZERO))
    provider_tax = quantize(sum((i["tax"] for i in items), ZERO))
    provider_fees = quantize(sum((i["fee"] + i["adjustment"] for i in items), ZERO))
    provider_total = quantize(sum((i["billed_amount"] for i in items), ZERO))

    statement_currencies = sorted({i["currency"] for i in items})
    tracked_rows = tracked.aggregates(conn, provider, start, end, None)
    tracked_currencies = tracked.currencies(conn, provider, start, end)
    tracked_usage = quantize(sum((r["amount"] for r in tracked_rows), ZERO))

    matches: list[dict] = []
    incomplete = False

    # --- currency: reported, never converted --------------------------------
    mixed = len(statement_currencies) > 1
    crossed = bool(tracked_currencies) and set(statement_currencies) - set(tracked_currencies)
    if mixed or crossed:
        incomplete = True
        matches.append(
            {
                "line_item_id": None,
                "strategy": "aggregate",
                "dimensions": {"statement": statement_currencies, "tracked": tracked_currencies},
                "provider_amount": provider_usage,
                "tracked_amount": tracked_usage,
                "difference": ZERO,
                "difference_pct": None,
                "classification": CURRENCY_MISMATCH,
                "explanation": (
                    "The statement and the tracked data are not in the same currency, so the "
                    "totals are not comparable. Nothing has been converted."
                ),
                "confidence": "confirmed",
                "evidence": [
                    f"statement: {', '.join(statement_currencies) or 'none'}",
                    f"tracked: {', '.join(tracked_currencies) or 'none'}",
                ],
            }
        )

    # --- matching, most specific strategy first -----------------------------
    index = _tracked_index(tracked_rows)
    used: set = set()
    seen_line_ids: dict = {}
    usage_items = [i for i in items if i["usage_subtotal"] != 0]
    non_usage = [i for i in items if i["usage_subtotal"] == 0]

    for item in usage_items:
        # A provider line id repeated inside one statement is a duplicate row,
        # not twice the usage.
        if item["line_item_id"]:
            prior = seen_line_ids.get(item["line_item_id"])
            if prior is not None:
                matches.append(_duplicate(item, prior))
                continue
            seen_line_ids[item["line_item_id"]] = item["row_number"]

        acct = _norm(item["provider_account"])
        day = item["service_date"]
        model = _norm(item["model"])
        key = _norm(item["api_key_ref"])

        bucket, strategy = None, None
        if key:
            bucket = index["key_date_model"].get((acct, key, day, model))
            strategy = "account_key_date_model"
        if bucket is None:
            bucket = index["date_model"].get((acct, day, model))
            strategy = "account_date_model"

        if bucket:
            amount, taken = _consume(bucket, used)
            matches.append(_compare(item, amount, strategy, taken, abs_tol, pct_tol))
        else:
            matches.append(_unmatched_provider(item, tracked_rows, start, end))

    for item in non_usage:
        matches.append(_non_usage(item))

    # --- tracked spend the statement never mentioned ------------------------
    leftovers = [r for r in tracked_rows if id(r) not in used]
    for row in leftovers:
        if row["amount"] == 0:
            continue
        matches.append(
            {
                "line_item_id": None,
                "strategy": "unmatched_tracked",
                "dimensions": {
                    "day": row["day"].isoformat(),
                    "model": row["model"],
                    "workspace_id": row["workspace_id"],
                    "api_key_id": row["api_key_id"],
                },
                "provider_amount": ZERO,
                "tracked_amount": quantize(row["amount"]),
                "difference": quantize(-row["amount"]),
                "difference_pct": None,
                "classification": METER_ONLY,
                "explanation": (
                    "Meter tracked this spend but no line on the statement matched it."
                ),
                "confidence": "possible",
                "evidence": [
                    f"tracked {row['amount']} on {row['day']} for "
                    f"{row['model'] or 'an unnamed model'}",
                    "no statement line with this day, model and account",
                ],
            }
        )

    # --- the headline comparison -------------------------------------------
    difference = quantize(provider_usage - tracked_usage)
    difference_pct = pct(difference, provider_usage)

    if not tracked.has_any_data(conn, provider, start, end):
        incomplete = True
        matches.append(
            {
                "line_item_id": None,
                "strategy": "aggregate",
                "dimensions": {"provider": provider, "period": f"{start}..{end}"},
                "provider_amount": provider_usage,
                "tracked_amount": ZERO,
                "difference": provider_usage,
                "difference_pct": None,
                "classification": INCOMPLETE_TRACKED,
                "explanation": (
                    "Meter has no connector data for this provider and period, so there is "
                    "nothing to compare the statement against yet."
                ),
                "confidence": "confirmed",
                "evidence": [f"no {provider} cost-API rows between {start} and {end}"],
            }
        )

    if incomplete:
        status = "incomplete_data"
    elif difference == 0:
        status = "matched"
    elif _within(difference, provider_usage, abs_tol, pct_tol):
        status = "within_tolerance"
    else:
        status = "discrepancy"

    unmatched_provider = sum(1 for m in matches if m["classification"] == PROVIDER_ONLY)
    unmatched_tracked = sum(1 for m in matches if m["classification"] == METER_ONLY)

    conn.execute(
        """
        UPDATE recon_run SET status = %s, provider_usage = %s, provider_credits = %s,
               provider_tax = %s, provider_fees = %s, provider_total = %s,
               tracked_usage = %s, usage_difference = %s, usage_difference_pct = %s,
               unmatched_provider_count = %s, unmatched_tracked_count = %s,
               completed_at = now()
         WHERE id = %s
        """,
        (
            status,
            provider_usage,
            provider_credits,
            provider_tax,
            provider_fees,
            provider_total,
            tracked_usage,
            difference,
            difference_pct,
            unmatched_provider,
            unmatched_tracked,
            run_id,
        ),
    )
    for match in matches:
        conn.execute(
            """
            INSERT INTO recon_match (tenant_id, run_id, line_item_id, strategy, dimensions,
                                     provider_amount, tracked_amount, difference, difference_pct,
                                     classification, explanation, confidence, evidence)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                tenant_id,
                run_id,
                match["line_item_id"],
                match["strategy"],
                json.dumps(match["dimensions"], default=str),
                match["provider_amount"],
                match["tracked_amount"],
                match["difference"],
                match["difference_pct"],
                match["classification"],
                match["explanation"],
                match["confidence"],
                json.dumps(match["evidence"], default=str),
            ),
        )
    return {"status": status}


def _compare(item, tracked_amount, strategy, taken, abs_tol, pct_tol) -> dict:
    provider_amount = quantize(item["usage_subtotal"])
    tracked_amount = quantize(tracked_amount)
    difference = quantize(provider_amount - tracked_amount)
    dimensions = {
        "row": item["row_number"],
        "day": item["service_date"],
        "model": item["model"],
        "provider_account": item["provider_account"],
        "api_key_ref": item["api_key_ref"],
        "category": item["usage_category"],
    }
    evidence = [
        f"statement row {item['row_number']}: {provider_amount} for "
        f"{item['model'] or 'an unnamed model'} on {item['service_date']}",
        f"matched {len(taken)} tracked row(s) totalling {tracked_amount} "
        f"({strategy.replace('_', ' ')})",
    ]
    if difference == 0:
        classification, explanation, confidence = (
            MATCHED,
            "The statement line and the tracked spend agree exactly.",
            "confirmed",
        )
    elif _within(difference, provider_amount, abs_tol, pct_tol):
        classification, explanation, confidence = (
            MATCHED,
            "Within tolerance. A difference this small is rounding between the provider's "
            "unit prices and ours, not missing usage.",
            "possible",
        )
    elif item["model"] and not any(t["model"] for t in taken):
        classification, explanation, confidence = (
            UNKNOWN_MODEL,
            f"The statement bills {item['model']}, which the tracked rows for this day do "
            "not name. The price used for it may differ.",
            "possible",
        )
    else:
        classification, explanation, confidence = (
            PRICING_MISMATCH,
            "Same usage on both sides, different money. The most likely cause is a price "
            "change the tracked rows were computed before.",
            "possible",
        )
        evidence.append(f"difference {difference} on matched usage")
    return {
        "line_item_id": item["id"],
        "strategy": strategy,
        "dimensions": dimensions,
        "provider_amount": provider_amount,
        "tracked_amount": tracked_amount,
        "difference": difference,
        "difference_pct": pct(difference, provider_amount),
        "classification": classification,
        "explanation": explanation,
        "confidence": confidence,
        "evidence": evidence,
    }


def _unmatched_provider(item, tracked_rows, start, end) -> dict:
    """A statement line nothing tracked corresponds to. The reason matters: a
    day outside the period Meter holds is a boundary problem, an unknown
    workspace is an attribution problem, and neither is missing usage."""
    day = item["service_date"]
    known_accounts = {_norm(r["workspace_id"]) for r in tracked_rows}
    evidence = [
        f"statement row {item['row_number']}: {item['usage_subtotal']} on {day}",
        "no tracked row with this day, model and account",
    ]
    if day and (day < start or day > end):
        classification, explanation, confidence = (
            PERIOD_BOUNDARY,
            f"This line is dated {day}, outside the {start}..{end} period being reconciled.",
            "confirmed",
        )
    elif item["provider_account"] and _norm(item["provider_account"]) not in known_accounts:
        classification, explanation, confidence = (
            UNATTRIBUTED_ACCOUNT,
            f"The statement bills workspace {item['provider_account']}, which Meter has "
            "no tracked spend for. It is probably not connected.",
            "possible",
        )
        listed = ", ".join(sorted(a for a in known_accounts if a)) or "none"
        evidence.append(f"tracked workspaces: {listed}")
    else:
        classification, explanation, confidence = (
            PROVIDER_ONLY,
            "The provider billed this usage and Meter has no record of it.",
            "possible",
        )
    return {
        "line_item_id": item["id"],
        "strategy": "unmatched_provider",
        "dimensions": {
            "row": item["row_number"],
            "day": day,
            "model": item["model"],
            "provider_account": item["provider_account"],
        },
        "provider_amount": quantize(item["usage_subtotal"]),
        "tracked_amount": ZERO,
        "difference": quantize(item["usage_subtotal"]),
        "difference_pct": None,
        "classification": classification,
        "explanation": explanation,
        "confidence": confidence,
        "evidence": evidence,
    }


def _non_usage(item) -> dict:
    """Tax, a credit, a fee or an adjustment. Reported and kept out of the usage
    comparison — this is the line that must never be read as missing usage."""
    amount = item["credit"] + item["tax"] + item["fee"] + item["adjustment"]
    kind = (
        "tax"
        if item["tax"]
        else "credit"
        if item["credit"]
        else "fee"
        if item["fee"]
        else "adjustment"
        if item["adjustment"]
        else "unsupported"
    )
    if kind == "unsupported":
        classification, explanation = (
            UNSUPPORTED_LINE,
            "This line carries no usage and no recognised financial category, so it is "
            "reported but not compared.",
        )
    else:
        classification, explanation = (
            f"provider_{kind}",
            f"A {kind} line. It is part of the invoice total and no part of usage, so it is "
            "excluded from the usage comparison.",
        )
    return {
        "line_item_id": item["id"],
        "strategy": "unmatched_provider",
        "dimensions": {"row": item["row_number"], "category": item["usage_category"], "kind": kind},
        "provider_amount": quantize(amount),
        "tracked_amount": ZERO,
        "difference": ZERO,
        "difference_pct": None,
        "classification": classification,
        "explanation": explanation,
        "confidence": "confirmed",
        "evidence": [
            f"statement row {item['row_number']} category '{item['usage_category']}' = {amount}"
        ],
    }


def _duplicate(item, first_row: int) -> dict:
    return {
        "line_item_id": item["id"],
        "strategy": "line_item_id",
        "dimensions": {"row": item["row_number"], "line_item_id": item["line_item_id"]},
        "provider_amount": quantize(item["usage_subtotal"]),
        "tracked_amount": ZERO,
        "difference": ZERO,
        "difference_pct": None,
        "classification": DUPLICATE_ROW,
        "explanation": (
            f"The provider's line id {item['line_item_id']} already appeared on row "
            f"{first_row} of this statement. It is counted once."
        ),
        "confidence": "confirmed",
        "evidence": [f"row {first_row} and row {item['row_number']} share a line id"],
    }


def _run_row(conn, run_id: str) -> dict:
    r = conn.execute(
        "SELECT id, import_id, provider, provider_account, period_start, period_end, currency, "
        "status, tolerance_abs, tolerance_pct, provider_usage, provider_credits, provider_tax, "
        "provider_fees, provider_total, tracked_usage, usage_difference, usage_difference_pct, "
        "unmatched_provider_count, unmatched_tracked_count, created_by, created_at, "
        "completed_at, failure_reason FROM recon_run WHERE id = %s",
        (run_id,),
    ).fetchone()
    return {
        "id": str(r[0]),
        "import_id": str(r[1]) if r[1] else None,
        "provider": r[2],
        "provider_account": r[3],
        "period_start": r[4].isoformat(),
        "period_end": r[5].isoformat(),
        "currency": r[6],
        "status": r[7],
        "tolerance_abs": float(r[8]),
        "tolerance_pct": float(r[9]),
        "provider_usage": float(r[10]),
        "provider_credits": float(r[11]),
        "provider_tax": float(r[12]),
        "provider_fees": float(r[13]),
        "provider_total": float(r[14]),
        "tracked_usage": float(r[15]),
        "usage_difference": float(r[16]),
        "usage_difference_pct": float(r[17]) if r[17] is not None else None,
        "unmatched_provider_count": r[18],
        "unmatched_tracked_count": r[19],
        "created_by": r[20],
        "created_at": r[21].isoformat() if r[21] else None,
        "completed_at": r[22].isoformat() if r[22] else None,
        "failure_reason": r[23],
    }


def runs(tenant_id: str, limit: int = 50) -> list[dict]:
    with tenant_conn(tenant_id) as conn:
        ids = [
            str(r[0])
            for r in conn.execute(
                "SELECT id FROM recon_run ORDER BY created_at DESC LIMIT %s", (limit,)
            ).fetchall()
        ]
        return [_run_row(conn, i) for i in ids]


def run_detail(tenant_id: str, run_id: str) -> dict:
    with tenant_conn(tenant_id) as conn:
        exists = conn.execute("SELECT 1 FROM recon_run WHERE id = %s", (run_id,)).fetchone()
        if not exists:
            raise ReconciliationError("No such reconciliation run.")
        run = _run_row(conn, run_id)
        rows = conn.execute(
            "SELECT strategy, dimensions, provider_amount, tracked_amount, difference, "
            "difference_pct, classification, explanation, confidence, evidence "
            "FROM recon_match WHERE run_id = %s ORDER BY classification, difference DESC",
            (run_id,),
        ).fetchall()
        run["matches"] = [
            {
                "strategy": m[0],
                "dimensions": m[1],
                "provider_amount": float(m[2]),
                "tracked_amount": float(m[3]),
                "difference": float(m[4]),
                "difference_pct": float(m[5]) if m[5] is not None else None,
                "classification": m[6],
                "explanation": m[7],
                "confidence": m[8],
                "evidence": m[9],
            }
            for m in rows
        ]
        run["breakdown"] = _breakdown(conn, run["import_id"]) if run["import_id"] else {}
        return run


def _breakdown(conn, import_id: str) -> dict:
    """The statement's usage sliced the ways a reader will want it."""

    def group(column: str) -> list[dict]:
        rows = conn.execute(
            f"SELECT COALESCE({column}::text, '—'), SUM(usage_subtotal), COUNT(*) "
            f"FROM recon_line_item WHERE import_id = %s AND mapping_status = 'ok' "
            f"GROUP BY 1 ORDER BY 2 DESC LIMIT 100",
            (import_id,),
        ).fetchall()
        return [{"key": r[0], "usage": float(r[1] or 0), "lines": int(r[2])} for r in rows]

    return {
        "by_date": group("service_date"),
        "by_model": group("model"),
        "by_account": group("provider_account"),
        "by_category": group("usage_category"),
    }
