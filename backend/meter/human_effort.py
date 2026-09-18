"""Human effort — the people half of what it costs to serve a customer.

Meter's whole premise is that a blended AI bill hides where the money goes. For
a company selling an AI product, the model bill hides something else: a customer
who is cheap to run can still be expensive to keep, because an engineer spent
three days on their workflow and someone sat on the deployment call. That labour
has never been in this product, so a low-inference customer looked profitable
whether it was or not.

**What this is not.** It is not build cost — that is what the team spent MAKING
a feature, it has no customer, and nothing here touches it. It is not inference,
and it never reaches inference_cost, the provider reconciliation or the
Unattributed bucket. It is a third category, stored on its own, added to metered
inference only at read time and only under a label that says what the sum is.

**Where the numbers come from.** A CSV the tenant exports from wherever they
already track time, carrying its own loaded hourly rate per row. Meter does not
estimate hours, infer them from commits, or hold a rate table: all three would
turn a stated fact into a guess, and a guess about somebody's salary is the kind
of number a CFO is right to throw the whole product out over.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import uuid
from decimal import Decimal, InvalidOperation
from typing import Optional

from .db import app_dsn, connect, tenant_tx

#: The vocabulary the migration's CHECK enforces, repeated here so a bad row is
#: rejected with a readable message instead of an integrity error.
ACTIVITY_TYPES = ("development", "support", "review", "rework", "other")

#: Big enough for a year of a large team's timesheets, small enough that a file
#: chosen by accident is refused rather than parsed. Matches BuildImportRequest.
MAX_CSV_BYTES = 5_000_000
#: A guard on row count as well as bytes: a file of 200k one-character rows is
#: small and still not a timesheet.
MAX_ROWS = 50_000

#: Sanity bounds. Not business rules — a day has 24 hours, and a four-figure
#: hourly rate is a decimal point in the wrong place.
MAX_HOURS_PER_ROW = Decimal("24")
MAX_RATE = Decimal("10000")


class EffortImportError(Exception):
    """The CSV cannot be used. The message is shown to the customer verbatim."""


class DuplicateImport(Exception):
    """This exact file has been imported before."""

    def __init__(self, imported_at: dt.datetime, row_count: int):
        self.imported_at = imported_at
        self.row_count = row_count
        super().__init__(
            f"This file was already imported on "
            f"{imported_at.strftime('%-d %b %Y at %H:%M UTC')} ({row_count} rows). "
            "Nothing was added. Edit the file or import a different one."
        )


def _checksum(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def _parse_date(raw: str, row: int) -> dt.date:
    """ISO dates only — `2026-09-15`.

    Deliberately not the flexible matcher `infra_csv` uses. That one has to read
    a bill whose format nobody controls, and it resolves an ambiguous
    `03/04/2026` by inspecting the whole column. This file is produced to a
    documented shape by the person importing it, so guessing between a US and a
    European reading would risk silently filing a quarter's labour in the wrong
    month to save them typing a hyphen.
    """
    text = raw.strip()
    try:
        parsed = dt.date.fromisoformat(text)
    except ValueError as exc:
        raise EffortImportError(f"Row {row}: date must be YYYY-MM-DD, got '{raw}'.") from exc
    if not (dt.date(2000, 1, 1) <= parsed <= dt.date.today() + dt.timedelta(days=366)):
        raise EffortImportError(f"Row {row}: date '{text}' is outside the range Meter accepts.")
    return parsed


def _decimal(raw: str, label: str, row: int) -> Decimal:
    try:
        value = Decimal(raw.strip().replace("$", "").replace(",", ""))
    except (InvalidOperation, AttributeError) as exc:
        raise EffortImportError(f"Row {row}: invalid {label} '{raw}'.") from exc
    if not value.is_finite():
        raise EffortImportError(f"Row {row}: invalid {label} '{raw}'.")
    return value


def parse_csv(text: str) -> list[dict]:
    """Read a timesheet export into validated rows, or raise on the first fault.

    Nothing is written from here. The whole file is validated before the caller
    opens a transaction, so an error in the last row cannot leave the first ones
    committed — a half-loaded month is worse than a rejected one, because the
    total looks plausible.
    """
    if len(text.encode("utf-8")) > MAX_CSV_BYTES:
        raise EffortImportError("That file is too large to import (limit 5 MB).")

    reader = csv.DictReader(io.StringIO(text.strip()))
    if reader.fieldnames is None:
        raise EffortImportError("CSV has no header row.")
    fields = {name.strip().lower(): name for name in reader.fieldnames}

    required = ("date", "person", "customer_id", "hours", "activity_type", "loaded_hourly_rate")
    missing = [name for name in required if name not in fields]
    if missing:
        raise EffortImportError(
            "CSV is missing required column"
            f"{'s' if len(missing) > 1 else ''}: {', '.join(missing)}. "
            f"Required: {', '.join(required)}."
        )

    def pick(row, name) -> Optional[str]:
        raw = row.get(fields[name]) if name in fields else None
        return raw.strip() if isinstance(raw, str) and raw.strip() else None

    rows: list[dict] = []
    for i, raw_row in enumerate(reader, start=2):  # row 1 is the header
        if not any((value or "").strip() for value in raw_row.values() if isinstance(value, str)):
            continue  # a trailing blank line is not an error
        if len(rows) >= MAX_ROWS:
            raise EffortImportError(f"That file has more than {MAX_ROWS:,} rows.")

        for name in required:
            if pick(raw_row, name) is None:
                raise EffortImportError(f"Row {i}: missing {name}.")

        hours = _decimal(pick(raw_row, "hours"), "hours", i)
        if hours <= 0:
            raise EffortImportError(f"Row {i}: hours must be greater than 0, got '{hours}'.")
        if hours > MAX_HOURS_PER_ROW:
            raise EffortImportError(f"Row {i}: {hours} hours in one day is not possible.")

        rate = _decimal(pick(raw_row, "loaded_hourly_rate"), "loaded_hourly_rate", i)
        if rate < 0:
            raise EffortImportError(f"Row {i}: loaded_hourly_rate cannot be negative.")
        if rate > MAX_RATE:
            raise EffortImportError(
                f"Row {i}: loaded_hourly_rate of {rate} looks like a typo — "
                f"the limit is {MAX_RATE}."
            )

        activity = pick(raw_row, "activity_type").lower()
        if activity not in ACTIVITY_TYPES:
            raise EffortImportError(
                f"Row {i}: activity_type must be one of {', '.join(ACTIVITY_TYPES)}, "
                f"got '{activity}'."
            )

        customer_id = pick(raw_row, "customer_id")
        person = pick(raw_row, "person")
        for label, value in (("customer_id", customer_id), ("person", person)):
            if len(value) > 200:
                raise EffortImportError(f"Row {i}: {label} is longer than 200 characters.")

        feature_raw = pick(raw_row, "feature_id")
        feature_id = None
        if feature_raw:
            try:
                feature_id = str(uuid.UUID(feature_raw))
            except ValueError as exc:
                raise EffortImportError(
                    f"Row {i}: feature_id '{feature_raw}' is not a valid id. "
                    "Leave it blank if the work does not map to a feature."
                ) from exc

        note = pick(raw_row, "note")
        if note and len(note) > 500:
            raise EffortImportError(f"Row {i}: note is longer than 500 characters.")

        rows.append(
            {
                "work_date": _parse_date(pick(raw_row, "date"), i),
                "customer_id": customer_id,
                "person_label": person,
                "feature_id": feature_id,
                "activity_type": activity,
                # Quantized on the way in so the stored value and the value that
                # priced it are the same number the column will hold.
                "hours": hours.quantize(Decimal("0.01")),
                "loaded_hourly_rate": rate.quantize(Decimal("0.0001")),
                "note": note,
                "row": i,
            }
        )

    if not rows:
        raise EffortImportError("CSV has no rows.")
    return rows


def import_csv(tenant_id: str, text: str) -> dict:
    """Validate a whole file, then write it in one transaction.

    Refuses a file whose content has been imported before: the failure this
    guards is a person clicking Import twice, which would double a customer's
    labour cost with nothing downstream able to notice.
    """
    rows = parse_csv(text)
    checksum = _checksum(text)
    batch_id = str(uuid.uuid4())

    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        prior = conn.execute(
            "SELECT imported_at, row_count FROM human_effort_import WHERE checksum = %s LIMIT 1",
            (checksum,),
        ).fetchone()
        if prior:
            raise DuplicateImport(prior[0], prior[1])

        # Feature ids are validated against THIS tenant's features before any
        # insert. The FK alone would not do it: a feature id belonging to another
        # tenant is invisible under RLS, so the insert would fail with an opaque
        # constraint error instead of naming the row.
        wanted = {r["feature_id"] for r in rows if r["feature_id"]}
        if wanted:
            known = {
                str(fid)
                for (fid,) in conn.execute(
                    "SELECT id FROM feature WHERE id = ANY(%s)", (list(wanted),)
                ).fetchall()
            }
            for row in rows:
                if row["feature_id"] and row["feature_id"] not in known:
                    raise EffortImportError(
                        f"Row {row['row']}: feature_id '{row['feature_id']}' is not a feature "
                        "in this organization."
                    )

        conn.execute(
            "INSERT INTO human_effort_import (id, tenant_id, checksum, row_count) "
            "VALUES (%s, %s, %s, %s)",
            (batch_id, tenant_id, checksum, len(rows)),
        )
        for row in rows:
            conn.execute(
                """
                INSERT INTO human_effort
                    (tenant_id, work_date, customer_id, person_label, feature_id,
                     activity_type, hours, loaded_hourly_rate, source, note, import_batch_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'csv', %s, %s)
                """,
                (
                    tenant_id,
                    row["work_date"],
                    row["customer_id"],
                    row["person_label"],
                    row["feature_id"],
                    row["activity_type"],
                    row["hours"],
                    row["loaded_hourly_rate"],
                    row["note"],
                    batch_id,
                ),
            )

    hours = sum((r["hours"] for r in rows), Decimal("0"))
    cost = sum((r["hours"] * r["loaded_hourly_rate"] for r in rows), Decimal("0"))
    return {
        "imported": len(rows),
        "batch_id": batch_id,
        "customers": len({r["customer_id"] for r in rows}),
        "hours": float(hours),
        "cost": float(cost.quantize(Decimal("0.01"))),
        "first_date": min(r["work_date"] for r in rows).isoformat(),
        "last_date": max(r["work_date"] for r in rows).isoformat(),
    }


def recent(tenant_id: str, limit: int = 50) -> list[dict]:
    """The newest effort rows, for showing what an import actually loaded."""
    limit = max(1, min(int(limit), 500))
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        rows = conn.execute(
            """
            SELECT h.work_date, h.customer_id, h.person_label, h.activity_type,
                   h.hours, h.loaded_hourly_rate, h.note, f.name
            FROM human_effort h LEFT JOIN feature f ON f.id = h.feature_id
            ORDER BY h.work_date DESC, h.created_at DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
    return [
        {
            "work_date": r[0].isoformat(),
            "customer_id": r[1],
            "person_label": r[2],
            "activity_type": r[3],
            "hours": float(r[4]),
            "loaded_hourly_rate": float(r[5]),
            "cost": float((r[4] * r[5]).quantize(Decimal("0.01"))),
            "note": r[6],
            "feature_name": r[7],
        }
        for r in rows
    ]
