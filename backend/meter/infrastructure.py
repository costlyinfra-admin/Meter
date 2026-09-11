"""Infrastructure cost: the cloud bill, ingested as first-class line items.

What this adds. Meter already reads model-provider bills (inference) and coding
tool spend (build). Neither covers the cloud a product actually runs on. This
module reads a cloud provider's cost API in full, classifies every line item
into exactly one category, attributes it to a feature where the bill itself says
so, and stores it in `infra_cost`.

Three things it is careful about:

**Nothing is dropped.** No service allowlist, no filter on the query, no silent
skip for a service we have never heard of. An unrecognised AWS service is
infrastructure (see `infra_classify`), and its raw name and dimensions are kept
so it can be reclassified later without re-fetching the bill.

**Nothing is counted twice.** Amazon Bedrock spend already arrives through the
existing "Amazon Bedrock (AWS cost)" inference connector, which remains its
authoritative path. Bedrock line items are still recorded here — a copy of the
bill with a hole in it is not a copy of the bill, and Phase 2 needs them to
reconcile the two paths — but with `counted = false`, and every total taken from
this table filters on `counted`. The Bedrock connector is not modified.

**Nothing is invented.** A feature is assigned only from an activated cost
allocation tag whose value matches a signal the customer configured, or from an
explicit service->feature rule they configured. A service name on its own never
implies a feature. Anything else is Unattributed, which is a real answer.

Phase 1 ends at the connector: this data is stored and shown on its own Cost
sources tab, and is deliberately not yet folded into Overview, features, or any
total. Phase 2 does that.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from decimal import Decimal
from typing import Optional

from . import credentials, infra_csv
from .db import admin_dsn, app_dsn, connect, tenant_tx
from .infra_classify import LineItem, classify
from .infra_providers import (
    CloudflareCostClient,
    DigitalOceanCostClient,
    MongoAtlasCostClient,
    SnowflakeCostClient,
    VercelCloudCostClient,
)
from .providers import (
    AwsCostExplorerClient,
    AzureCloudCostClient,
    GcpBillingCostClient,
    ProviderError,
    month_start,
    next_month,
)

logger = logging.getLogger("meter.infrastructure")

#: The infrastructure providers this tab offers, in display order. Adding one is
#: an entry here plus a client — the tab, the API and the UI all read this list
#: rather than hard-coding AWS.
#: `ingest` says how a provider's numbers arrive: "api" reads the vendor, "csv"
#: takes a file the customer downloaded. It is on every entry rather than
#: defaulted, so adding a provider forces the question to be answered.
PROVIDERS: tuple[dict, ...] = (
    {
        "type": "aws",
        "ingest": "api",
        "name": "Amazon Web Services",
        "short": "AWS",
        "status": "available",
        "note": "Reads AWS Cost Explorer — the whole bill, read-only.",
    },
    # NOTE the id. "azure" is already taken by the Azure OpenAI *inference*
    # connector, and a tenant who connected that must not see this card light up
    # as connected — they are different credentials reading different scopes of
    # the same bill.
    {
        "type": "azure_cloud",
        "ingest": "api",
        "name": "Microsoft Azure",
        "short": "Azure",
        "status": "available",
        "note": "Reads Azure Cost Management — the whole subscription, read-only.",
    },
    {
        "type": "gcp",
        "ingest": "api",
        "name": "Google Cloud Platform",
        "short": "GCP",
        "status": "available",
        # GCP has no cost API; the billing export is the only path to real spend,
        # and it has no history before the customer switched it on.
        "note": "Reads the BigQuery billing export — the whole bill, read-only.",
    },
    # Managed platforms. Narrower bills than a hyperscaler, but the same
    # contract: the vendor must report DOLLARS. Platforms that publish only
    # usage are deliberately absent — see infra_providers.py.
    {
        "type": "digitalocean",
        "ingest": "api",
        "name": "DigitalOcean",
        "short": "DigitalOcean",
        "status": "available",
        "note": "Reads your DigitalOcean invoices — real line items, read-only.",
    },
    {
        "type": "mongodb_atlas",
        "ingest": "api",
        "name": "MongoDB Atlas",
        "short": "Atlas",
        "status": "available",
        "note": "Reads your Atlas organisation invoices — per-cluster line items, read-only.",
    },
    {
        "type": "cloudflare",
        "ingest": "api",
        "name": "Cloudflare",
        "short": "Cloudflare",
        "status": "available",
        # Cloudflare reports what you subscribe to, not per-resource usage, and a
        # subscription only describes its current period.
        "note": "Reads your Cloudflare subscriptions — current period, read-only.",
    },
    {
        "type": "snowflake",
        "ingest": "api",
        "name": "Snowflake",
        "short": "Snowflake",
        "status": "available",
        "note": "Reads ORGANIZATION_USAGE spend in currency — read-only SQL.",
    },
    # "vercel_cloud", not "vercel": the latter is the Vercel AI Gateway connector
    # on the Inference tab, a different scope of the same invoice.
    {
        "type": "vercel_cloud",
        "ingest": "api",
        "name": "Vercel",
        "short": "Vercel",
        "status": "available",
        "note": "Reads Vercel billing charges (FOCUS) — daily, read-only.",
    },
    # Import-only. These three publish an authoritative invoice you can download
    # but nothing you can fetch: Redis Cloud has a cost report, Supabase shows
    # invoices in its dashboard, and Neon publishes consumption that would have
    # to be priced into a modelled figure. Reading the real numbers from a file
    # beats inventing them from a price list — see infra_csv.py.
    {
        "type": "redis_cloud",
        "name": "Redis Cloud",
        "short": "Redis",
        "status": "available",
        "ingest": "csv",
        "note": "Import the cost report you download from Redis Cloud.",
    },
    {
        "type": "supabase",
        "name": "Supabase",
        "short": "Supabase",
        "status": "available",
        "ingest": "csv",
        "note": "Import the invoice you download from the Supabase dashboard.",
    },
    {
        "type": "neon",
        "name": "Neon",
        "short": "Neon",
        "status": "available",
        "ingest": "csv",
        "note": "Import the invoice you download from the Neon console.",
    },
)
_BY_TYPE = {p["type"]: p for p in PROVIDERS}

#: Connector types that ingest through this module. Kept separate from PROVIDERS
#: so a provider can be listed before it is syncable.
#: Providers this module can sync from an API. CSV-import providers are live but
#: have nothing to poll, so a scheduled run must not try.
LIVE_PROVIDERS = (
    "aws",
    "azure_cloud",
    "gcp",
    "digitalocean",
    "mongodb_atlas",
    "cloudflare",
    "snowflake",
    "vercel_cloud",
)

#: Providers whose numbers arrive as an uploaded file.
CSV_PROVIDERS = ("redis_cloud", "supabase", "neon")

#: How far back a manual "Sync now" reaches. Cloud bills are restated for days
#: after the fact, so a sync always re-reads recent history rather than trusting
#: what it imported the first time.
DEFAULT_BACKFILL_MONTHS = 12


class InfraError(Exception):
    """A problem the UI should show verbatim. Never carries a credential."""


def provider(provider_type: str) -> dict:
    if provider_type not in _BY_TYPE:
        raise InfraError(f"Unknown infrastructure provider: {provider_type}")
    return _BY_TYPE[provider_type]


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------
def make_client(provider_type: str, secret: str):
    """The cost client for a provider. Raises for one that isn't live yet."""
    if provider_type == "aws":
        return AwsCostExplorerClient(secret)
    if provider_type == "azure_cloud":
        return AzureCloudCostClient(secret)
    if provider_type == "gcp":
        return GcpBillingCostClient(secret)
    if provider_type == "digitalocean":
        return DigitalOceanCostClient(secret)
    if provider_type == "mongodb_atlas":
        return MongoAtlasCostClient(secret)
    if provider_type == "cloudflare":
        return CloudflareCostClient(secret)
    if provider_type == "snowflake":
        return SnowflakeCostClient(secret)
    if provider_type == "vercel_cloud":
        return VercelCloudCostClient(secret)
    if provider_type in _BY_TYPE:
        raise InfraError(f"{_BY_TYPE[provider_type]['name']} ingestion is not available yet.")
    raise InfraError(f"Unknown infrastructure provider: {provider_type}")


def _make_client(provider_type: str, secret: str):
    # Indirection so tests can inject a fake client.
    return make_client(provider_type, secret)


#: Per-provider defaults for the settings a customer may omit. Only non-secret
#: settings appear here; keys, secrets and private keys never leave the backend.
_CONFIG_DEFAULTS = {
    "aws": {
        "metric": "UnblendedCost",
        "granularity": "DAILY",
        "group_by": ["SERVICE", "TAG"],
        "scope_key": "region",
        "scope_default": "us-east-1",
    },
    "azure_cloud": {
        "metric": "ActualCost",
        "granularity": "Daily",
        "group_by": ["ServiceName", "TAG"],
        "scope_key": "subscription_id",
        "scope_default": "",
    },
    "digitalocean": {
        "metric": "invoice",
        "granularity": "MONTHLY",
        "group_by": ["product", "project"],
        # DigitalOcean's credential is a bare token with no account identifier,
        # and the token is the LAST thing that may appear in a config panel.
        "scope_key": "team",
        "scope_default": "",
    },
    "mongodb_atlas": {
        "metric": "invoice",
        "granularity": "MONTHLY",
        "group_by": ["sku", "project"],
        "scope_key": "org_id",
        "scope_default": "",
    },
    "cloudflare": {
        "metric": "subscription",
        "granularity": "MONTHLY",
        "group_by": ["product", "zone"],
        "scope_key": "account_id",
        "scope_default": "",
    },
    "snowflake": {
        "metric": "usage_in_currency",
        "granularity": "DAILY",
        "group_by": ["service_type", "account"],
        "scope_key": "account",
        "scope_default": "",
    },
    "vercel_cloud": {
        # FOCUS names the cost column, so the metric is a real choice here.
        "metric": "BilledCost",
        "granularity": "DAILY",
        "group_by": ["ServiceName", "TAG"],
        "scope_key": "team_id",
        "scope_default": "",
    },
    "gcp": {
        # The export reports one figure — the billed cost — so there is no
        # metric to choose, and saying "UnblendedCost" here would be a lie
        # borrowed from another provider's vocabulary.
        "metric": "cost",
        "granularity": "DAILY",
        "group_by": ["service", "sku", "TAG"],
        "scope_key": "dataset",
        "scope_default": "",
    },
}


def describe_config(secret: str, provider_type: str = "aws") -> dict:
    """The non-secret half of a stored credential, for the configuration panel.

    Access keys, client secrets and service-account private keys are never
    returned by this — or by anything else. What a customer needs to see is
    which tag drives attribution, and which metric/granularity/scope their
    numbers came from, in that provider's own vocabulary.
    """
    try:
        raw = json.loads(secret)
    except (ValueError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    d = _CONFIG_DEFAULTS.get(provider_type, _CONFIG_DEFAULTS["aws"])
    scope = raw.get(d["scope_key"]) or d["scope_default"]
    return {
        "tag": raw.get("tag") or "feature",
        "metric": raw.get("metric") or d["metric"],
        # Each cloud spells this its own way ("DAILY" on AWS, "Daily" on Azure);
        # echoing the customer's own setting beats imposing one house style.
        "granularity": str(raw.get("granularity") or d["granularity"]),
        # What the numbers are scoped to: an AWS region, an Azure subscription,
        # a GCP export dataset. One field, because the panel shows one line.
        "scope": str(scope),
        "scope_label": d["scope_key"].replace("_", " "),
        "group_by": raw.get("group_by") or d["group_by"],
    }


# ---------------------------------------------------------------------------
# Feature attribution
# ---------------------------------------------------------------------------
def _load_mappings(conn) -> tuple[dict, dict]:
    """(by_tag, by_service): external_ref -> feature_id, from configured signals.

    `usage_tag`/`api_key` signals are tag values the customer mapped to a
    feature — the direct path, and the same signals the Bedrock connector reads,
    so a tenant who already mapped their tags gets attribution here for free.
    `service` signals are an explicit service->feature rule: an allocation the
    customer configured, not one we inferred from a service name.
    """
    rows = conn.execute(
        """
        SELECT feature_id, signal_type, external_ref
        FROM feature_signal
        WHERE signal_type IN ('usage_tag', 'api_key', 'service') AND external_ref IS NOT NULL
        """
    ).fetchall()
    by_tag: dict[str, str] = {}
    by_service: dict[str, str] = {}
    for feature_id, signal_type, external_ref in rows:
        target = by_service if signal_type == "service" else by_tag
        target[external_ref] = str(feature_id)
    return by_tag, by_service


def attribute(item, maps: tuple[dict, dict]) -> tuple[Optional[str], str, str]:
    """(feature_id, allocation_method, confidence) for one line item.

    Direct beats allocated: a tag on the resource itself is the customer saying
    what this spend is for, which outranks a rule about its service. Neither
    matching is Unattributed — never a guess from the service name.
    """
    by_tag, by_service = maps
    tag = (item.tag_value or "").strip()
    if tag and tag in by_tag:
        return by_tag[tag], "direct", "high"
    service = (item.service or "").strip()
    if service and service in by_service:
        return by_service[service], "allocated", "med"
    return None, "unattributed", "low"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def item_key(item, *, metric: str, granularity: str) -> str:
    """A stable identity for a line item within its (tenant, provider, period).

    Built from the raw billing dimensions plus what was measured, so it does NOT
    move when a classification rule changes — re-running a sync after editing
    the rule table updates the existing rows instead of duplicating them.
    """
    payload = json.dumps(
        {
            "d": {k: v for k, v in sorted((item.dimensions or {}).items())},
            "m": metric,
            "g": granularity,
            "c": item.currency,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:40]


def _fold(items: list, *, metric: str, granularity: str) -> dict[tuple, dict]:
    """Collapse items sharing (period, item_key) into one row, summing amounts.

    One grouped Cost Explorer query should not return the same key twice, but a
    tolerant parser and a paginated API make that an assumption rather than a
    guarantee — and the unique index would reject the second row. Summing keeps
    the total right either way.
    """
    folded: dict[tuple, dict] = {}
    for item in items:
        key = (item.period, item_key(item, metric=metric, granularity=granularity))
        row = folded.get(key)
        if row is None:
            folded[key] = {"item": item, "amount": Decimal(item.amount)}
        else:
            row["amount"] += Decimal(item.amount)
    return folded


def persist(
    tenant_id: str,
    provider_type: str,
    items: list,
    *,
    start: dt.date,
    end: dt.date,
    metric: str,
    granularity: str,
    source: str = "cost_api",
) -> dict:
    """Classify, attribute and store a window of line items. Idempotent.

    The window is replaced wholesale rather than merged: AWS restates recent
    days, and a line item that disappeared from the bill must disappear from us
    too. Re-running the same sync therefore converges on the same rows.
    """
    folded = _fold(items, metric=metric, granularity=granularity)
    totals = {"infrastructure": Decimal("0"), "excluded": Decimal("0")}
    by_category: dict[str, Decimal] = {}
    attributed = Decimal("0")
    unattributed = Decimal("0")

    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        maps = _load_mappings(conn)
        conn.execute(
            # Scoped to this source: re-importing a file must never delete rows
            # an API sync wrote, and vice versa.
            "DELETE FROM infra_cost "
            "WHERE provider = %s AND source = %s AND period >= %s AND period < %s",
            (provider_type, source, start, end),
        )
        for (period, key), row in folded.items():
            item, amount = row["item"], row["amount"]
            verdict = classify(
                LineItem(
                    service=item.service or "",
                    usage_type=item.usage_type,
                    operation=item.operation,
                    region=item.region,
                    account_id=item.account_id,
                    tag_value=item.tag_value,
                ),
                provider_type,
            )
            # A category another connector already owns is recorded, not counted.
            counted = verdict.dedupe_owner is None
            feature_id, method, confidence = attribute(item, maps)
            conn.execute(
                """
                INSERT INTO infra_cost
                    (tenant_id, feature_id, provider, period, month, granularity, metric,
                     service, usage_type, operation, account_id, region, tag_key, tag_value,
                     dimensions, amount, currency, category, category_rule, counted,
                     dedupe_owner, allocation_method, confidence, item_key, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    tenant_id,
                    feature_id,
                    provider_type,
                    period,
                    month_start(period),
                    granularity,
                    metric,
                    item.service or "",
                    item.usage_type,
                    item.operation,
                    item.account_id,
                    item.region,
                    item.tag_key,
                    item.tag_value,
                    json.dumps(item.dimensions or {}),
                    amount,
                    item.currency,
                    verdict.category,
                    verdict.rule,
                    counted,
                    verdict.dedupe_owner,
                    method,
                    confidence,
                    key,
                    source,
                ),
            )
            by_category[verdict.category] = by_category.get(verdict.category, Decimal("0")) + amount
            if not counted:
                totals["excluded"] += amount
            elif verdict.category == "infrastructure":
                totals["infrastructure"] += amount
            # Attribution is reported for infrastructure only, which is what
            # this connector's tab shows and what `summary()` returns. Counting
            # every category here would make the same key mean two different
            # things in two places.
            if counted and verdict.category == "infrastructure":
                if feature_id:
                    attributed += amount
                else:
                    unattributed += amount

    return {
        "provider": provider_type,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "items": len(folded),
        "infrastructure": float(totals["infrastructure"]),
        "excluded": float(totals["excluded"]),
        "by_category": {k: float(v) for k, v in sorted(by_category.items())},
        "attributed": float(attributed),
        "unattributed": float(unattributed),
    }


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
def _record_run(
    tenant_id: str,
    provider_type: str,
    *,
    start: dt.date,
    end: dt.date,
    trigger: str,
    status: str,
    items: int = 0,
    amount: float = 0.0,
    error: Optional[str] = None,
) -> None:
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        conn.execute(
            """
            INSERT INTO infra_sync_run
                (tenant_id, provider, covered_from, covered_to, items, amount,
                 trigger, status, error_message, finished_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            """,
            (tenant_id, provider_type, start, end, items, amount, trigger, status, error),
        )


def sync_window(
    tenant_id: str,
    provider_type: str,
    secret: str | list[str],
    *,
    start: dt.date,
    end: dt.date,
    trigger: str = "manual",
) -> dict:
    """Fetch, classify and store ``[start, end)``, recording the run either way.

    ``secret`` may be one credential or several: a tenant can hold more than one
    account with the same cloud — two AWS payer accounts, say — and their bill
    is the sum.

    EVERY ACCOUNT IS FETCHED BEFORE ANYTHING IS WRITTEN. `persist` is idempotent
    by clearing the provider's window and rewriting it, so persisting per account
    would have the second account's write delete the first account's rows: the
    customer would see one account's cloud bill and nothing would look wrong.
    A partial failure therefore writes nothing and the run is recorded as failed,
    because rewriting the window from the accounts that answered would silently
    drop the one that did not.

    A failure is stored as a failed run with its message, so the connector card
    can say what went wrong instead of showing a state nobody wrote down. The
    message comes from ProviderError/InfraError, which are written to never
    contain a credential.
    """
    secrets = [secret] if isinstance(secret, str) else list(secret)
    try:
        items: list = []
        metric = None
        granularity = None
        for one in secrets:
            with _make_client(provider_type, one) as client:
                items.extend(client.fetch_items(start, end))
                # Same provider, so every client reports the same metric and
                # granularity; the first to answer names them.
                metric = metric or client.metric
                granularity = granularity or client.granularity
        summary = persist(
            tenant_id,
            provider_type,
            items,
            start=start,
            end=end,
            metric=metric,
            granularity=granularity,
        )
    except Exception as exc:
        message = str(exc)[:500] if isinstance(exc, (ProviderError, InfraError)) else None
        _record_run(
            tenant_id,
            provider_type,
            start=start,
            end=end,
            trigger=trigger,
            status="error",
            # Anything unexpected is logged in full and reported generically:
            # an arbitrary exception's text is not vetted for secrets.
            error=message or "Sync failed. See the logs for details.",
        )
        logger.warning("infra sync failed tenant=%s provider=%s: %s", tenant_id, provider_type, exc)
        raise
    _record_run(
        tenant_id,
        provider_type,
        start=start,
        end=end,
        trigger=trigger,
        status="success",
        items=summary["items"],
        amount=summary["infrastructure"],
    )
    return summary


def import_csv(
    tenant_id: str,
    provider_type: str,
    text: str,
    *,
    tag: str = "feature",
    mapping_override: Optional[dict] = None,
    dry_run: bool = False,
) -> dict:
    """Read a downloaded bill and, unless ``dry_run``, store it.

    Two calls on purpose. The first previews: it reports which columns were
    matched, what the file totals, which rows were skipped and why — and writes
    nothing. Only a second call commits. An import that surprises someone is an
    import that should have been previewed, and these files are hand-downloaded,
    so there is no schedule to hurry.

    The window is the file's own date range, and only CSV-sourced rows inside it
    are replaced. Re-uploading a corrected file therefore converges rather than
    doubling, and never touches rows an API connector wrote.
    """
    meta = provider(provider_type)
    if meta.get("ingest") != "csv":
        raise InfraError(f"{meta['name']} syncs from its API — there is nothing to import.")
    try:
        report = infra_csv.parse(
            text, tag_key=tag, mapping_override=mapping_override, default_service=meta["name"]
        )
    except infra_csv.CsvImportError as exc:
        raise InfraError(str(exc)) from exc

    summary = {**report.as_dict(), "provider": provider_type, "dry_run": dry_run}
    if dry_run:
        return summary

    start, end = report.first_day, report.last_day + dt.timedelta(days=1)
    stored = persist(
        tenant_id,
        provider_type,
        report.items,
        start=start,
        end=end,
        metric="invoice",
        granularity="DAILY",
        source="csv",
    )
    _record_run(
        tenant_id,
        provider_type,
        start=start,
        end=end,
        trigger="manual",
        status="success",
        items=stored["items"],
        amount=stored["infrastructure"],
    )
    # Merged deliberately, not with {**a, **b}: `persist` reports the half-open
    # window it replaced and the report reports the file's inclusive date range.
    # Both are called "to", and letting one silently win would make the number
    # shown to the customer mean whichever happened to be second.
    return {
        **summary,
        **{k: v for k, v in stored.items() if k not in ("from", "to", "provider")},
        "dry_run": False,
    }


def backfill_start(months: int, *, today: Optional[dt.date] = None) -> dt.date:
    """First-of-month ``months - 1`` months before today (month-aligned)."""
    today = today or dt.date.today()
    month = today.month - (max(months, 1) - 1)
    year = today.year + (month - 1) // 12
    return dt.date(year, (month - 1) % 12 + 1, 1)


def run_infra_sync(
    tenant_id: str,
    provider_type: str,
    secret: str | list[str],
    *,
    months: int = DEFAULT_BACKFILL_MONTHS,
    today: Optional[dt.date] = None,
    trigger: str = "manual",
) -> dict:
    """Sync the last ``months`` months of a provider's bill, up to today.

    One call over the whole window rather than one per month: Cost Explorer bills
    per request and returns a date-bucketed answer anyway, so a year of daily
    line items is one query (plus its pages), not twelve.
    """
    today = today or dt.date.today()
    start = backfill_start(months, today=today)
    # Exclusive end, and never into the future: Cost Explorer rejects that.
    end = min(next_month(month_start(today)), today + dt.timedelta(days=1))
    return sync_window(tenant_id, provider_type, secret, start=start, end=end, trigger=trigger)


def sync_connected(
    tenant_id: str, *, months: int = 1, trigger: str = "scheduled", today: Optional[dt.date] = None
) -> dict:
    """Sync every live infrastructure provider this tenant has connected.

    Resilient per provider: one bad credential never stops the others, and every
    failure is returned so the caller can show it.
    """
    synced: list[dict] = []
    errors: list[dict] = []
    for provider_type in LIVE_PROVIDERS:
        secrets = [sec for _, sec in credentials.get_secrets(tenant_id, provider_type)]
        if not secrets:
            continue
        try:
            summary = run_infra_sync(
                tenant_id, provider_type, secrets, months=months, today=today, trigger=trigger
            )
        except Exception as exc:  # noqa: BLE001 — report per provider, keep going
            errors.append({"provider": provider_type, "error": str(exc)[:200]})
            continue
        synced.append(summary)
    return {"synced": synced, "errors": errors}


def run_scheduled_infra_sync(months: int = 1) -> list[dict]:
    """Nightly refresh across every tenant. Cron entry point.

    Short window on purpose: the nightly job keeps recent days current (cloud
    bills are restated for days after the fact), while the full 12-month
    backfill stays behind "Sync now" on Cost sources.
    """
    with connect(admin_dsn()) as conn:
        rows = conn.execute(
            "SELECT DISTINCT tenant_id, connector_type FROM connector_credential "
            "WHERE connector_type = ANY(%s)",
            (list(LIVE_PROVIDERS),),
        ).fetchall()
    results: list[dict] = []
    for tenant_id, provider_type in rows:
        secrets = [sec for _, sec in credentials.get_secrets(str(tenant_id), provider_type)]
        if not secrets:
            continue
        try:
            results.append(
                run_infra_sync(
                    str(tenant_id), provider_type, secrets, months=months, trigger="scheduled"
                )
            )
        except Exception as exc:  # one tenant failing must not stop the rest
            logger.warning(
                "scheduled infra sync failed tenant=%s provider=%s: %s",
                tenant_id,
                provider_type,
                exc,
            )
            results.append(
                {"tenant_id": str(tenant_id), "provider": provider_type, "error": str(exc)[:200]}
            )
    return results


# ---------------------------------------------------------------------------
# Read paths (the tab)
# ---------------------------------------------------------------------------
def provider_status(tenant_id: str) -> list[dict]:
    """Every infrastructure provider with its connection state and last sync.

    `status` is registry metadata rather than a constant: every provider listed
    is usable today, and one that is ever listed before it is is marked here
    rather than left off the page — a customer asking "can I connect this?"
    deserves an answer on the page, not the absence of a row.

    What "connected" means depends on how a provider's numbers arrive. An API
    provider is connected when it holds a credential. A file-import provider has
    no credential to hold, so it is connected once it has actually been given a
    file — anything else would be a card claiming a connection nobody made.
    """
    connected: set[str] = set()
    imported: set[str] = set()
    last_runs: dict[str, dict] = {}
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        connected = {
            r[0] for r in conn.execute("SELECT DISTINCT connector_type FROM connector_credential")
        }
        imported = {
            r[0]
            for r in conn.execute("SELECT DISTINCT provider FROM infra_cost WHERE source = 'csv'")
        }
        for row in conn.execute(
            """
            SELECT DISTINCT ON (provider)
                   provider, status, started_at, finished_at, items, amount, error_message
            FROM infra_sync_run
            ORDER BY provider, started_at DESC
            """
        ).fetchall():
            last_runs[row[0]] = {
                "status": row[1],
                "started_at": row[2].isoformat() if row[2] else None,
                "finished_at": row[3].isoformat() if row[3] else None,
                "items": row[4],
                "amount": float(row[5] or 0),
                "error_message": row[6],
            }

    out = []
    for p in PROVIDERS:
        if p.get("ingest") == "csv":
            is_connected = p["type"] in imported
            secret = None
        else:
            # A credential is not a connection unless there is a client behind
            # it, so a provider this module cannot ingest never reads connected.
            is_connected = p["type"] in LIVE_PROVIDERS and p["type"] in connected
            # The newest credential, and only to describe non-secret settings
            # (region, metric) on the card. With several accounts this describes
            # one of them; it is display, never a figure.
            secret = credentials.get_secret(tenant_id, p["type"]) if is_connected else None
        out.append(
            {
                **p,
                "connected": is_connected,
                "last_sync": last_runs.get(p["type"]),
                # Non-secret settings only; keys are never returned.
                "config": describe_config(secret, p["type"]) if secret else None,
            }
        )
    return out


def summary(tenant_id: str, provider_type: str = "aws", month: Optional[dt.date] = None) -> dict:
    """Counted infrastructure spend for a month, by service, plus what was excluded.

    Deliberately reports the excluded (Bedrock) total as its own number rather
    than hiding it: a customer looking at an AWS bill and at this page should be
    able to see where the difference went.
    """
    anchor = month_start(month or dt.date.today())
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        services = conn.execute(
            """
            SELECT service, SUM(amount), SUM(amount) FILTER (WHERE feature_id IS NOT NULL)
            FROM infra_cost
            WHERE provider = %s AND month = %s AND counted AND category = 'infrastructure'
            GROUP BY service
            ORDER BY 2 DESC
            LIMIT 50
            """,
            (provider_type, anchor),
        ).fetchall()
        totals = conn.execute(
            """
            SELECT
                COALESCE(SUM(amount) FILTER (
                    WHERE counted AND category = 'infrastructure'), 0),
                COALESCE(SUM(amount) FILTER (
                    WHERE counted AND category = 'infrastructure'
                      AND feature_id IS NOT NULL), 0),
                COALESCE(SUM(amount) FILTER (WHERE NOT counted), 0),
                COUNT(*)
            FROM infra_cost
            WHERE provider = %s AND month = %s
            """,
            (provider_type, anchor),
        ).fetchone()
        by_category = conn.execute(
            """
            SELECT category, COALESCE(SUM(amount), 0)
            FROM infra_cost
            WHERE provider = %s AND month = %s AND counted
            GROUP BY category ORDER BY 2 DESC
            """,
            (provider_type, anchor),
        ).fetchall()

    total, attributed, excluded, rows = totals
    return {
        "provider": provider_type,
        "month": anchor.isoformat(),
        "total": float(total),
        "attributed": float(attributed),
        "unattributed": float(Decimal(total) - Decimal(attributed)),
        # Recorded, and owned by another connector. See the module docstring.
        "excluded": float(excluded),
        "rows": rows,
        "by_category": [{"category": c, "amount": float(a)} for c, a in by_category],
        "services": [
            {"service": s, "amount": float(a), "attributed": float(at or 0)}
            for s, a, at in services
        ],
    }


if __name__ == "__main__":
    runs = run_scheduled_infra_sync()
    print(f"Synced {len(runs)} tenant/provider infrastructure runs.")
    for run in runs:
        print(" ", run)
