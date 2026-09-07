"""Inference cost ingest + attribution (connector path).

Pulls a month of authoritative provider spend (providers.py) and writes it to
`inference_cost` with source='cost_api'. Each record is attributed to a feature
via the tenant's key/project mappings (feature_signal rows), or — if nothing
matches — left in the **Unattributed bucket** (feature_id NULL). Either way the
full provider total is stored, so the sum always reconciles to the bill.

Confidence ladder (design §7.3):
  * dedicated per-feature API key  -> high
  * project/workspace mapping       -> med
  * unmapped (Unattributed)         -> low
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
from collections import defaultdict
from decimal import Decimal
from typing import Optional

from . import credentials, pricing, resources
from .db import admin_dsn, app_dsn, connect, tenant_tx
from .providers import (
    CostRecord,
    UsageRecord,
    aggregate,
    make_cost_client,
    month_start,
    next_month,
)


def _aggregate_month(records: list[CostRecord], month: dt.date) -> list[CostRecord]:
    """Collapse day-grain records into one row per key/model for the month."""
    return aggregate([dataclasses.replace(r, period=month) for r in records])


# 'ignore'-classified spend is excluded from normal reporting totals. NULL
# (legacy/unclassified) stays included. Two forms: bare and for the aliased table.
_ACTIVE_ENV = "(environment IS NULL OR environment <> 'ignore')"
_ACTIVE_ENV_IC = "(ic.environment IS NULL OR ic.environment <> 'ignore')"


def _make_cost_client(provider: str, admin_key: str):
    # Indirection so tests can inject a fake provider client.
    return make_cost_client(provider, admin_key)


def _load_mappings(conn) -> tuple[dict, dict]:
    """Return (by_api_key, by_service) maps of external_ref -> feature_id."""
    rows = conn.execute(
        """
        SELECT feature_id, signal_type, external_ref
        FROM feature_signal
        WHERE signal_type IN ('api_key', 'service') AND external_ref IS NOT NULL
        """
    ).fetchall()
    by_api_key: dict[str, str] = {}
    by_service: dict[str, str] = {}
    for feature_id, signal_type, external_ref in rows:
        target = by_api_key if signal_type == "api_key" else by_service
        target[external_ref] = str(feature_id)
    return by_api_key, by_service


def _attribute(record: CostRecord, maps: tuple[dict, dict]) -> tuple[Optional[str], str]:
    by_api_key, by_service = maps
    if record.api_key_ref and record.api_key_ref in by_api_key:
        return by_api_key[record.api_key_ref], "high"
    if record.project and record.project in by_service:
        return by_service[record.project], "med"
    if record.project and record.project in by_api_key:
        return by_api_key[record.project], "high"
    return None, "low"  # Unattributed bucket


def run_inference_ingest(tenant_id: str, provider: str, period: dt.date, admin_key: str) -> dict:
    """Fetch a provider's monthly cost and ingest it. Returns a summary.

    Anthropic uses the detailed path when the client exposes the Usage Report:
    authoritative Cost Report dollars are reconciled against per-workspace/per-key
    usage and labelled with an environment. Every other provider (and simpler
    clients, e.g. tests) uses the flat cost-report attribution path.
    """
    with _make_cost_client(provider, admin_key) as client:
        cost_records = client.fetch_costs(period)
        if provider == "anthropic" and hasattr(client, "fetch_usage"):
            usage = client.fetch_usage(period)
            workspaces = client.fetch_workspaces()
            api_keys = client.fetch_api_keys()
            return ingest_anthropic(tenant_id, period, cost_records, usage, workspaces, api_keys)
    return ingest_records(tenant_id, provider, period, cost_records)


def _months_back(day: dt.date, n: int) -> dt.date:
    """First-of-month ``n`` months before ``day`` (month-aligned)."""
    month = day.month - n
    year = day.year
    while month <= 0:
        month += 12
        year -= 1
    return dt.date(year, month, 1)


def run_inference_backfill(
    tenant_id: str,
    provider: str,
    admin_key: str,
    *,
    months: int = 12,
    anchor: Optional[dt.date] = None,
) -> dict:
    """Ingest the last ``months`` months (ending at ``anchor``, default this month).

    Reuses the per-month ingest so attribution, classification, and reconciliation
    are identical to a single-month sync — this just walks the window, oldest month
    first, and each month is idempotent (a re-sync replaces that month's rows).

    Resilient to a single bad month (a transient provider error is recorded and the
    walk continues), but if EVERY month fails the first error is raised so a real
    problem (e.g. a rejected admin key) still surfaces to the caller.
    """
    anchor_month = month_start(anchor or dt.date.today())
    periods = [_months_back(anchor_month, i) for i in range(months - 1, -1, -1)]
    by_month: list[dict] = []
    errors: list[dict] = []
    first_error: Optional[Exception] = None
    total = 0.0
    estimated = 0.0
    rows = 0
    for period in periods:
        try:
            summary = run_inference_ingest(tenant_id, provider, period, admin_key)
        except Exception as exc:  # noqa: BLE001 — record and continue; surfaced below if all fail
            first_error = first_error or exc
            errors.append({"period": period.isoformat(), "error": str(exc)[:200]})
            continue
        by_month.append(
            {"period": summary["period"], "total": summary["total"], "rows": summary.get("rows", 0)}
        )
        total += summary["total"]
        estimated += summary.get("estimated", 0.0)
        rows += summary.get("rows", 0)

    if not by_month and first_error is not None:
        raise first_error  # nothing landed — surface the real cause (bad key, etc.)

    return {
        "provider": provider,
        "months": months,
        "period": periods[-1].isoformat(),  # newest month (back-compat with single sync)
        "total": total,  # authoritative billed dollars
        "estimated": estimated,  # not-yet-billed estimate (current month only)
        "rows": rows,
        "by_month": by_month,
        "errors": errors,
    }


_DAILY_COLS = (
    "tenant_id, feature_id, provider, model, api_key_ref, amount, currency, day, "
    "tokens_in, tokens_out, request_count, cached_tokens_in, cache_write_tokens, "
    "cache_write_5m_tokens, cache_write_1h_tokens, workspace_id, "
    "workspace_name, api_key_id, api_key_name, environment, source, confidence"
)


def _insert_daily(
    conn,
    tenant_id,
    *,
    day,
    provider,
    amount,
    source,
    confidence,
    currency="USD",
    feature_id=None,
    model=None,
    api_key_ref=None,
    tokens_in=None,
    tokens_out=None,
    request_count=None,
    cached_tokens_in=None,
    cache_write_tokens=None,
    cache_write_5m=None,
    cache_write_1h=None,
    workspace_id=None,
    workspace_name=None,
    api_key_id=None,
    api_key_name=None,
    environment=None,
) -> None:
    """Persist one day-resolution inference-cost row (the additive daily table)."""
    conn.execute(
        f"INSERT INTO inference_cost_daily ({_DAILY_COLS}) VALUES (" + ", ".join(["%s"] * 22) + ")",
        (
            tenant_id,
            feature_id,
            provider,
            model,
            api_key_ref,
            amount,
            currency,
            day,
            tokens_in,
            tokens_out,
            request_count,
            cached_tokens_in,
            cache_write_tokens,
            cache_write_5m,
            cache_write_1h,
            workspace_id,
            workspace_name,
            api_key_id,
            api_key_name,
            environment,
            source,
            confidence,
        ),
    )


def _delete_daily(conn, provider: str, start: dt.date, source: str) -> None:
    """Idempotent per (provider, month, source): clear the month's daily rows."""
    conn.execute(
        "DELETE FROM inference_cost_daily "
        "WHERE provider = %s AND day >= %s AND day < %s AND source = %s",
        (provider, start, next_month(start), source),
    )


def sync_connected(tenant_id: str, period: Optional[dt.date] = None) -> dict:
    """Ingest the CURRENT month for every connected inference provider.

    Backs the Overview's refresh control: one call pulls fresh cost from each
    provider the tenant has connected, rather than only re-reading what is already
    stored. Deliberately single-month (fast); the 12-month backfill stays behind
    "Sync now" on Cost sources and the nightly cron.

    Resilient per provider — one bad credential never blocks the others; every
    failure is returned so the UI can show it instead of silently doing nothing.
    """
    period = period or dt.date.today()  # the ingest path needs a concrete month
    statuses = credentials.connector_statuses(tenant_id)
    providers = [c["type"] for c in statuses if c["category"] == "inference" and c["connected"]]
    synced: list[dict] = []
    errors: list[dict] = []
    total = 0.0
    for provider in providers:
        admin_key = credentials.get_secret(tenant_id, provider)
        if not admin_key:  # credential vanished between listing and use
            continue
        try:
            summary = run_inference_ingest(tenant_id, provider, period, admin_key)
        except Exception as exc:  # noqa: BLE001 — report per provider, keep going
            logging.getLogger("meter.ingest").warning(
                "refresh sync failed for %s: %s", provider, exc
            )
            errors.append({"provider": provider, "error": str(exc)[:200]})
            continue
        synced.append({"provider": provider, "total": summary["total"]})
        total += summary["total"]
    return {
        "providers": len(providers),
        "synced": synced,
        "errors": errors,
        "total": total,
    }


def ingest_records(
    tenant_id: str, provider: str, period: dt.date, records: list[CostRecord]
) -> dict:
    """Attribute and persist already-fetched cost records (idempotent per month).

    Records arrive at DAY resolution (each stamped with its bucket's day). We write
    them to the daily table as-is, and a month-aggregated rollup to inference_cost —
    so the monthly authority is unchanged (one row per key/model) while the daily
    detail is preserved.
    """
    start = month_start(period)
    attributed = Decimal("0")
    unattributed = Decimal("0")

    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        maps = _load_mappings(conn)
        # Idempotent: replace this provider+month's connector rows (monthly + daily).
        conn.execute(
            "DELETE FROM inference_cost "
            "WHERE provider = %s AND period = %s AND source = 'cost_api'",
            (provider, start),
        )
        _delete_daily(conn, provider, start, "cost_api")

        # Daily rows, at the resolution the API returned.
        for record in records:
            feature_id, confidence = _attribute(record, maps)
            _insert_daily(
                conn,
                tenant_id,
                day=record.period,
                provider=record.provider,
                model=record.model,
                api_key_ref=record.api_key_ref or record.project,
                amount=record.amount,
                currency=record.currency,
                tokens_in=record.tokens_in,
                tokens_out=record.tokens_out,
                request_count=record.request_count,
                cached_tokens_in=record.cached_tokens_in,
                cache_write_tokens=record.cache_write_tokens,
                cache_write_5m=record.cache_write_5m,
                cache_write_1h=record.cache_write_1h,
                source="cost_api",
                confidence=confidence,
            )

        # Monthly rollup (one row per key/model), unchanged authority.
        monthly = _aggregate_month(records, start)
        for record in monthly:
            feature_id, confidence = _attribute(record, maps)
            conn.execute(
                """
                INSERT INTO inference_cost
                    (tenant_id, feature_id, provider, model, api_key_ref, amount, currency,
                     period, tokens_in, tokens_out, request_count, cached_tokens_in,
                     cache_write_tokens, cache_write_5m_tokens, cache_write_1h_tokens,
                     source, confidence)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        'cost_api', %s)
                """,
                (
                    tenant_id,
                    feature_id,
                    record.provider,
                    record.model,
                    record.api_key_ref or record.project,
                    record.amount,
                    record.currency,
                    start,
                    record.tokens_in,
                    record.tokens_out,
                    record.request_count,
                    record.cached_tokens_in,
                    record.cache_write_tokens,
                    record.cache_write_5m,
                    record.cache_write_1h,
                    confidence,
                ),
            )
            if feature_id:
                attributed += record.amount
            else:
                unattributed += record.amount

    total = attributed + unattributed
    return {
        "provider": provider,
        "period": start.isoformat(),
        "rows": len(records),
        "total": float(total),
        "attributed": float(attributed),
        "unattributed": float(unattributed),
    }


# ---------------------------------------------------------------------------
# Anthropic detailed path: workspace / API-key identity + environment.
# ---------------------------------------------------------------------------
def _usage_weight(row: UsageRecord) -> Decimal:
    """Proportional weight for splitting a workspace's billed dollars.

    Priced dollars are the best proxy for real cost; fall back to total tokens,
    then request count, so a workspace's split (and each key's environment) is
    preserved even when a model is unpriced.
    """
    priced = pricing.price(row.model or "", row.tokens_in, row.tokens_out, "anthropic")
    if priced > 0:
        return priced
    tokens = Decimal(row.tokens_in + row.tokens_out)
    if tokens > 0:
        return tokens
    return Decimal(row.request_count or 0)


def _allocate(total: Decimal, weights: list[Decimal]) -> list[Decimal]:
    """Split ``total`` across ``weights`` so the parts sum EXACTLY to ``total``.

    The last row absorbs the rounding remainder, guaranteeing the reconciliation
    invariant (sum of allocated == authoritative billed) to the cent.
    """
    q = Decimal("0.0001")
    wsum = sum(weights, Decimal("0"))
    if not weights or wsum <= 0:
        return [Decimal("0") for _ in weights]
    out: list[Decimal] = []
    running = Decimal("0")
    for i, w in enumerate(weights):
        if i == len(weights) - 1:
            out.append((total - running).quantize(q))
        else:
            part = (total * w / wsum).quantize(q)
            out.append(part)
            running += part
    return out


def _attribute_detail(
    api_key_id: Optional[str], workspace_id: Optional[str], maps: tuple[dict, dict]
) -> tuple[Optional[str], str]:
    """Preserve existing feature attribution over the new explicit ids.

    A per-key mapping (api_key signal) wins; a per-workspace mapping (service
    signal) is next. No mapping -> Unattributed. (Feature/customer attribution of
    production traffic is a later milestone; this only keeps today's capability.)
    """
    by_api_key, by_service = maps
    if api_key_id and api_key_id in by_api_key:
        return by_api_key[api_key_id], "high"
    if workspace_id and workspace_id in by_service:
        return by_service[workspace_id], "med"
    if workspace_id and workspace_id in by_api_key:
        return by_api_key[workspace_id], "high"
    return None, "low"


def _insert_anthropic_row(
    conn,
    tenant_id: str,
    feature_id: Optional[str],
    row: UsageRecord,
    amount: Decimal,
    start: dt.date,
    workspace_id: Optional[str],
    workspace_name: Optional[str],
    api_key_name: Optional[str],
    environment: str,
    confidence: str,
    source: str = "cost_api",
) -> None:
    """Persist one reconciled Anthropic cost row with explicit identity columns.

    NOTE: ``api_key_ref`` now holds the genuine ``api_key_id`` (not the workspace,
    as the legacy fallback did); ``workspace_id`` has its own column. ``source`` is
    'cost_api' for authoritative billed rows and 'cost_api_est' for the estimated,
    not-yet-billed rows of the current month.
    """
    conn.execute(
        """
        INSERT INTO inference_cost
            (tenant_id, feature_id, provider, model, api_key_ref, amount, currency,
             period, tokens_in, tokens_out, request_count, cached_tokens_in,
             cache_write_tokens, cache_write_5m_tokens, cache_write_1h_tokens,
             workspace_id, workspace_name, api_key_id, api_key_name,
             environment, source, confidence)
        VALUES (%s, %s, 'anthropic', %s, %s, %s, 'USD', %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            tenant_id,
            feature_id,
            row.model,
            row.api_key_id,
            amount,
            start,
            row.tokens_in or None,
            row.tokens_out or None,
            row.request_count or None,
            row.cached_tokens_in or None,
            row.cache_write_tokens or None,
            row.cache_write_5m or None,
            row.cache_write_1h or None,
            workspace_id,
            workspace_name,
            row.api_key_id,
            api_key_name,
            environment,
            source,
            confidence,
        ),
    )


def _tokens(r) -> int:
    return (r.tokens_in or 0) + (r.tokens_out or 0)


def _estimate_unbilled(
    cost_records, usage_records, billed_total: Decimal, start: dt.date
) -> Decimal:
    """Estimate not-yet-billed inference spend for the CURRENT month.

    Anthropic's Cost Report lags usage by a day or two, so the most recent usage
    carries no billed dollars yet. We price that gap at Anthropic's OWN effective
    rate (billed dollars / billed tokens) — no external price list, and right even
    for models we don't list-price.

    Day-precise when the Usage Report carries per-day detail: price the tokens on
    the days AFTER the last billed day. Falls back to a whole-month token ratio when
    day info is absent. Returns 0 for a past (fully-billed) month, or when the Cost
    Report has no token detail to calibrate against.
    """
    if start != month_start(dt.date.today()) or billed_total <= 0:
        return Decimal("0")
    billed_tokens = sum(_tokens(r) for r in cost_records)
    if billed_tokens <= 0:
        return Decimal("0")
    rate = billed_total / Decimal(billed_tokens)  # Anthropic's own $/token

    # Precise path: tokens on days beyond the last billed day are not yet billed.
    billed_days = [r.period for r in cost_records if r.period]
    last_billed = max(billed_days) if billed_days else start
    trailing = sum(_tokens(u) for u in usage_records if u.day and u.day > last_billed)
    if trailing > 0:
        return (Decimal(trailing) * rate).quantize(Decimal("0.01"))

    # Fallback: no per-day usage detail -> scale by the whole-month token gap.
    usage_tokens = sum(_tokens(u) for u in usage_records)
    if usage_tokens <= billed_tokens:
        return Decimal("0")
    gap = Decimal(usage_tokens - billed_tokens) / Decimal(billed_tokens)
    return (billed_total * gap).quantize(Decimal("0.01"))


def _anthropic_daily_rows(cost_records, usage_records, class_map, maps, workspaces, api_keys):
    """Reconcile Anthropic billed dollars PER DAY into day-resolution rows.

    Mirrors the monthly reconciliation (allocate each workspace's billed dollars
    across its usage by priced weight), but grouped by day — so the daily rows sum,
    per month, to the same authoritative billed total. Returns dicts ready for
    ``_insert_daily``.
    """
    billed: dict = defaultdict(lambda: defaultdict(lambda: Decimal("0")))  # day -> ws -> $
    for r in cost_records:
        billed[r.period][r.project] += r.amount

    usage: dict = defaultdict(lambda: defaultdict(dict))  # day -> ws -> {(key,model,tier): rec}
    for u in usage_records:
        bucket = usage[u.day][u.workspace_id]
        k = (u.api_key_id, u.model, u.service_tier)
        agg = bucket.get(k)
        if agg is None:
            bucket[k] = dataclasses.replace(u)
        else:
            agg.tokens_in += u.tokens_in
            agg.tokens_out += u.tokens_out
            agg.cached_tokens_in += u.cached_tokens_in
            agg.cache_write_tokens += u.cache_write_tokens
            agg.cache_write_5m += u.cache_write_5m
            agg.cache_write_1h += u.cache_write_1h
            agg.request_count += u.request_count

    out: list[dict] = []
    for day, ws_billed in billed.items():
        for ws_id, authoritative in ws_billed.items():
            rows = list(usage.get(day, {}).get(ws_id, {}).values())
            allocations = _allocate(authoritative, [_usage_weight(r) for r in rows])
            if rows and sum(allocations, Decimal("0")) > 0:
                for row, amount in zip(rows, allocations):
                    env = class_map.get(
                        ("api_key", row.api_key_id), resources.DEFAULT_CLASSIFICATION
                    )
                    feature_id, confidence = _attribute_detail(row.api_key_id, ws_id, maps)
                    out.append(
                        {
                            "day": day,
                            "feature_id": feature_id,
                            "model": row.model,
                            "api_key_ref": row.api_key_id,
                            "amount": amount,
                            "tokens_in": row.tokens_in or None,
                            "tokens_out": row.tokens_out or None,
                            "request_count": row.request_count or None,
                            "cached_tokens_in": row.cached_tokens_in or None,
                            "cache_write_tokens": row.cache_write_tokens or None,
                            "cache_write_5m": row.cache_write_5m or None,
                            "cache_write_1h": row.cache_write_1h or None,
                            "workspace_id": ws_id,
                            "workspace_name": workspaces.get(ws_id),
                            "api_key_id": row.api_key_id,
                            "api_key_name": api_keys.get(row.api_key_id or "", {}).get("name"),
                            "environment": env,
                            "confidence": confidence,
                        }
                    )
            else:
                env = class_map.get(("workspace", ws_id), resources.DEFAULT_CLASSIFICATION)
                out.append(
                    {
                        "day": day,
                        "amount": authoritative,
                        "workspace_id": ws_id,
                        "workspace_name": workspaces.get(ws_id),
                        "environment": env,
                        "confidence": "low",
                    }
                )
    return out


def ingest_anthropic(
    tenant_id: str,
    period: dt.date,
    cost_records: list[CostRecord],
    usage_records: list[UsageRecord],
    workspaces: dict[str, str],
    api_keys: dict[str, dict],
) -> dict:
    """Reconcile authoritative Cost Report dollars against detailed usage.

    The Cost Report is the billing authority (dollars per workspace); the Usage
    Report supplies the per-(api_key, model) split within a workspace. For each
    workspace we allocate its billed dollars across its usage rows by priced
    weight — so the persisted total ALWAYS equals the bill. Each row's environment
    is the resolved snapshot of the API key's MANUAL classification (never inferred
    from names); newly seen keys default to ``unclassified``. A workspace billed
    with no usable usage is preserved as a single row rather than guessed.
    """
    start = month_start(period)

    # Register discovered resources (workspaces + API keys) so the user can classify
    # them, then load the saved classifications. Registration never overwrites a
    # manual choice; unseen resources default to 'unclassified'.
    discovered: list[dict] = []
    for ws_id in {r.project for r in cost_records if r.project} | {
        u.workspace_id for u in usage_records if u.workspace_id
    }:
        discovered.append(
            {
                "resource_type": "workspace",
                "resource_id": ws_id,
                "resource_name": workspaces.get(ws_id),
            }
        )
    for key_id in {u.api_key_id for u in usage_records if u.api_key_id}:
        meta = api_keys.get(key_id, {})
        discovered.append(
            {
                "resource_type": "api_key",
                "resource_id": key_id,
                "resource_name": meta.get("name"),
                "parent_resource_id": meta.get("workspace_id"),
            }
        )
    resources.register_resources(tenant_id, "anthropic", discovered)
    class_map = resources.get_classifications(tenant_id, "anthropic")

    # Authoritative billed dollars per workspace (cost_report groups by workspace_id).
    billed_by_ws: dict[Optional[str], Decimal] = {}
    for r in cost_records:
        billed_by_ws[r.project] = billed_by_ws.get(r.project, Decimal("0")) + r.amount
    billed_total = sum(billed_by_ws.values(), Decimal("0"))

    # Aggregate usage within each workspace by (api_key_id, model, service_tier).
    usage_by_ws: dict[Optional[str], dict[tuple, UsageRecord]] = defaultdict(dict)
    for u in usage_records:
        bucket = usage_by_ws[u.workspace_id]
        k = (u.api_key_id, u.model, u.service_tier)
        agg = bucket.get(k)
        if agg is None:
            bucket[k] = UsageRecord(
                workspace_id=u.workspace_id,
                api_key_id=u.api_key_id,
                model=u.model,
                service_tier=u.service_tier,
                tokens_in=u.tokens_in,
                tokens_out=u.tokens_out,
                cached_tokens_in=u.cached_tokens_in,
                cache_write_tokens=u.cache_write_tokens,
                cache_write_5m=u.cache_write_5m,
                cache_write_1h=u.cache_write_1h,
                request_count=u.request_count,
            )
        else:
            agg.tokens_in += u.tokens_in
            agg.tokens_out += u.tokens_out
            agg.cached_tokens_in += u.cached_tokens_in
            agg.cache_write_tokens += u.cache_write_tokens
            agg.cache_write_5m += u.cache_write_5m
            agg.cache_write_1h += u.cache_write_1h
            agg.request_count += u.request_count

    by_env: dict[str, Decimal] = {}
    attributed = Decimal("0")
    unattributed = Decimal("0")
    estimated = _estimate_unbilled(cost_records, usage_records, billed_total, start)
    rows_written = 0

    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        maps = _load_mappings(conn)
        conn.execute(
            "DELETE FROM inference_cost WHERE provider = 'anthropic' "
            "AND period = %s AND source IN ('cost_api', 'cost_api_est')",
            (start,),
        )
        for ws_id, authoritative in billed_by_ws.items():
            ws_name = workspaces.get(ws_id) if ws_id else None
            rows = list(usage_by_ws.get(ws_id, {}).values())
            allocations = _allocate(authoritative, [_usage_weight(r) for r in rows])
            if rows and sum(allocations, Decimal("0")) > 0:
                for row, amount in zip(rows, allocations):
                    meta = api_keys.get(row.api_key_id or "", {})
                    api_key_name = meta.get("name")
                    # Environment is the resolved snapshot of the KEY's manual
                    # classification (never inferred from its name).
                    environment = class_map.get(
                        ("api_key", row.api_key_id), resources.DEFAULT_CLASSIFICATION
                    )
                    feature_id, confidence = _attribute_detail(row.api_key_id, ws_id, maps)
                    _insert_anthropic_row(
                        conn,
                        tenant_id,
                        feature_id,
                        row,
                        amount,
                        start,
                        ws_id,
                        ws_name,
                        api_key_name,
                        environment,
                        confidence,
                    )
                    by_env[environment] = by_env.get(environment, Decimal("0")) + amount
                    if feature_id:
                        attributed += amount
                    else:
                        unattributed += amount
                    rows_written += 1
            else:
                # Billed dollars with no usable usage detail -> the workspace's own
                # classification if the user set one, else unclassified.
                environment = class_map.get(("workspace", ws_id), resources.DEFAULT_CLASSIFICATION)
                _insert_anthropic_row(
                    conn,
                    tenant_id,
                    None,
                    UsageRecord(workspace_id=ws_id),
                    authoritative,
                    start,
                    ws_id,
                    ws_name,
                    None,
                    environment,
                    "low",
                )
                by_env[environment] = by_env.get(environment, Decimal("0")) + authoritative
                unattributed += authoritative
                rows_written += 1

        # Daily-resolution billed rows (additive detail table): the same billed
        # dollars reconciled PER DAY, so daily trends and an exact MTD are possible.
        # The monthly rows above are untouched; summing these over a month equals the
        # monthly billed total.
        _delete_daily(conn, "anthropic", start, "cost_api")
        for daily in _anthropic_daily_rows(
            cost_records, usage_records, class_map, maps, workspaces, api_keys
        ):
            _insert_daily(conn, tenant_id, provider="anthropic", source="cost_api", **daily)

        # Estimated, not-yet-billed rows for the current month (source cost_api_est),
        # allocated across usage exactly like the billed dollars so they attribute to
        # features and carry their key's classification — but stay clearly separate
        # from the authoritative bill.
        if estimated > 0:
            all_usage = [r for bucket in usage_by_ws.values() for r in bucket.values()]
            for row, amount in zip(
                all_usage, _allocate(estimated, [_usage_weight(r) for r in all_usage])
            ):
                if amount <= 0:
                    continue
                environment = class_map.get(
                    ("api_key", row.api_key_id), resources.DEFAULT_CLASSIFICATION
                )
                feature_id, _conf = _attribute_detail(row.api_key_id, row.workspace_id, maps)
                _insert_anthropic_row(
                    conn,
                    tenant_id,
                    feature_id,
                    row,
                    amount,
                    start,
                    row.workspace_id,
                    workspaces.get(row.workspace_id or ""),
                    api_keys.get(row.api_key_id or "", {}).get("name"),
                    environment,
                    "low",
                    source="cost_api_est",
                )

    return {
        "provider": "anthropic",
        "period": start.isoformat(),
        "rows": rows_written,
        "total": float(billed_total),
        "estimated": float(estimated),
        "attributed": float(attributed),
        "unattributed": float(unattributed),
        "by_environment": {k: float(v) for k, v in by_env.items()},
    }


def anthropic_resource_detail(tenant_id: str, period: Optional[dt.date] = None) -> dict:
    """Anthropic's one detail table: each API key, its workspace, its current manual
    classification, and its cost — so every resource can be classified.

    ``period=None`` (the Configure panel's default) lists EVERY workspace/API key
    ever seen across the synced history, with its total cost — so a 12-month backfill
    is fully classifiable, not just the current month. Passing a ``period`` scopes it
    to that single month. Classification comes from the saved config
    (``resource_classification``) so a just-made choice shows immediately.
    """
    class_map = resources.get_classifications(tenant_id, "anthropic")
    all_time = period is None
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        # Group by resource id (not name) and take the latest name, so a resource
        # that spent across several months — or was renamed — appears exactly once.
        if all_time:
            rows = conn.execute(
                """
                SELECT workspace_id, MAX(workspace_name), api_key_id, MAX(api_key_name),
                       SUM(amount)
                FROM inference_cost
                WHERE provider = 'anthropic' AND source = 'cost_api'
                GROUP BY workspace_id, api_key_id
                ORDER BY SUM(amount) DESC
                """
            ).fetchall()
            start = None
        else:
            start = month_start(period)
            rows = conn.execute(
                """
                SELECT workspace_id, MAX(workspace_name), api_key_id, MAX(api_key_name),
                       SUM(amount)
                FROM inference_cost
                WHERE provider = 'anthropic' AND period = %s AND source = 'cost_api'
                GROUP BY workspace_id, api_key_id
                ORDER BY SUM(amount) DESC
                """,
                (start,),
            ).fetchall()

    detail_rows: list[dict] = []
    for ws_id, ws_name, key_id, key_name, amount in rows:
        # A key row is classifiable; a workspace-only fallback row (no key) is not.
        if key_id:
            classification = class_map.get(("api_key", key_id), resources.DEFAULT_CLASSIFICATION)
        else:
            classification = class_map.get(("workspace", ws_id), resources.DEFAULT_CLASSIFICATION)
        detail_rows.append(
            {
                "resource_type": "api_key" if key_id else "workspace",
                "resource_id": key_id or ws_id,
                "name": key_name or ("(no key detail)" if key_id else None),
                "group": ws_name or ws_id or "—",
                "classification": classification,
                "cost": float(amount),
            }
        )

    return {
        "provider": "anthropic",
        "period": start.isoformat() if start else None,
        "all_time": all_time,  # cost column is total across history when True
        "classifiable": True,
        "columns": {"group": "Workspace", "name": "API key"},
        "rows": detail_rows,
    }


def inference_summary(tenant_id: str, period: dt.date) -> dict:
    """Per-feature inference totals + the Unattributed bucket, for a month."""
    start = month_start(period)
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        rows = conn.execute(
            f"""
            SELECT ic.feature_id, f.name, ic.confidence, SUM(ic.amount)
            FROM inference_cost ic
            LEFT JOIN feature f ON f.id = ic.feature_id
            WHERE ic.period = %s AND ic.source = 'cost_api' AND {_ACTIVE_ENV_IC}
            GROUP BY ic.feature_id, f.name, ic.confidence
            ORDER BY SUM(ic.amount) DESC
            """,  # noqa: S608
            (start,),
        ).fetchall()
        provider_rows = conn.execute(
            f"""
            SELECT provider, SUM(amount) FROM inference_cost
            WHERE period = %s AND source = 'cost_api' AND {_ACTIVE_ENV}
            GROUP BY provider
            """,  # noqa: S608
            (start,),
        ).fetchall()

    features = []
    unattributed = 0.0
    for feature_id, name, confidence, amount in rows:
        if feature_id is None:
            unattributed += float(amount)
        else:
            features.append(
                {
                    "feature_id": str(feature_id),
                    "name": name,
                    "amount": float(amount),
                    "confidence": confidence,
                }
            )
    by_provider = {p: float(a) for p, a in provider_rows}
    return {
        "period": start.isoformat(),
        "features": features,
        "unattributed": unattributed,
        "by_provider": by_provider,
        "total": float(sum(by_provider.values())),
    }


def run_scheduled_ingest(periods: Optional[list[dt.date]] = None) -> list[dict]:
    """Ingest every tenant's connected inference providers. Cron entry point."""
    periods = periods or [month_start(dt.date.today())]
    with connect(admin_dsn()) as conn:
        pairs = conn.execute(
            """
            SELECT DISTINCT tenant_id, connector_type FROM connector_credential
            WHERE connector_type IN ('anthropic', 'openai')
            """
        ).fetchall()

    from . import hook  # local import to avoid a module-load cycle

    results: list[dict] = []
    reconciled: set[tuple] = set()
    for tenant_id, provider in pairs:
        admin_key = credentials.get_secret(str(tenant_id), provider)
        if not admin_key:
            continue
        for period in periods:
            try:
                results.append(run_inference_ingest(str(tenant_id), provider, period, admin_key))
                # Keep hook numbers tied to the bill each cycle (no-op if no hook data).
                if (str(tenant_id), period) not in reconciled:
                    hook.reconcile(str(tenant_id), period)
                    reconciled.add((str(tenant_id), period))
            except Exception as exc:  # one tenant/provider failing must not stop the rest
                logging.getLogger("meter.ingest").warning(
                    "inference ingest failed for tenant=%s provider=%s: %s",
                    tenant_id,
                    provider,
                    exc,
                )
                results.append(
                    {"tenant_id": str(tenant_id), "provider": provider, "error": str(exc)}
                )
    return results


if __name__ == "__main__":
    summary = run_scheduled_ingest()
    print(f"Ingested {len(summary)} tenant/provider runs.")
    for item in summary:
        print(" ", item)
