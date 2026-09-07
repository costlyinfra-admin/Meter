"""Build-cost ingest + allocation (coding tools).

Takes per-developer coding-tool spend (a Cursor-for-Teams seat export or any CSV)
and allocates each developer's spend across features by the PRs they authored
(the `actor` on feature_signal 'pr' rows, recorded during discovery). Writes
build_cost rows per feature and per developer, broken down by tool.

Confidence (design §7.3, build side):
  * all of a developer's PRs map to one feature -> high (direct match)
  * spread across several features            -> med  (inferred overlap split)
  * developer has no attributable PRs          -> low  (-> Unattributed bucket)

Build cost is ALWAYS separate from inference cost (invariant 2).
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Optional

from .db import app_dsn, connect, tenant_tx
from .github import GitHubClient
from .providers import month_start
from .seatpricing import seat_price

VALID_TOOLS = {"claude_code", "cursor", "copilot", "codex"}
_CONFIDENCE_RANK = {"high": 3, "med": 2, "low": 1}
# Upper bound on the optional CSV `months` column — matches the provider ingest
# history cap, and guards against a typo turning into thousands of rows.
MAX_MONTHS = 24


@dataclass
class DeveloperSpend:
    # `developer_id` is the attribution / grouping key (the GitHub handle when we
    # have one, else the display name / login). `name` and `handle` are the
    # separately-stored display identities; both are optional (legacy / automated
    # imports supply only the key). `months` is how many consecutive monthly
    # records this row represents, ending at the import period (default 1).
    developer_id: str
    tool: str
    amount: Decimal
    period: Optional[dt.date] = None
    name: Optional[str] = None
    handle: Optional[str] = None
    months: int = 1


class CsvImportError(Exception):
    """Raised when a coding-tool CSV cannot be parsed."""


def developer_label(name: Optional[str], handle: Optional[str], fallback: str = "") -> str:
    """Display label for a developer.

    "Name (handle)" when both are known; the name alone when the handle is
    missing; the handle alone when the name is missing; otherwise a fallback
    (the raw attribution key), so nothing renders blank.
    """
    name = (name or "").strip()
    handle = (handle or "").strip()
    if name and handle:
        return f"{name} ({handle})"
    return name or handle or fallback


def parse_csv(
    text: str, default_tool: Optional[str] = None, default_period: Optional[dt.date] = None
) -> list[DeveloperSpend]:
    """Parse a coding-tool export.

    Preferred format (one row per developer):
        developer,github_handle,tool,amount[,months]
    where `developer` is a display name and `github_handle` is the GitHub login
    used for PR attribution (matched case-insensitively). The optional `months`
    column backfills history: `...,50.00,12` writes twelve monthly $50 records
    ending at the import period. Legacy exports without a github_handle column
    (developer,amount[,tool]) are still accepted — there the developer column is
    the attribution key. Header names are matched case-insensitively and flexibly.
    """
    reader = csv.DictReader(io.StringIO(text.strip()))
    if reader.fieldnames is None:
        raise CsvImportError("CSV has no header row.")
    fields = {name.strip().lower(): name for name in reader.fieldnames}

    def pick(row, *names):
        for n in names:
            if n in fields and row[fields[n]] not in (None, ""):
                return row[fields[n]].strip()
        return None

    # A github_handle column signals the newer name+handle format; without it we
    # fall back to the legacy single-identity behaviour (unchanged).
    has_handle_col = any(h in fields for h in ("github_handle", "github", "handle"))

    spends: list[DeveloperSpend] = []
    for i, row in enumerate(reader, start=2):  # row 1 is the header
        if has_handle_col:
            name = pick(row, "developer", "name", "user")
            handle = pick(row, "github_handle", "github", "handle")
            if not name and not handle:
                raise CsvImportError(f"Row {i}: a developer name or github_handle is required.")
            # Attribute by handle when present; otherwise fall back to the name.
            developer_id = handle or name
        else:
            developer_id = pick(row, "developer", "developer_id", "email", "user", "login")
            name = handle = None
            if not developer_id:
                raise CsvImportError(f"Row {i}: missing developer.")

        amount_raw = pick(row, "amount", "cost", "spend")
        if amount_raw is None:
            raise CsvImportError(f"Row {i}: missing amount.")
        try:
            amount = Decimal(amount_raw.replace("$", "").replace(",", ""))
        except InvalidOperation as exc:
            raise CsvImportError(f"Row {i}: invalid amount '{amount_raw}'.") from exc
        if amount < 0:
            raise CsvImportError(f"Row {i}: amount cannot be negative, got '{amount_raw}'.")

        tool = (pick(row, "tool") or default_tool or "").strip()
        if tool not in VALID_TOOLS:
            raise CsvImportError(
                f"Row {i}: tool must be one of {sorted(VALID_TOOLS)}, got '{tool}'."
            )

        months_raw = pick(row, "months", "month_count", "num_months")
        if months_raw is None:
            months = 1
        else:
            try:
                months = int(months_raw)
            except ValueError as exc:
                raise CsvImportError(
                    f"Row {i}: months must be a whole number, got '{months_raw}'."
                ) from exc
            if months < 1 or months > MAX_MONTHS:
                raise CsvImportError(
                    f"Row {i}: months must be between 1 and {MAX_MONTHS}, got '{months_raw}'."
                )

        spends.append(
            DeveloperSpend(
                developer_id, tool, amount, default_period, name=name, handle=handle, months=months
            )
        )
    if not spends:
        raise CsvImportError("CSV contained no spend rows.")
    return spends


def _split_amount(total: Decimal, weights: dict[str, int]) -> dict[str, Decimal]:
    """Split ``total`` across keys proportional to integer weights, exactly."""
    weight_sum = sum(weights.values())
    ordered = sorted(weights.items(), key=lambda kv: (-kv[1], kv[0]))
    out: dict[str, Decimal] = {}
    allocated = Decimal("0")
    for key, weight in ordered:
        share = (total * weight / weight_sum).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        out[key] = share
        allocated += share
    # Put any rounding remainder on the largest share so the total is exact.
    out[ordered[0][0]] += total - allocated
    return out


def _developer_feature_distribution(conn) -> dict[str, dict[str, int]]:
    """actor(lowercased) -> {feature_id: number of authored PRs attributed to it}.

    Keyed by the lowercased GitHub login so attribution matches handles
    case-insensitively (GitHub logins are themselves case-insensitive).
    """
    rows = conn.execute(
        """
        SELECT fs.actor, fs.feature_id
        FROM feature_signal fs
        JOIN feature f ON f.id = fs.feature_id
        WHERE fs.signal_type = 'pr' AND fs.actor IS NOT NULL
        """
    ).fetchall()
    dist: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for actor, feature_id in rows:
        dist[actor.lower()][str(feature_id)] += 1
    return dist


def _attribution_key(spend: DeveloperSpend) -> str:
    """The lowercased GitHub handle used to match PRs (falls back to the key)."""
    return (spend.handle or spend.developer_id).lower()


def _month_span(start: dt.date, months: int) -> list[dt.date]:
    """`months` first-of-month dates ending at (and newest-first from) `start`:
    [start, start-1mo, …, start-(months-1)mo]."""
    out = []
    year, month = start.year, start.month
    for _ in range(months):
        out.append(dt.date(year, month, 1))
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    return out


def allocate_and_store(tenant_id: str, spends: list[DeveloperSpend], period: dt.date) -> dict:
    """Allocate developer spend to features and persist build_cost rows.

    Each row is written to every month in its span (its `months` value, ending at
    `period`), so a single CSV can backfill history. The same PR-authorship split
    is applied to each month.
    """
    anchor = month_start(period)
    # Exactly the (tool, period) cells this import writes — replaced idempotently,
    # grouped by tool so we never delete a month a different tool owns.
    tool_periods: dict[str, set] = defaultdict(set)
    for s in spends:
        tool_periods[s.tool].update(_month_span(anchor, s.months))

    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        dist = _developer_feature_distribution(conn)
        for tool, periods in tool_periods.items():
            conn.execute(
                "DELETE FROM build_cost WHERE tool = %s AND period = ANY(%s)",
                (tool, list(periods)),
            )
        for spend in spends:
            feature_counts = dist.get(_attribution_key(spend), {})
            for p in _month_span(anchor, spend.months):
                if not feature_counts:
                    # No attributable PRs -> Unattributed bucket (feature_id NULL).
                    _insert_build_cost(conn, tenant_id, None, spend, spend.amount, "low", p)
                    continue
                confidence = "high" if len(feature_counts) == 1 else "med"
                for feature_id, amount in _split_amount(spend.amount, feature_counts).items():
                    _insert_build_cost(conn, tenant_id, feature_id, spend, amount, confidence, p)

    summary = build_summary(tenant_id, period)
    # How many months of history this import covers (for the UI's confirmation).
    summary["months_imported"] = max((s.months for s in spends), default=1)
    return summary


#: Rows written by the PR-authorship allocator. Fine-tuning runs (source
#: 'fine_tune') are attributed directly by the user and must never be re-derived.
_ALLOCATED_SOURCE = "coding_tool+github"


def reattribute(tenant_id: str) -> dict:
    """Recompute feature attribution for allocator-written build cost.

    Discovery regenerates proposals by deleting `status='proposed'` features, and
    `build_cost.feature_id` is ON DELETE SET NULL — so spend that was attributed to
    a proposed feature silently drops into the Unattributed bucket and STAYS there,
    because build cost (unlike inference) is attributed only at import time.

    This re-runs the same PR-authorship allocation over the rows already stored:
    each (developer, tool, period) group's total is re-split across the features
    that developer's PRs now map to. Idempotent — running it twice changes nothing —
    and the per-group totals are preserved exactly, so the tenant's build total is
    never altered, only where it lands.
    """
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        dist = _developer_feature_distribution(conn)
        groups = conn.execute(
            """
            SELECT developer_id, developer_name, github_handle, tool, period, SUM(amount)
            FROM build_cost
            WHERE source = %s
            GROUP BY developer_id, developer_name, github_handle, tool, period
            """,
            (_ALLOCATED_SOURCE,),
        ).fetchall()
        if not groups:
            return {"rows": 0, "attributed": 0.0, "unattributed": 0.0}

        conn.execute("DELETE FROM build_cost WHERE source = %s", (_ALLOCATED_SOURCE,))
        rows = 0
        attributed = Decimal("0")
        unattributed = Decimal("0")
        for developer_id, name, handle, tool, period, total in groups:
            spend = DeveloperSpend(developer_id, tool, Decimal(total), name=name, handle=handle)
            feature_counts = dist.get(_attribution_key(spend), {})
            if not feature_counts:
                _insert_build_cost(conn, tenant_id, None, spend, spend.amount, "low", period)
                unattributed += spend.amount
                rows += 1
                continue
            confidence = "high" if len(feature_counts) == 1 else "med"
            for feature_id, amount in _split_amount(spend.amount, feature_counts).items():
                _insert_build_cost(conn, tenant_id, feature_id, spend, amount, confidence, period)
                attributed += amount
                rows += 1
    return {
        "rows": rows,
        "attributed": float(attributed),
        "unattributed": float(unattributed),
    }


def _insert_build_cost(conn, tenant_id, feature_id, spend, amount, confidence, period):
    conn.execute(
        """
        INSERT INTO build_cost
            (tenant_id, feature_id, developer_id, developer_name, github_handle, tool,
             amount, period, confidence, source)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'coding_tool+github')
        """,
        (
            tenant_id,
            feature_id,
            spend.developer_id,
            spend.name,
            spend.handle,
            spend.tool,
            amount,
            period,
            confidence,
        ),
    )


def _make_github_client(token):  # seam so tests can inject a fake
    return GitHubClient(token)


def import_copilot_seats(tenant_id: str, owner: str, token: str, period: dt.date) -> dict:
    """Pull GitHub Copilot seat assignments and allocate their cost to features.

    Each assigned seat is a fixed per-developer build cost (seat price by plan),
    which the existing PR-authorship allocator maps onto features — exactly like
    a CSV import, but pulled automatically (no upload). Idempotent per period.
    """
    start = month_start(period)
    with _make_github_client(token) as gh:
        plan = gh.copilot_plan_type(owner)
        seats = gh.fetch_copilot_seats(owner)
    price = seat_price("copilot", plan)
    spends = [DeveloperSpend(seat.login, "copilot", price) for seat in seats]

    if spends:
        summary = allocate_and_store(tenant_id, spends, period)
    else:
        # No seats -> clear any prior Copilot rows for the period (reflect reality).
        with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
            conn.execute("DELETE FROM build_cost WHERE period = %s AND tool = 'copilot'", (start,))
        summary = build_summary(tenant_id, period)

    summary["seats"] = len(seats)
    summary["plan"] = plan
    summary["seat_price"] = float(price)
    return summary


def record_training_cost(
    tenant_id: str,
    feature_id: str,
    amount,
    label: str,
    period: dt.date,
    run_ref: Optional[str] = None,
) -> dict:
    """Record a one-time fine-tuning / training run as BUILD cost on a feature.

    Fine-tuning an open-source model is part of what it cost to *build* the
    feature, so it lives on the build side (never blended with inference). It is
    directly attributed (the customer names the feature), so confidence is high.
    """
    start = month_start(period)
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        conn.execute(
            """
            INSERT INTO build_cost
                (tenant_id, feature_id, developer_id, tool, pr_ref, amount, period,
                 confidence, source)
            VALUES (%s, %s, %s, 'fine_tune', %s, %s, %s, 'high', 'fine_tune')
            """,
            (tenant_id, feature_id, label, run_ref, Decimal(str(amount)), start),
        )
    return build_summary(tenant_id, period)


def build_summary(tenant_id: str, period: dt.date) -> dict:
    """Build cost per feature and per developer, broken down by tool."""
    start = month_start(period)
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        rows = conn.execute(
            """
            SELECT bc.feature_id, f.name, bc.developer_id, bc.tool, bc.amount, bc.confidence
            FROM build_cost bc
            LEFT JOIN feature f ON f.id = bc.feature_id
            WHERE bc.period = %s
            """,
            (start,),
        ).fetchall()

    features: dict[str, dict] = {}
    developers: dict[str, dict] = {}
    unattributed = 0.0
    total = 0.0

    for feature_id, name, developer_id, tool, amount, confidence in rows:
        amt = float(amount)
        total += amt

        if feature_id is None:
            unattributed += amt
        else:
            fid = str(feature_id)
            feat = features.setdefault(
                fid,
                {
                    "feature_id": fid,
                    "name": name,
                    "amount": 0.0,
                    "by_tool": {},
                    "confidence": "low",
                },
            )
            feat["amount"] += amt
            feat["by_tool"][tool] = feat["by_tool"].get(tool, 0.0) + amt
            if _CONFIDENCE_RANK[confidence] > _CONFIDENCE_RANK[feat["confidence"]]:
                feat["confidence"] = confidence

        dev = developers.setdefault(
            developer_id, {"developer_id": developer_id, "amount": 0.0, "by_tool": {}}
        )
        dev["amount"] += amt
        dev["by_tool"][tool] = dev["by_tool"].get(tool, 0.0) + amt

    return {
        "period": start.isoformat(),
        "features": sorted(features.values(), key=lambda f: -f["amount"]),
        "developers": sorted(developers.values(), key=lambda d: -d["amount"]),
        "unattributed": unattributed,
        "total": total,
    }
