"""FOCUS — the FinOps Open Cost and Usage Specification.

A billing export in FOCUS format has the same columns whatever produced it, so
AWS, Azure, GCP, OCI, Snowflake and Datadog all arrive looking alike. That is
the opposite of the situation infra_csv.py was written for, and it is why this
module exists: that parser matches headers *by meaning* precisely because a
vendor's own column names cannot be verified without an account. FOCUS names can
be — they are published — so here we match them exactly and use what they mean.

What the spec gives us that a guessed schema cannot:

  * **Which dollars these are.** FOCUS carries up to four costs per row.
    `BilledCost` is what the invoice says; `EffectiveCost` amortizes a
    commitment purchase across the periods it covers; `ListCost` is before
    discounts; `ContractedCost` applies negotiated rates. Summing the wrong one
    is not a rounding error, it is a different question. Meter defaults to
    BilledCost because invariant 5 makes the provider's own invoice
    authoritative on dollars, and records which column it used.

  * **What kind of charge each row is.** `ChargeCategory` separates Usage and
    Purchase from Tax, Credit and Adjustment. A naive import sums all five and
    attributes sales tax to a service, which is why the preview reports the
    split before anything is written.

  * **Corrections.** A row whose `ChargeClass` is "Correction" restates a
    closed billing period. It is real money and is imported, but a month whose
    total moved after the fact should say so rather than quietly disagree with
    a figure somebody already wrote down.

  * **Tags as a map.** `Tags` is a JSON object, not a single value, so the one
    key a customer attributes by can be read out of it instead of being lost.

**Every released version, not only the newest.** Detection keys on
`ChargeCategory`, `ChargePeriodStart` and a cost column, all of which have been
present since 1.0, and the columns Meter reads — `ServiceName`,
`BillingCurrency`, `RegionId`, `SubAccountId`, `SkuId`, `Tags` and the four
costs — have been spelled the same way throughout. Where the spec did rename
something the old name is still accepted and stored under the current one:
`ProviderName` was removed in 1.3 and replaced by `ServiceProviderName`, so a
file from either era files the provider under the same key. Columns a vendor
added under the `x_` prefix the spec reserves are recognised as extensions
rather than as names we failed to understand.

This module knows the spec and nothing about Meter's storage — it reports what a
file contains and leaves the writing to infrastructure.py.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

#: The cost columns, in the order Meter prefers them when no choice is made.
#: BilledCost first: the invoice is what Meter reconciles against.
COST_COLUMNS: tuple[str, ...] = ("BilledCost", "EffectiveCost", "ListCost", "ContractedCost")

DEFAULT_COST_COLUMN = "BilledCost"

#: ChargeCategory's five allowed values (FOCUS 1.4). A row must carry one.
CATEGORIES: tuple[str, ...] = ("Usage", "Purchase", "Tax", "Credit", "Adjustment")

#: What the product is for: running something. Tax, Credit and Adjustment are
#: real money on the invoice but are not the cost of a service, and attributing
#: them to one would be inventing a number.
SERVICE_CATEGORIES: tuple[str, ...] = ("Usage", "Purchase")

#: ChargeClass's only non-null value: this row restates a closed period.
CORRECTION = "Correction"

#: Columns that together mean "this is FOCUS". All three are mandatory in every
#: released version, and ChargeCategory in particular is not a name another
#: billing export happens to use.
SIGNATURE: tuple[str, ...] = ("ChargeCategory", "ChargePeriodStart")

#: The prefix FOCUS reserves for vendor-specific columns. Expected, not unknown,
#: so they are reported as extensions rather than as columns we failed to read.
EXTENSION_PREFIX = "x_"

#: FOCUS column -> the meaning infra_csv's row builder works in. Several
#: spellings map to one meaning where the spec has used more than one.
_MEANINGS: dict[str, tuple[str, ...]] = {
    "date": ("ChargePeriodStart", "BillingPeriodStart"),
    "service": ("ServiceName",),
    "currency": ("BillingCurrency",),
    "region": ("RegionId", "RegionName"),
    "account": ("SubAccountId", "SubAccountName", "BillingAccountId"),
    "usage_type": ("SkuId", "SkuPriceId"),
}

#: Columns kept on the row for later reprocessing. Each is worth having because
#: something downstream can group or filter by it.
_DIMENSIONS: tuple[str, ...] = (
    "ChargeCategory",
    "ChargeClass",
    "ChargeDescription",
    "ServiceCategory",
    "ServiceName",
    "ServiceProviderName",
    "ProviderName",
    "InvoiceIssuerName",
    "ResourceId",
    "ResourceName",
    "ResourceType",
    "RegionId",
    "SubAccountId",
    "SkuId",
    "PricingCategory",
    "CommitmentDiscountId",
)

#: Columns the spec defines that Meter has nothing to do with. Listed so a
#: clean FOCUS file reports no unused columns at all: "ChargePeriodEnd was not
#: used" is true and useless, and it buries the warning that matters next to it.
_RECOGNISED_UNUSED: tuple[str, ...] = (
    "ChargePeriodEnd",
    "BillingPeriodEnd",
    "BillingAccountName",
    "BillingAccountType",
    "SubAccountType",
    "ChargeFrequency",
    "PricingQuantity",
    "PricingUnit",
    "ConsumedQuantity",
    "ConsumedUnit",
    "ContractedUnitPrice",
    "ListUnitPrice",
    "CommitmentDiscountName",
    "CommitmentDiscountType",
    "CommitmentDiscountCategory",
    "CommitmentDiscountStatus",
    "CapacityReservationId",
    "CapacityReservationStatus",
    "InvoiceId",
    "PricingCurrency",
    "PricingCurrencyEffectiveCost",
    "PricingCurrencyListCost",
    "PricingCurrencyContractedCost",
)

#: Columns the spec renamed, mapped to the name Meter stores them under. A file
#: is read whichever version produced it, and the stored row uses one key either
#: way — otherwise the same fact would be filed under two names depending on
#: which release the customer's exporter had caught up with.
_CANONICAL: dict[str, str] = {
    # ProviderName was removed in 1.3 and ServiceProviderName introduced in its
    # place. Both are accepted; both are stored as the current name.
    "ProviderName": "ServiceProviderName",
}

_NORMALIZE = re.compile(r"[^a-z0-9]+")


def _key(header: str) -> str:
    return _NORMALIZE.sub("", (header or "").strip().lower())


def column(row: dict, *names: str):
    """One FOCUS column out of a row, whatever the exporter did to its casing.

    Public because the Vercel connector reads FOCUS from an API rather than a
    file and must match names the same way this module does — two different
    normalisations would mean a column found on one path and missed on the
    other.
    """
    present = {_key(str(k)): v for k, v in (row or {}).items()}
    for name in names:
        if _key(name) in present:
            return present[_key(name)]
    return None


@dataclass
class Focus:
    """A file recognised as FOCUS, and how to read it.

    `headers` maps a canonical FOCUS column name to the header as it actually
    appeared, so a file that lowercased its columns on the way through a
    conversion still reads correctly.
    """

    headers: dict = field(default_factory=dict)
    #: Cost columns this file actually carries, in preference order.
    costs: tuple = ()
    #: Vendor columns under the x_ prefix. Expected; never a warning.
    extensions: tuple = ()

    def header(self, column: str) -> Optional[str]:
        return self.headers.get(column)

    def cost_column(self, preferred: Optional[str] = None) -> str:
        """Which cost column to read. The caller's choice if the file has it."""
        if preferred and preferred in self.costs:
            return preferred
        return self.costs[0]

    def mapping(self, cost_column: str) -> dict:
        """meaning -> actual header, for the row builder.

        `tag` points at the Tags column even though its value is read out of a
        JSON map rather than taken whole. The preview lists this mapping to say
        where each number came from, and showing the tag as "not used" would
        tell a customer their attribution had been dropped when it had not.
        """
        found = {"amount": self.headers[cost_column]}
        if "Tags" in self.headers:
            found["tag"] = self.headers["Tags"]
        for meaning, columns in _MEANINGS.items():
            for column in columns:
                if column in self.headers:
                    found[meaning] = self.headers[column]
                    break
        return found

    def dimensions(self, row: dict) -> dict:
        """The FOCUS columns worth keeping on the stored row.

        Filed under the current spec name even where the file used an older
        one, so a query written today works on last year's import.
        """
        kept = {}
        for column in _DIMENSIONS:
            header = self.headers.get(column)
            value = (row.get(header) or "").strip() if header else ""
            if value:
                kept.setdefault(_CANONICAL.get(column, column), value)
        return kept


def detect(fieldnames: list) -> Optional[Focus]:
    """A Focus for a FOCUS file, or None. Never raises on a file it cannot read."""
    present = {}
    for name in fieldnames or []:
        if name:
            present.setdefault(_key(name), name)

    if any(_key(column) not in present for column in SIGNATURE):
        return None
    costs = tuple(c for c in COST_COLUMNS if _key(c) in present)
    if not costs:
        # Every released version makes BilledCost mandatory, so a file with the
        # signature columns and no cost column is malformed rather than FOCUS.
        return None

    known = set(SIGNATURE) | set(COST_COLUMNS) | set(_DIMENSIONS) | set(_RECOGNISED_UNUSED)
    known.add("Tags")
    for columns in _MEANINGS.values():
        known.update(columns)
    headers = {column: present[_key(column)] for column in known if _key(column) in present}
    extensions = tuple(
        name for key, name in present.items() if key.startswith(_key(EXTENSION_PREFIX))
    )
    return Focus(headers=headers, costs=costs, extensions=extensions)


def read_tags(raw) -> dict:
    """The Tags column as a mapping. A value that is not readable JSON is not an
    error — the rest of the row is still a real charge."""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    # FOCUS gives a valueless tag key the boolean true, and allows a scheme
    # prefix ("someScheme/env"). Both are flattened to text here, because what
    # a customer attributes by is a string.
    return {str(k): ("" if v is True else str(v)) for k, v in parsed.items() if v is not None}


def tag_value(tags: dict, key: str) -> Optional[str]:
    """The value for one tag key, tolerating a scheme prefix and case.

    A customer types "feature"; the file may carry "Feature",
    "userScheme/feature" or "aws:feature". Matching only the exact string would
    lose the attribution the whole import exists to establish.
    """
    if not tags or not key:
        return None
    wanted = _key(key)
    for name, value in tags.items():
        candidate = name.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        if _key(candidate) == wanted or _key(name) == wanted:
            return value or None
    return None
