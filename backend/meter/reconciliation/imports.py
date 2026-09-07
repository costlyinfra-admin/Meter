"""Reading a provider billing export: parse, map, validate, preview, commit.

There is no documented provider CSV format in this repository, and inventing one
would guarantee a mismatch with whatever a customer actually downloads. So the
import is explicit: the caller says which of their columns holds each field.
``suggest_mapping`` guesses from the headers to make that a confirmation rather
than a chore, and the guess is always shown before anything is stored.

The file is treated as hostile throughout. It is text from outside the system:
its cells are never evaluated, never used to build a path, never logged in full,
and any cell that a spreadsheet would treat as a formula is neutralised before
it is shown back to anyone.

Only the mapped fields plus the mapped source columns are persisted. A billing
export can carry a contact address or an account manager's name; keeping the
whole row "just in case" would store personal data this module has no use for.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import re
from typing import Any, Optional

from .common import ZERO, money, quantize, record_audit, tenant_conn
from .flag import ReconciliationError

#: Caps. A monthly statement is thousands of rows, not millions; anything past
#: this is a mistake or an attack, and either way is refused rather than parsed.
MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 20_000
MAX_COLUMNS = 200
MAX_CELL = 2000
PREVIEW_ROWS = 25

#: The fields reconciliation understands. Everything else in the file is ignored.
FIELDS = {
    "service_date": "Date the usage was incurred",
    "period_start": "Start of the line's billing period",
    "period_end": "End of the line's billing period",
    "provider_account": "Provider workspace or account id",
    "api_key_ref": "API key id or name, if the statement carries one",
    "model": "Model name",
    "usage_category": "Line-item category (usage, tax, credit, fee…)",
    "quantity": "Tokens or units billed",
    "usage_subtotal": "Usage charge before tax, credits and fees",
    "credit": "Credit or discount applied",
    "tax": "Tax charged",
    "fee": "Fee charged",
    "adjustment": "Provider adjustment",
    "billed_amount": "Final amount billed for the line",
    "currency": "ISO currency of the line",
    "statement_id": "Provider invoice or statement id",
    "line_item_id": "Provider's own id for this line",
}

#: The minimum that makes a line reconcilable: when it happened, and what it cost.
REQUIRED = ("service_date", "usage_subtotal")

#: Header guesses, in order. First match wins, matched case-insensitively on a
#: header with punctuation and spacing stripped.
HINTS: dict[str, tuple] = {
    "service_date": ("servicedate", "usagedate", "date", "day", "invoicedate", "billingdate"),
    "period_start": ("periodstart", "startdate", "billingperiodstart", "from"),
    "period_end": ("periodend", "enddate", "billingperiodend", "to"),
    "provider_account": (
        "workspaceid",
        "workspace",
        "accountid",
        "account",
        "organizationid",
        "orgid",
        "projectid",
        "project",
    ),
    "api_key_ref": ("apikeyid", "apikey", "keyid", "key", "apikeyname"),
    "model": ("model", "modelname", "modelid", "sku", "product"),
    "usage_category": ("category", "usagetype", "linetype", "type", "description", "itemtype"),
    "quantity": ("quantity", "tokens", "units", "usagequantity", "totaltokens"),
    "usage_subtotal": (
        "usagesubtotal",
        "subtotal",
        "usagecost",
        "usageamount",
        "amountusage",
        "netamount",
        "cost",
        "amount",
    ),
    "credit": ("credit", "credits", "discount", "discounts", "creditamount"),
    "tax": ("tax", "taxamount", "vat", "salestax"),
    "fee": ("fee", "fees", "servicefee", "feeamount"),
    "adjustment": ("adjustment", "adjustments", "correction"),
    "billed_amount": (
        "billedamount",
        "total",
        "totalamount",
        "invoicetotal",
        "amountdue",
        "grandtotal",
    ),
    "currency": ("currency", "currencycode", "isocurrency"),
    "statement_id": ("statementid", "invoiceid", "invoicenumber", "invoice"),
    "line_item_id": ("lineitemid", "lineid", "recordid", "id", "usageid"),
}

#: Categories that are financial rather than usage. A line in one of these never
#: contributes to the usage comparison, however its amount column is filled in.
NON_USAGE_CATEGORIES = {
    "tax": "tax",
    "vat": "tax",
    "salestax": "tax",
    "gst": "tax",
    "credit": "credit",
    "credits": "credit",
    "discount": "credit",
    "promotion": "credit",
    "promotionalcredit": "credit",
    "refund": "credit",
    "fee": "fee",
    "fees": "fee",
    "servicefee": "fee",
    "platformfee": "fee",
    "adjustment": "adjustment",
    "correction": "adjustment",
    "truedown": "adjustment",
    "trueup": "adjustment",
}

#: Date formats, in order. ISO first, then the US month/day order the supported
#: providers emit. Day/month is deliberately absent: 05/04/2026 cannot be read
#: both ways, and guessing per row would move usage across a month boundary —
#: which is the one error a reconciliation tool must not make quietly. An export
#: using day/month must be mapped from an ISO column, or converted first.
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%m/%d/%Y",
    "%m-%d-%Y",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%b %d, %Y",
    "%d %b %Y",
)

#: Leading characters a spreadsheet reads as a formula.
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def neutralise(value: str) -> str:
    """Make a cell inert for a spreadsheet, for previews and exports alike.

    A cell beginning =, +, -, @ (or a control character) is executed on open by
    Excel, Sheets and Numbers. Prefixing a single quote makes it text. This is
    applied on the way OUT — what is stored stays exactly as the provider wrote
    it, because the stored copy is the audit trail.
    """
    if value and value[0] in _FORMULA_LEAD:
        return "'" + value
    return value


def _key(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (header or "").lower())


def suggest_mapping(headers: list[str]) -> dict[str, Optional[str]]:
    """Guess which column holds each field. A starting point, never a decision:
    the guess is shown for confirmation before anything is imported."""
    by_key = {}
    for header in headers:
        by_key.setdefault(_key(header), header)
    mapping: dict[str, Optional[str]] = {}
    used: set[str] = set()
    for field, hints in HINTS.items():
        for hint in hints:
            header = by_key.get(hint)
            if header and header not in used:
                mapping[field] = header
                used.add(header)
                break
        else:
            mapping[field] = None
    return mapping


def parse_date(value: Any) -> Optional[dt.date]:
    """A date from a statement cell, or None. Dates are taken at face value in
    the provider's own terms — no timezone is applied, because a billing export
    states a billing day, not an instant, and shifting it would move usage
    across a period boundary that the provider did not move it across."""
    if isinstance(value, dt.date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(text[: len(fmt) + 8], fmt).date()
        except ValueError:
            continue
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


def safe_filename(name: str) -> str:
    """A display name, never a path. Directory separators and traversal are
    stripped rather than escaped, because this string is only ever shown."""
    cleaned = re.sub(r"[^A-Za-z0-9._ -]", "_", (name or "").replace("\\", "/").split("/")[-1])
    cleaned = cleaned.replace("..", "_").strip() or "import.csv"
    return cleaned[:200]


def checksum(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()


def read_csv(content: str) -> tuple:
    """(headers, rows) from CSV text. Raises ReconciliationError on anything
    that is not a usable table."""
    if not content.strip():
        raise ReconciliationError("The file is empty.")
    if len(content.encode("utf-8", "replace")) > MAX_BYTES:
        raise ReconciliationError(f"File is larger than {MAX_BYTES // (1024 * 1024)}MB.")
    # A BOM from a spreadsheet export would otherwise become part of the first
    # header name and break every mapping guess.
    text = content.lstrip("﻿")
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    try:
        headers = next(reader)
    except StopIteration:
        raise ReconciliationError("The file has no header row.") from None
    headers = [h.strip()[:MAX_CELL] for h in headers]
    if len(headers) > MAX_COLUMNS:
        raise ReconciliationError(f"More than {MAX_COLUMNS} columns.")
    if not any(headers):
        raise ReconciliationError("The header row is blank.")

    rows = []
    for raw in reader:
        if len(rows) >= MAX_ROWS:
            raise ReconciliationError(f"More than {MAX_ROWS} rows; split the export.")
        if not any((cell or "").strip() for cell in raw):
            continue  # blank line, common at the end of an export
        rows.append([(cell or "")[:MAX_CELL] for cell in raw])
    if not rows:
        raise ReconciliationError("The file has a header but no rows.")
    return headers, rows


def _cell(row: list, index: Optional[int]) -> str:
    if index is None or index >= len(row):
        return ""
    return (row[index] or "").strip()


def normalise(
    headers: list[str], rows: list[list], mapping: dict, default_currency: str = "USD"
) -> list[dict]:
    """Turn source rows into the module's own shape.

    Every row comes back, valid or not, each carrying its own errors — so the
    caller can show what would be imported and what would be rejected before
    committing either.
    """
    index = {
        field: (headers.index(col) if col in headers else None)
        for field, col in mapping.items()
        if col
    }
    out = []
    for number, row in enumerate(rows, start=1):
        errors: list[str] = []
        service_date = parse_date(_cell(row, index.get("service_date")))
        period_start = parse_date(_cell(row, index.get("period_start")))
        period_end = parse_date(_cell(row, index.get("period_end")))
        if service_date is None and period_start is None:
            errors.append("No usable date")

        subtotal_raw = _cell(row, index.get("usage_subtotal"))
        subtotal = money(subtotal_raw, None)
        billed = money(_cell(row, index.get("billed_amount")), None)
        if subtotal is None and billed is None:
            errors.append("No usable amount")

        category = _cell(row, index.get("usage_category"))
        kind = NON_USAGE_CATEGORIES.get(_key(category))

        credit = money(_cell(row, index.get("credit"))) or ZERO
        tax = money(_cell(row, index.get("tax"))) or ZERO
        fee = money(_cell(row, index.get("fee"))) or ZERO
        adjustment = money(_cell(row, index.get("adjustment"))) or ZERO
        usage = subtotal if subtotal is not None else ZERO

        # A row whose category says it is tax (or a credit, or a fee) is that,
        # whatever column its amount happened to arrive in. Letting a tax line
        # count as usage is exactly the error this module exists to prevent.
        if kind:
            amount = usage if usage != 0 else (billed or ZERO)
            usage = ZERO
            if kind == "tax":
                tax = tax or amount
            elif kind == "credit":
                credit = credit or amount
            elif kind == "fee":
                fee = fee or amount
            else:
                adjustment = adjustment or amount

        currency = (_cell(row, index.get("currency")) or default_currency).upper()[:8]
        if not re.fullmatch(r"[A-Z]{3}", currency):
            currency = default_currency.upper()

        quantity = money(_cell(row, index.get("quantity")), None)

        out.append(
            {
                "row_number": number,
                "service_date": service_date,
                "period_start": period_start,
                "period_end": period_end,
                "provider_account": _cell(row, index.get("provider_account")) or None,
                "api_key_ref": _cell(row, index.get("api_key_ref")) or None,
                "model": _cell(row, index.get("model")) or None,
                "usage_category": category or ("usage" if not kind else kind),
                "quantity": quantity,
                "usage_subtotal": quantize(usage),
                "credit": quantize(credit),
                "tax": quantize(tax),
                "fee": quantize(fee),
                "adjustment": quantize(adjustment),
                # With no explicit total column, the line's own parts are its total —
                # so a tax or credit line still contributes to the invoice total
                # while contributing nothing to usage.
                "billed_amount": quantize(
                    billed if billed is not None else usage + credit + tax + fee + adjustment
                ),
                "currency": currency,
                "statement_id": _cell(row, index.get("statement_id")) or None,
                "line_item_id": _cell(row, index.get("line_item_id")) or None,
                # Only the columns that were mapped. The rest of the file — which
                # may carry contact or account-manager details — is not stored.
                "raw": {field: _cell(row, idx) for field, idx in index.items()},
                "mapping_status": "rejected" if errors else "ok",
                "mapping_errors": errors,
            }
        )
    return out


def preview(content: str, mapping: Optional[dict] = None, default_currency: str = "USD") -> dict:
    """What this file would import, without importing it."""
    headers, rows = read_csv(content)
    suggested = suggest_mapping(headers)
    chosen = {**suggested, **{k: v for k, v in (mapping or {}).items() if v}}
    missing = [f for f in REQUIRED if not chosen.get(f)]
    normalised = normalise(headers, rows, chosen, default_currency) if not missing else []

    accepted = [r for r in normalised if r["mapping_status"] == "ok"]
    rejected = [r for r in normalised if r["mapping_status"] != "ok"]
    dates = [d for d in (r["service_date"] or r["period_start"] for r in accepted) if d]
    currencies = sorted({r["currency"] for r in accepted})

    return {
        "headers": [neutralise(h) for h in headers],
        "suggested_mapping": suggested,
        "mapping": chosen,
        "missing_required": missing,
        "field_help": FIELDS,
        "row_count": len(rows),
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "currencies": currencies,
        "period_start": min(dates).isoformat() if dates else None,
        "period_end": max(dates).isoformat() if dates else None,
        "usage_subtotal": float(sum((r["usage_subtotal"] for r in accepted), ZERO)),
        "credits": float(sum((r["credit"] for r in accepted), ZERO)),
        "tax": float(sum((r["tax"] for r in accepted), ZERO)),
        "fees": float(sum((r["fee"] + r["adjustment"] for r in accepted), ZERO)),
        "billed_total": float(sum((r["billed_amount"] for r in accepted), ZERO)),
        "rows": [_preview_row(r) for r in normalised[:PREVIEW_ROWS]],
        "rejected_rows": [_preview_row(r) for r in rejected[:PREVIEW_ROWS]],
        "checksum": checksum(content),
    }


def _preview_row(row: dict) -> dict:
    """One normalised row, safe to render. Every string is neutralised so a
    preview cannot become a live formula in whatever the reader pastes it into."""
    return {
        "row_number": row["row_number"],
        "service_date": row["service_date"].isoformat() if row["service_date"] else None,
        "provider_account": neutralise(row["provider_account"] or ""),
        "api_key_ref": neutralise(row["api_key_ref"] or ""),
        "model": neutralise(row["model"] or ""),
        "usage_category": neutralise(row["usage_category"] or ""),
        "quantity": float(row["quantity"]) if row["quantity"] is not None else None,
        "usage_subtotal": float(row["usage_subtotal"]),
        "credit": float(row["credit"]),
        "tax": float(row["tax"]),
        "fee": float(row["fee"]),
        "adjustment": float(row["adjustment"]),
        "billed_amount": float(row["billed_amount"]),
        "currency": row["currency"],
        "status": row["mapping_status"],
        "errors": row["mapping_errors"],
    }


def commit(
    tenant_id: str,
    *,
    provider: str,
    filename: str,
    content: str,
    mapping: Optional[dict] = None,
    provider_account: Optional[str] = None,
    actor: Optional[str] = None,
    replace_import_id: Optional[str] = None,
) -> dict:
    """Store an export. Repeating the same file is a no-op, not a duplicate.

    Identity is the file's checksum for the provider. Re-importing a file
    already committed returns the existing import untouched, so a retried or
    double-clicked upload cannot produce two statements for one month.
    """
    result = preview(content, mapping)
    if result["missing_required"]:
        raise ReconciliationError(
            "Map these columns before importing: " + ", ".join(result["missing_required"])
        )
    headers, rows = read_csv(content)
    normalised = normalise(headers, rows, result["mapping"])
    accepted = [r for r in normalised if r["mapping_status"] == "ok"]
    if not accepted:
        raise ReconciliationError("No row in this file could be read; check the column mapping.")

    digest = result["checksum"]
    currency = result["currencies"][0] if len(result["currencies"]) == 1 else "MIXED"

    with tenant_conn(tenant_id) as conn:
        existing = conn.execute(
            "SELECT id FROM recon_import WHERE provider = %s AND checksum = %s "
            "AND status = 'committed'",
            (provider, digest),
        ).fetchone()
        if existing:
            # Idempotent: the same bytes for the same provider are the same
            # statement. Say so rather than storing it twice.
            record_audit(
                conn,
                tenant_id,
                "import_duplicate_ignored",
                actor=actor,
                import_id=str(existing[0]),
                detail={"checksum": digest},
            )
            return {**_import_row(conn, str(existing[0])), "duplicate": True}

        if replace_import_id:
            conn.execute(
                "UPDATE recon_import SET status = 'superseded' "
                "WHERE id = %s AND status = 'committed'",
                (replace_import_id,),
            )
            record_audit(
                conn,
                tenant_id,
                "import_replaced",
                actor=actor,
                import_id=replace_import_id,
                detail={"replaced_by_checksum": digest},
            )

        import_id = conn.execute(
            """
            INSERT INTO recon_import (tenant_id, provider, provider_account, filename, checksum,
                                      currency, period_start, period_end, imported_by,
                                      row_count, rejected_count, metadata, validation_errors)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                tenant_id,
                provider,
                provider_account,
                safe_filename(filename),
                digest,
                currency,
                result["period_start"],
                result["period_end"],
                actor,
                len(accepted),
                result["rejected_count"],
                json.dumps({"mapping": result["mapping"], "source_headers": len(headers)}),
                json.dumps(
                    [
                        {"row": r["row_number"], "errors": r["mapping_errors"]}
                        for r in normalised
                        if r["mapping_status"] != "ok"
                    ][:200]
                ),
            ),
        ).fetchone()[0]

        for row in normalised:
            conn.execute(
                """
                INSERT INTO recon_line_item
                    (tenant_id, import_id, row_number, service_date, period_start, period_end,
                     provider_account, api_key_ref, model, usage_category, quantity,
                     usage_subtotal, credit, tax, fee, adjustment, billed_amount, currency,
                     statement_id, line_item_id, raw, mapping_status, mapping_errors)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s)
                """,
                (
                    tenant_id,
                    import_id,
                    row["row_number"],
                    row["service_date"],
                    row["period_start"],
                    row["period_end"],
                    row["provider_account"],
                    row["api_key_ref"],
                    row["model"],
                    row["usage_category"],
                    row["quantity"],
                    row["usage_subtotal"],
                    row["credit"],
                    row["tax"],
                    row["fee"],
                    row["adjustment"],
                    row["billed_amount"],
                    row["currency"],
                    row["statement_id"],
                    row["line_item_id"],
                    json.dumps(row["raw"]),
                    row["mapping_status"],
                    json.dumps(row["mapping_errors"]),
                ),
            )

        record_audit(
            conn,
            tenant_id,
            "import_created",
            actor=actor,
            import_id=str(import_id),
            detail={
                "filename": safe_filename(filename),
                "rows": len(accepted),
                "rejected": result["rejected_count"],
                "provider": provider,
            },
        )
        return {**_import_row(conn, str(import_id)), "duplicate": False}


def remove(tenant_id: str, import_id: str, *, actor: Optional[str] = None) -> dict:
    """Recoverable removal: the import is marked removed and stops being used,
    and every row of it stays exactly where it is."""
    with tenant_conn(tenant_id) as conn:
        updated = conn.execute(
            "UPDATE recon_import SET status = 'removed', removed_at = now() "
            "WHERE id = %s AND status <> 'removed' RETURNING id",
            (import_id,),
        ).fetchone()
        if not updated:
            raise ReconciliationError("That import does not exist, or is already removed.")
        record_audit(conn, tenant_id, "import_removed", actor=actor, import_id=import_id)
        return _import_row(conn, import_id)


def _import_row(conn, import_id: str) -> dict:
    row = conn.execute(
        "SELECT id, provider, provider_account, filename, checksum, status, currency, "
        "period_start, period_end, imported_by, imported_at, row_count, rejected_count, "
        "metadata, validation_errors, removed_at "
        "FROM recon_import WHERE id = %s",
        (import_id,),
    ).fetchone()
    runs = conn.execute(
        "SELECT COUNT(*) FROM recon_run WHERE import_id = %s", (import_id,)
    ).fetchone()[0]
    return {
        "id": str(row[0]),
        "provider": row[1],
        "provider_account": row[2],
        "filename": row[3],
        "checksum": row[4],
        "status": row[5],
        "currency": row[6],
        "period_start": row[7].isoformat() if row[7] else None,
        "period_end": row[8].isoformat() if row[8] else None,
        "imported_by": row[9],
        "imported_at": row[10].isoformat() if row[10] else None,
        "row_count": row[11],
        "rejected_count": row[12],
        "mapping": (row[13] or {}).get("mapping", {}),
        "validation_errors": row[14] or [],
        "removed_at": row[15].isoformat() if row[15] else None,
        "run_count": int(runs),
    }


def history(tenant_id: str, limit: int = 50) -> list[dict]:
    with tenant_conn(tenant_id) as conn:
        ids = [
            str(r[0])
            for r in conn.execute(
                "SELECT id FROM recon_import ORDER BY imported_at DESC LIMIT %s", (limit,)
            ).fetchall()
        ]
        return [_import_row(conn, i) for i in ids]
