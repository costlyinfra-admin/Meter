"""Importing a cloud bill from a CSV the customer downloaded.

Some platforms publish an authoritative invoice you can download but nothing you
can fetch. Redis Cloud has a per-subscription cost report, Supabase shows
invoices in its dashboard, Neon reports consumption you would otherwise have to
price yourself. Building API connectors for those would mean modelling cost from
a price list and presenting the result as a bill; this reads the real numbers
instead, at the cost of somebody clicking Download once a month.

**Why one parser and not three.** The obvious design is a parser per vendor,
keyed to that vendor's exact column names. Those names cannot be verified
without an account with each of them, and a hard-coded schema that is subtly
wrong fails the worst possible way — it imports confidently and silently drops
or mis-reads columns. So this matches headers by meaning, accepts what any
reasonable billing export calls things, and REPORTS what it matched. A customer
sees "Cost -> amount, Usage Date -> date" before anything is written, and a
column we did not understand is named rather than ignored.

The same parser therefore works for a vendor nobody has thought of yet, which is
the point: this path exists precisely for bills we cannot fetch.

Only two things are required — a date and an amount. Everything else improves
attribution when present and is absent honestly when not.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional

from .providers import CloudCostItem


class CsvImportError(Exception):
    """The CSV cannot be read. The message is shown to the customer verbatim."""


#: What a column might be called, by what it means. Matched case-insensitively
#: with punctuation and spacing ignored, so "Charge Period Start", "charge_period
#: _start" and "chargeperiodstart" are one name. Order matters: the first alias
#: present wins, so the most specific names lead.
_ALIASES: dict[str, tuple[str, ...]] = {
    "date": (
        "chargeperiodstart",
        "usagedate",
        "usagestartdate",
        "startdate",
        "date",
        "day",
        "billingperiodstart",
        "billingperiod",
        "period",
        "month",
        "invoicedate",
        "timestamp",
    ),
    "amount": (
        "billedcost",
        "totalcost",
        "totalprice",
        "amountusd",
        "amount",
        "cost",
        "charge",
        "charges",
        "price",
        "total",
        "effectivecost",
        "spend",
    ),
    "service": (
        "servicename",
        "service",
        "product",
        "productname",
        "sku",
        "skuid",
        "item",
        "lineitem",
        "resourcetype",
        "chargedescription",
        "description",
        "type",
    ),
    "currency": ("billingcurrency", "currency", "currencycode"),
    "tag": (
        "feature",
        "tag",
        "label",
        "project",
        "projectname",
        "subscription",
        "subscriptionname",
        "organization",
        "team",
    ),
    "usage_type": ("usagetype", "meter", "metername", "unit", "database", "databasename"),
    "region": ("region", "regionid", "regionname", "location"),
    "account": ("account", "accountname", "accountid", "subaccountname", "subaccountid"),
}

#: Columns without which there is no line item to write.
REQUIRED = ("date", "amount")

_NORMALIZE = re.compile(r"[^a-z0-9]+")
#: Currency tokens billing exports tack onto a money column — "Cost (USD)",
#: "Amount USD", "Total EUR". Stripped so the column still reads as its meaning.
_CURRENCY_SUFFIX = re.compile(r"(?:in)?(?:usd|eur|gbp|cad|aud|inr|jpy|chf|sek|dollars?|euros?)$")
#: Currency symbols, thousands separators and stray whitespace around a number.
_MONEY_STRIP = re.compile(r"[^0-9.\-()]")


def _normalize(header: str) -> str:
    return _NORMALIZE.sub("", (header or "").strip().lower())


def _core(header: str) -> str:
    """The header's meaning with a trailing currency token removed.

    "Cost (USD)" and "Cost" are the same column asked for twice; without this
    the first one falls through to "no amount column found", which is a
    confusing thing to tell someone looking at a column plainly labelled Cost.
    """
    normalized = _normalize(header)
    stripped = _CURRENCY_SUFFIX.sub("", normalized)
    return stripped or normalized


@dataclass
class CsvReport:
    """What a parse found, for the customer to confirm before anything is written."""

    items: list
    #: meaning -> the header it was taken from, e.g. {"amount": "Cost (USD)"}.
    mapping: dict
    #: Headers present in the file that carried no meaning we recognised.
    unmapped: list
    rows_read: int
    rows_skipped: int
    #: Why rows were skipped, most common first — never a silent drop.
    warnings: list
    total: Decimal
    currency: str
    first_day: Optional[dt.date]
    last_day: Optional[dt.date]

    def as_dict(self) -> dict:
        return {
            "rows_read": self.rows_read,
            "rows_imported": len(self.items),
            "rows_skipped": self.rows_skipped,
            "mapping": self.mapping,
            "unmapped_columns": self.unmapped,
            "warnings": self.warnings,
            "total": float(self.total),
            "currency": self.currency,
            "from": self.first_day.isoformat() if self.first_day else None,
            "to": self.last_day.isoformat() if self.last_day else None,
        }


def _parse_money(raw: str) -> Optional[Decimal]:
    """A billing amount from a spreadsheet cell.

    Handles "$1,234.56", "1 234,56"-free plain forms, and accountancy negatives
    written as "(25.00)" — a credit is real money and must not be read as a
    positive charge.
    """
    text = (raw or "").strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    cleaned = _MONEY_STRIP.sub("", text).replace("(", "").replace(")", "")
    if cleaned in ("", "-", "."):
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    return -value if negative else value


#: Unambiguous formats, tried before anything with a guessable day/month order.
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d-%b-%Y", "%b %d, %Y", "%d %b %Y", "%Y-%m")
#: "05/04/2026" — two numbers and a year, where the order is a convention.
_SLASHED = re.compile(r"^\s*(\d{1,2})[/.](\d{1,2})[/.](\d{2,4})\s*$")


def _slashed_order(values: list) -> tuple[str, Optional[str]]:
    """Whether a column of "05/04/2026" dates is day-first or month-first.

    Inferred from the whole column rather than assumed, because the two readings
    put the same spend in different MONTHS and picking one silently is exactly
    the black-box behaviour this product exists to replace. A value with a first
    component above 12 can only be a day; one with a second component above 12
    can only be a day in the other position. If neither ever appears the file is
    genuinely ambiguous, and the caller is told so rather than reassured.
    """
    day_first = month_first = seen = False
    for raw in values:
        match = _SLASHED.match(str(raw or ""))
        if not match:
            continue
        seen = True
        first, second = int(match.group(1)), int(match.group(2))
        if first > 12:
            day_first = True
        if second > 12:
            month_first = True
    if day_first and month_first:
        raise CsvImportError(
            "The date column mixes day-first and month-first dates, so no reading of "
            "it is correct. Re-export with ISO dates (YYYY-MM-DD)."
        )
    if day_first:
        return "%d/%m/%Y", None
    if month_first:
        return "%m/%d/%Y", None
    if not seen:
        # No slashed dates at all: nothing to be ambiguous about.
        return "%m/%d/%Y", None
    return "%m/%d/%Y", (
        "Dates like 05/04/2026 are ambiguous and were read month-first "
        "(5 April would be 04/05/2026). Re-export with ISO dates to be certain."
    )


def _parse_day(raw: str, slashed_format: str = "%m/%d/%Y") -> Optional[dt.date]:
    text = (raw or "").strip()
    if not text:
        return None
    # An ISO timestamp: take the date part.
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            parsed = dt.datetime.strptime(text[: len(fmt) + 4], fmt)
        except ValueError:
            continue
        return parsed.date().replace(day=1) if fmt == "%Y-%m" else parsed.date()
    match = _SLASHED.match(text)
    if match:
        try:
            return dt.datetime.strptime(text.replace(".", "/"), slashed_format).date()
        except ValueError:
            return None
    return None


def _build_mapping(fieldnames: list, override: Optional[dict] = None) -> tuple[dict, list]:
    """(meaning -> header, headers we did not recognise).

    Detection is best-effort by design, and `override` is how a customer fixes
    it: the preview shows what was matched, and a column we guessed wrong is
    corrected by naming it rather than by renaming the file.
    """
    exact: dict = {}
    core: dict = {}
    for name in fieldnames:
        if name is None:
            continue
        # First header wins a duplicate; a later duplicate is reported as
        # unmapped rather than silently overwriting.
        exact.setdefault(_normalize(name), name)
        core.setdefault(_core(name), name)

    mapping: dict = {}
    claimed: set = set()

    for meaning, header in (override or {}).items():
        if meaning in _ALIASES and header in fieldnames:
            mapping[meaning] = header
            claimed.add(header)

    for meaning, aliases in _ALIASES.items():
        if meaning in mapping:
            continue
        for table in (exact, core):
            match = next(
                (table[a] for a in aliases if a in table and table[a] not in claimed), None
            )
            if match is not None:
                mapping[meaning] = match
                claimed.add(match)
                break
    unmapped = [name for name in fieldnames if name and name not in claimed]
    return mapping, unmapped


def parse(
    text: str,
    *,
    tag_key: str = "feature",
    mapping_override: Optional[dict] = None,
    default_service: str = "",
) -> CsvReport:
    """Read a downloaded bill. Raises CsvImportError on anything unreadable.

    Nothing is written here: this returns what it found so the caller can show
    it before committing. An import that surprises someone is an import that
    should have been previewed.
    """
    if not (text or "").strip():
        raise CsvImportError("The file is empty.")
    reader = csv.DictReader(io.StringIO(text.strip()))
    if not reader.fieldnames:
        raise CsvImportError("The file has no header row, so its columns cannot be identified.")

    mapping, unmapped = _build_mapping(list(reader.fieldnames), mapping_override)
    missing = [m for m in REQUIRED if m not in mapping]
    if missing:
        article = {"amount": "an amount", "date": "a date"}
        raise CsvImportError(
            "Could not identify "
            + " or ".join(article[m] for m in missing)
            + " column. The file has: "
            + ", ".join(str(f) for f in reader.fieldnames if f)
            + ". Tell us which column to use, or rename it and re-upload."
        )

    def cell(row: dict, meaning: str) -> str:
        header = mapping.get(meaning)
        value = row.get(header) if header else None
        if isinstance(value, str):
            return value.strip()
        return "" if value is None else str(value)

    # Materialised so the date column can be read as a whole before any row is
    # interpreted. The API caps the upload, so this is bounded.
    rows = [r for r in reader if any((v or "").strip() for v in r.values() if isinstance(v, str))]
    date_header = mapping["date"]
    slashed_format, ambiguity = _slashed_order([r.get(date_header) for r in rows])

    items: list = []
    skipped = 0
    reasons: dict = {}
    total = Decimal("0")
    currencies: set = set()
    days: list = []

    for row in rows:
        day = _parse_day(cell(row, "date"), slashed_format)
        amount = _parse_money(cell(row, "amount"))
        if day is None:
            skipped += 1
            reasons["unreadable date"] = reasons.get("unreadable date", 0) + 1
            continue
        if amount is None:
            skipped += 1
            reasons["unreadable amount"] = reasons.get("unreadable amount", 0) + 1
            continue
        if amount == 0:
            skipped += 1
            reasons["zero amount"] = reasons.get("zero amount", 0) + 1
            continue

        currency = (cell(row, "currency") or "USD").upper()[:8] or "USD"
        currencies.add(currency)
        tag_value = cell(row, "tag") or None
        # A bill with no service column still has a known service: whose bill it
        # is. Naming the platform is stating what we know, not inferring — and it
        # is the difference between the import classifying as infrastructure and
        # landing entirely in Unclassified.
        service = cell(row, "service") or default_service
        items.append(
            CloudCostItem(
                period=day,
                amount=amount,
                currency=currency,
                service=service,
                usage_type=cell(row, "usage_type") or None,
                account_id=cell(row, "account") or None,
                region=cell(row, "region") or None,
                tag_key=tag_key,
                tag_value=tag_value,
                # The row as imported, so a number can be traced to its line in
                # the file the customer uploaded.
                dimensions={
                    "service": service,
                    "usage_type": cell(row, "usage_type") or None,
                    f"TAG:{tag_key}": tag_value,
                    "imported_from": "csv",
                },
            )
        )
        total += amount
        days.append(day)

    if not items:
        detail = "; ".join(f"{n} with an {why}" for why, n in reasons.items())
        raise CsvImportError(
            "No rows could be imported." + (f" Skipped {detail}." if detail else "")
        )

    warnings = [
        f"{n} row{'s' if n != 1 else ''} skipped: {why}" for why, n in sorted(reasons.items())
    ]
    if len(currencies) > 1:
        # Summing mixed currencies would produce a number that means nothing.
        warnings.append(
            "The file mixes " + ", ".join(sorted(currencies)) + "; amounts are stored as given "
            "and totals across currencies will not be meaningful."
        )
    if ambiguity:
        warnings.append(ambiguity)
    if unmapped:
        warnings.append("Columns not used: " + ", ".join(str(u) for u in unmapped if u))

    return CsvReport(
        items=items,
        mapping={meaning: header for meaning, header in mapping.items()},
        unmapped=[u for u in unmapped if u],
        rows_read=len(items) + skipped,
        rows_skipped=skipped,
        warnings=warnings,
        total=total,
        currency=sorted(currencies)[0] if len(currencies) == 1 else "mixed",
        first_day=min(days) if days else None,
        last_day=max(days) if days else None,
    )
