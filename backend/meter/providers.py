"""Read-only provider cost connectors (Anthropic + OpenAI).

Each client fetches a month of spend from a provider's Admin/Cost API and returns
a list of normalized ``CostRecord``s — the authoritative dollar total broken down
by api key / project (workspace) / model. GET-only (read-only). The admin key is
the customer's own, supplied per tenant (stored encrypted).

NOTE: the exact JSON shapes of these Admin APIs evolve and can't be verified
offline. Parsing here follows the documented spec and is deliberately tolerant;
the real responses should be validated against a live account. The normalized
``CostRecord`` is the stable contract the rest of the system depends on, and the
attribution/persistence logic (inference.py) is fully tested against it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

import httpx

from .pricing import price
from .retrying import http_get_with_retry

# Anthropic's Cost Report reports `amount` in the currency's lowest unit (cents);
# every other provider here reports dollars. Divide Anthropic amounts by this once,
# at the parse boundary, so the rest of the system is uniformly in dollars.
_CENTS_PER_DOLLAR = Decimal(100)


@dataclass
class CostRecord:
    provider: str  # "anthropic" | "openai"
    period: dt.date  # month start
    amount: Decimal
    currency: str = "USD"
    api_key_ref: Optional[str] = None
    project: Optional[str] = None  # OpenAI project / Anthropic workspace
    model: Optional[str] = None
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    request_count: Optional[int] = None
    # Input tokens served from the provider's prompt cache (opt spec §8). None when
    # the provider doesn't report it; used for connector-only cache utilization.
    cached_tokens_in: Optional[int] = None
    # Input tokens written INTO the cache (cache creation) — billed at a premium.
    # Part of tokens_in, tracked separately for the by-token-type breakdown.
    # Anthropic prices writes by TTL, so the 5m/1h split is kept when reported.
    cache_write_tokens: Optional[int] = None
    cache_write_5m: Optional[int] = None
    cache_write_1h: Optional[int] = None


@dataclass
class UsageRecord:
    """A detailed usage row from a provider's Usage Report API (tokens, not dollars).

    Carries the identity dimensions the Cost Report lacks — ``workspace_id`` and
    ``api_key_id`` — so spend can be attributed to a workspace/key and classified.
    Dollars are NOT taken from here: the Cost Report stays the billing authority
    and usage is priced only to weight the proportional split (see inference.py).
    """

    workspace_id: Optional[str] = None
    api_key_id: Optional[str] = None
    model: Optional[str] = None
    service_tier: Optional[str] = None
    tokens_in: int = 0
    tokens_out: int = 0
    cached_tokens_in: int = 0
    cache_write_tokens: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    request_count: int = 0
    day: Optional[dt.date] = None  # the usage day (from the report's daily bucket)


class ProviderError(Exception):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def month_start(day: dt.date) -> dt.date:
    return day.replace(day=1)


def next_month(start: dt.date) -> dt.date:
    return (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1)


def _cache_creation(item: dict) -> tuple[int, int]:
    """(5-minute, 1-hour) cache-WRITE tokens from a usage/cost line item.

    Anthropic reports cache creation as a nested object keyed by cache TTL
    (``cache_creation.ephemeral_5m_input_tokens`` / ``...1h...``) because the two
    are priced differently. Older/simpler shapes use a single flat
    ``cache_creation_input_tokens``; that is treated as 5-minute (the default TTL).
    """
    cc = item.get("cache_creation")
    if isinstance(cc, dict):
        m5 = _int_or_none(cc.get("ephemeral_5m_input_tokens")) or 0
        h1 = _int_or_none(cc.get("ephemeral_1h_input_tokens")) or 0
        if m5 or h1:
            return m5, h1
    return (_int_or_none(item.get("cache_creation_input_tokens")) or 0, 0)


def _bucket_day(bucket: dict, fallback: dt.date) -> dt.date:
    """The day a cost/usage bucket represents, from its ``starting_at``/``start_time``.

    Provider reports return daily buckets; this pulls the day so we can persist cost
    at day resolution. Falls back to the month anchor if a bucket omits its date.
    """
    raw = bucket.get("starting_at") or bucket.get("start_time")
    if raw is None:
        return fallback
    if isinstance(raw, (int, float)):  # epoch seconds (OpenAI)
        return dt.datetime.fromtimestamp(raw, dt.timezone.utc).date()
    try:
        return dt.date.fromisoformat(str(raw)[:10])
    except ValueError:
        return fallback


def month_query_end(start: dt.date, *, today: Optional[dt.date] = None) -> dt.date:
    """Exclusive end date for a cost query over the month beginning at ``start``.

    A fully-elapsed month ends at the first of the next month. The CURRENT
    (in-progress) month is instead capped at *tomorrow*, so we query month-to-date
    rather than into the future: provider cost APIs (Anthropic's cost/usage reports
    among them) reject — or silently return nothing for — a future ``ending_at``,
    which is why the current month otherwise imports no rows. Past months are
    unaffected (``next_month`` is already <= tomorrow).
    """
    today = today or dt.date.today()
    return min(next_month(start), today + dt.timedelta(days=1))


def aggregate(records: list[CostRecord]) -> list[CostRecord]:
    """Collapse records sharing (api_key_ref, project, model) into one row."""
    buckets: dict[tuple, CostRecord] = {}
    for r in records:
        key = (r.provider, r.period, r.api_key_ref, r.project, r.model, r.currency)
        if key not in buckets:
            buckets[key] = CostRecord(
                provider=r.provider,
                period=r.period,
                amount=Decimal("0"),
                currency=r.currency,
                api_key_ref=r.api_key_ref,
                project=r.project,
                model=r.model,
                tokens_in=0,
                tokens_out=0,
                request_count=0,
            )
        agg = buckets[key]
        agg.amount += r.amount
        agg.tokens_in = (agg.tokens_in or 0) + (r.tokens_in or 0)
        agg.tokens_out = (agg.tokens_out or 0) + (r.tokens_out or 0)
        agg.request_count = (agg.request_count or 0) + (r.request_count or 0)
        if r.cached_tokens_in is not None:
            agg.cached_tokens_in = (agg.cached_tokens_in or 0) + r.cached_tokens_in
        if r.cache_write_tokens is not None:
            agg.cache_write_tokens = (agg.cache_write_tokens or 0) + r.cache_write_tokens
        if r.cache_write_5m is not None:
            agg.cache_write_5m = (agg.cache_write_5m or 0) + r.cache_write_5m
        if r.cache_write_1h is not None:
            agg.cache_write_1h = (agg.cache_write_1h or 0) + r.cache_write_1h
    return list(buckets.values())


class _BaseCostClient:
    base_url = ""

    def __init__(
        self,
        admin_key: str,
        *,
        client: Optional[httpx.Client] = None,
        base_url: Optional[str] = None,
    ):
        self._key = admin_key
        self._base = (base_url or self.base_url).rstrip("/")
        self._client = client or httpx.Client(timeout=30.0)
        self._owns_client = client is None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def close(self):
        if self._owns_client:
            self._client.close()

    def _get(self, path: str, params: dict, headers: dict) -> httpx.Response:
        resp = http_get_with_retry(
            self._client, f"{self._base}{path}", params=params, headers=headers
        )
        if resp.status_code == 401:
            raise ProviderError("Provider rejected the admin key (401).", 401)
        if resp.status_code >= 400:
            raise ProviderError(
                f"Provider API error {resp.status_code}: {resp.text[:200]}", resp.status_code
            )
        return resp


class AnthropicCostClient(_BaseCostClient):
    """Anthropic Admin Cost + Usage + org-metadata API. Auth via x-api-key admin key.

    Two read paths, kept distinct:
      * ``fetch_costs`` — the Cost Report (authoritative billed dollars, grouped by
        workspace + description). The billing source of truth.
      * ``fetch_usage`` — the Messages Usage Report (tokens, grouped by workspace +
        api_key + model + tier). Provides the identity dimensions the cost report
        lacks; used only to split the authoritative dollars, never to compute them.
    Plus two org-metadata lookups (``fetch_workspaces``, ``fetch_api_keys``) that
    resolve the ids in usage rows to human names.
    """

    base_url = "https://api.anthropic.com"

    def _headers(self) -> dict:
        return {"x-api-key": self._key, "anthropic-version": "2023-06-01"}

    def fetch_costs(self, period: dt.date) -> list[CostRecord]:
        start = month_start(period)
        end = month_query_end(start)  # capped at tomorrow -> month-to-date, never future
        # The Cost Report returns one DAILY bucket per page and defaults to just 7
        # buckets — so without an explicit limit AND pagination it silently returns
        # only the first week of the month, dropping every workspace whose spend
        # lands later. Request the full month of buckets and page through, exactly
        # like fetch_usage below.
        records: list[CostRecord] = []
        page: Optional[str] = None
        while True:
            params: dict = {
                "starting_at": start.isoformat(),
                "ending_at": end.isoformat(),
                # cost_report only groups by workspace_id + description (NOT model);
                # the description line-item carries the model/token-type label.
                "group_by[]": ["workspace_id", "description"],
                "limit": 31,  # daily buckets in a month
            }
            if page:
                params["page"] = page
            data = self._get("/v1/organizations/cost_report", params, self._headers()).json()
            records.extend(_parse_anthropic(data, start))
            if data.get("has_more") and data.get("next_page"):
                page = data["next_page"]
                continue
            return aggregate(records)

    def fetch_usage(self, period: dt.date) -> list[UsageRecord]:
        """Detailed token usage for the month, grouped by workspace/key/model/tier.

        Paginated via the ``page`` cursor. Returns per-bucket rows (typically daily);
        the caller aggregates. Dollars are intentionally absent — this is attribution
        detail, not billing.
        """
        start = month_start(period)
        end = month_query_end(start)  # capped at tomorrow -> month-to-date, never future
        out: list[UsageRecord] = []
        page: Optional[str] = None
        while True:
            params: dict = {
                "starting_at": start.isoformat(),
                "ending_at": end.isoformat(),
                "bucket_width": "1d",
                "group_by[]": ["workspace_id", "api_key_id", "model", "service_tier"],
                "limit": 31,  # daily buckets in a month
            }
            if page:
                params["page"] = page
            data = self._get(
                "/v1/organizations/usage_report/messages", params, self._headers()
            ).json()
            out.extend(_parse_anthropic_usage(data, start))
            if data.get("has_more") and data.get("next_page"):
                page = data["next_page"]
                continue
            return out

    def fetch_workspaces(self) -> dict[str, str]:
        """Resolve ``workspace_id -> workspace_name`` for the org (paginated)."""
        out: dict[str, str] = {}
        for ws in self._paginate_admin("/v1/organizations/workspaces"):
            wid = ws.get("id")
            if wid:
                out[wid] = ws.get("name") or wid
        return out

    def fetch_api_keys(self) -> dict[str, dict]:
        """Resolve ``api_key_id -> {name, workspace_id}`` for the org (paginated)."""
        out: dict[str, dict] = {}
        for key in self._paginate_admin("/v1/organizations/api_keys"):
            kid = key.get("id")
            if kid:
                out[kid] = {"name": key.get("name"), "workspace_id": key.get("workspace_id")}
        return out

    def _paginate_admin(self, path: str) -> list[dict]:
        """Walk an Admin list endpoint via the ``after_id``/``has_more`` cursor."""
        items: list[dict] = []
        after: Optional[str] = None
        while True:
            params: dict = {"limit": 100}
            if after:
                params["after_id"] = after
            data = self._get(path, params, self._headers()).json()
            batch = data.get("data") or []
            items.extend(b for b in batch if isinstance(b, dict))
            if data.get("has_more") and data.get("last_id"):
                after = data["last_id"]
                continue
            return items


class OpenAICostClient(_BaseCostClient):
    """OpenAI organization Costs API. Auth via Bearer admin key."""

    base_url = "https://api.openai.com"

    def fetch_costs(self, period: dt.date) -> list[CostRecord]:
        start = month_start(period)
        end = month_query_end(start)  # month-to-date, never into the future
        # Like Anthropic's, the Costs API returns daily buckets and paginates — page
        # through so a long month is never truncated to the first page of days.
        records: list[CostRecord] = []
        page: Optional[str] = None
        while True:
            params: dict = {
                "start_time": int(dt.datetime(start.year, start.month, start.day).timestamp()),
                "end_time": int(dt.datetime(end.year, end.month, end.day).timestamp()),
                "group_by[]": ["project_id", "line_item"],
                "limit": 180,
            }
            if page:
                params["page"] = page
            data = self._get(
                "/v1/organization/costs",
                params,
                headers={"Authorization": f"Bearer {self._key}"},
            ).json()
            records.extend(_parse_openai(data, start))
            if data.get("has_more") and data.get("next_page"):
                page = data["next_page"]
                continue
            return aggregate(records)


class GoogleCostClient(_BaseCostClient):
    """Google Cloud Billing for Gemini / Vertex spend. Auth via OAuth bearer token.

    Google has no per-API-key cost endpoint like Anthropic/OpenAI — spend lives in
    Cloud Billing, broken down by project + SKU/model. We attribute by GCP project
    (map project -> feature, like the OpenAI project path). The shape follows the
    documented Cloud Billing reports and is parsed tolerantly; cost is the reported
    dollar amount, or computed from tokens when only usage is returned.
    """

    base_url = "https://cloudbilling.googleapis.com"

    def fetch_costs(self, period: dt.date) -> list[CostRecord]:
        start = month_start(period)
        end = month_query_end(start)  # month-to-date, never into the future
        resp = self._get(
            "/v1/cost",
            params={"start_date": start.isoformat(), "end_date": end.isoformat()},
            headers={"Authorization": f"Bearer {self._key}"},
        )
        return aggregate(list(_parse_google(resp.json(), start)))


def _parse_google(payload: dict, period: dt.date):
    rows = payload.get("data") or payload.get("costs") or payload.get("results") or []
    for item in rows:
        if not isinstance(item, dict):
            continue
        model = item.get("model") or item.get("sku") or item.get("service")
        tokens_in = int(item.get("prompt_tokens") or item.get("input_tokens") or 0)
        tokens_out = int(item.get("completion_tokens") or item.get("output_tokens") or 0)
        requests = item.get("requests") or item.get("request_count")
        amount = _to_decimal(item.get("cost") or item.get("amount"))
        if amount is None:
            amount = price(model or "", tokens_in, tokens_out, "google")
        yield CostRecord(
            provider="google",
            period=period,
            amount=amount,
            currency=item.get("currency", "USD"),
            project=item.get("project_id") or item.get("project"),
            model=model,
            tokens_in=tokens_in or None,
            tokens_out=tokens_out or None,
            request_count=int(requests) if requests is not None else None,
        )


def _int_or_none(value) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (ValueError, TypeError):
        return None


def _parse_anthropic(payload: dict, period: dt.date):
    for bucket in payload.get("data", []):
        day = _bucket_day(bucket, period)  # daily bucket -> the actual day
        for item in bucket.get("results", []) or bucket.get("items", []):
            raw = _to_decimal(item.get("amount"))
            if raw is None:
                continue
            # Anthropic's cost_report returns `amount` in the currency's LOWEST unit
            # (cents) as a decimal string — per the Cost API contract, "123.45" USD
            # means $1.2345. Convert to dollars ONCE here at the source so every
            # downstream number (storage, reconciliation, dashboard) is in dollars.
            amount = raw / _CENTS_PER_DOLLAR
            # Cache/token fields when the report includes usage (parsed tolerantly;
            # None when absent). cache_read_input_tokens = input served from cache.
            cache_read = _int_or_none(item.get("cache_read_input_tokens"))
            write_5m, write_1h = _cache_creation(item)
            cache_write = (write_5m + write_1h) or None
            input_tokens = _int_or_none(item.get("input_tokens"))
            if input_tokens is None and cache_read is not None:
                # Some reports split input; total = uncached + cache read (+ creation).
                uncached = _int_or_none(item.get("uncached_input_tokens")) or 0
                input_tokens = uncached + (cache_write or 0) + cache_read
            yield CostRecord(
                provider="anthropic",
                period=day,
                amount=amount,
                currency=item.get("currency", "USD"),
                api_key_ref=item.get("api_key_id"),
                project=item.get("workspace_id"),
                # cost_report returns "description" (e.g. "Claude Sonnet 4.5 · input");
                # fall back to it so the line-item label survives as the model.
                model=item.get("model") or item.get("description"),
                tokens_in=input_tokens,
                tokens_out=_int_or_none(item.get("output_tokens")),
                cached_tokens_in=cache_read,
                cache_write_tokens=cache_write,
                cache_write_5m=write_5m or None,
                cache_write_1h=write_1h or None,
            )


def _parse_anthropic_usage(payload: dict, period: dt.date):
    """Yield UsageRecords from a Messages Usage Report response (tolerant parsing)."""
    for bucket in payload.get("data", []):
        day = _bucket_day(bucket, period)  # daily bucket -> the actual usage day
        for item in bucket.get("results", []) or bucket.get("items", []):
            if not isinstance(item, dict):
                continue
            # Total input = uncached + cache-creation + cache-read; some payloads
            # instead give a single input_tokens. cache_read is tracked separately.
            cache_read = _int_or_none(item.get("cache_read_input_tokens")) or 0
            write_5m, write_1h = _cache_creation(item)
            cache_write = write_5m + write_1h
            input_tokens = _int_or_none(item.get("input_tokens"))
            if input_tokens is None:
                uncached = _int_or_none(item.get("uncached_input_tokens")) or 0
                input_tokens = uncached + cache_write + cache_read
            yield UsageRecord(
                workspace_id=item.get("workspace_id"),
                api_key_id=item.get("api_key_id"),
                model=item.get("model"),
                service_tier=item.get("service_tier"),
                tokens_in=input_tokens,
                tokens_out=_int_or_none(item.get("output_tokens")) or 0,
                cached_tokens_in=cache_read,
                cache_write_tokens=cache_write,
                cache_write_5m=write_5m,
                cache_write_1h=write_1h,
                request_count=_int_or_none(item.get("request_count"))
                or _int_or_none(item.get("num_requests"))
                or 0,
                day=day,
            )


def _openai_cached_tokens(item: dict) -> Optional[int]:
    details = item.get("input_tokens_details") or item.get("prompt_tokens_details")
    if isinstance(details, dict):
        cached = _int_or_none(details.get("cached_tokens"))
        if cached is not None:
            return cached
    return _int_or_none(item.get("cached_tokens"))


def _parse_openai(payload: dict, period: dt.date):
    for bucket in payload.get("data", []):
        day = _bucket_day(bucket, period)  # daily bucket -> the actual day
        for item in bucket.get("results", []):
            amount = _to_decimal(
                (item.get("amount") or {}).get("value")
                if isinstance(item.get("amount"), dict)
                else item.get("amount")
            )
            if amount is None:
                continue
            yield CostRecord(
                provider="openai",
                period=day,
                amount=amount,
                currency=(item.get("amount") or {}).get("currency", "USD")
                if isinstance(item.get("amount"), dict)
                else "USD",
                api_key_ref=item.get("api_key_id"),
                project=item.get("project_id"),
                model=item.get("model") or item.get("line_item"),
                tokens_in=_int_or_none(item.get("input_tokens") or item.get("prompt_tokens")),
                tokens_out=_int_or_none(item.get("output_tokens") or item.get("completion_tokens")),
                cached_tokens_in=_openai_cached_tokens(item),
            )


def _to_decimal(value) -> Optional[Decimal]:
    if value is None or isinstance(value, (dict, list)):
        return None
    try:
        return Decimal(str(value))
    except (ValueError, ArithmeticError):
        return None


# --------------------------------------------------------------------------
# Hosted open-source aggregators (Together, Fireworks, OpenRouter)
# --------------------------------------------------------------------------
# Each fronts many open-weight models behind one API key. Their usage/cost APIs
# are OpenAI-ish and return per-(model, key) rows for a window. Cost is taken
# from a reported dollar amount when present, else computed from token counts via
# our pricing tables (keyed by (provider, model)). As with the Anthropic/OpenAI
# clients, the exact JSON shapes can't be verified offline — parsing is tolerant
# and the stable contract is the normalized CostRecord.
_HOSTED_PROVIDERS = {
    "openrouter": ("https://openrouter.ai", "/api/v1/activity"),
    "together": ("https://api.together.xyz", "/v1/usage"),
    "fireworks": ("https://api.fireworks.ai", "/v1/usage"),
    # OpenAI-compatible inference providers (Bearer key + a usage endpoint). Cost
    # is the reported dollar amount when present, else priced from tokens via the
    # (provider, model) table in pricing.py. Endpoints are best-effort per their
    # docs and parsed tolerantly; the hook SDK is the precise path for these.
    "groq": ("https://api.groq.com", "/openai/v1/usage"),
    "mistral": ("https://api.mistral.ai", "/v1/usage"),
    "xai": ("https://api.x.ai", "/v1/usage"),
    "perplexity": ("https://api.perplexity.ai", "/v1/usage"),
    "cohere": ("https://api.cohere.ai", "/v1/usage"),
    "replicate": ("https://api.replicate.com", "/v1/account"),
}


class HostedUsageCostClient(_BaseCostClient):
    """Generic cost client for hosted open-source aggregators."""

    def __init__(self, provider: str, admin_key: str, path: str, **kwargs):
        super().__init__(admin_key, **kwargs)
        self.provider = provider
        self.path = path

    def fetch_costs(self, period: dt.date) -> list[CostRecord]:
        start = month_start(period)
        end = month_query_end(start)  # month-to-date, never into the future
        resp = self._get(
            self.path,
            params={"start_date": start.isoformat(), "end_date": end.isoformat()},
            headers={"Authorization": f"Bearer {self._key}"},
        )
        return aggregate(list(_parse_hosted_usage(resp.json(), self.provider, start)))


def _parse_hosted_usage(payload: dict, provider: str, period: dt.date):
    rows = payload.get("data") or payload.get("usage") or payload.get("results") or []
    for item in rows:
        if not isinstance(item, dict):
            continue
        model = item.get("model") or item.get("model_name")
        tokens_in = int(
            item.get("prompt_tokens") or item.get("input_tokens") or item.get("tokens_in") or 0
        )
        tokens_out = int(
            item.get("completion_tokens")
            or item.get("output_tokens")
            or item.get("tokens_out")
            or 0
        )
        requests = item.get("requests") or item.get("request_count") or item.get("count")
        # Prefer a reported dollar cost; otherwise price the tokens ourselves.
        amount = _to_decimal(
            item.get("cost") or item.get("amount") or item.get("total_cost") or item.get("usage")
        )
        if amount is None:
            amount = price(model or "", tokens_in, tokens_out, provider)
        yield CostRecord(
            provider=provider,
            period=period,
            amount=amount,
            currency=item.get("currency", "USD"),
            api_key_ref=item.get("api_key_id") or item.get("api_key"),
            model=model,
            tokens_in=tokens_in or None,
            tokens_out=tokens_out or None,
            request_count=int(requests) if requests is not None else None,
        )


# --------------------------------------------------------------------------
# Amazon Bedrock — cloud-cost connector (AWS Cost Explorer)
# --------------------------------------------------------------------------
# Bedrock has no model-provider cost API; its spend is in AWS billing. We read
# Cost Explorer (ce.us-east-1.amazonaws.com), filter to "Amazon Bedrock", and
# group by a cost-allocation TAG the customer sets per feature (the AWS-standard
# way to split shared cloud spend). The tag value attributes to a feature like an
# API key; untagged Bedrock spend -> Unattributed. Credentials are an AWS access
# key/secret/region/tag, stored as one encrypted JSON blob. Signed with SigV4 by
# hand to avoid a heavy boto3 dependency (keeps the backend thin/serverless-ish).
_CE_HOST = "ce.us-east-1.amazonaws.com"
_CE_REGION = "us-east-1"
_CE_SERVICE = "ce"
_CE_TARGET = "AWSInsightsIndexService.GetCostAndUsage"


def _sigv4_headers(secret_key, access_key, target, body, now):
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    content_type = "application/x-amz-json-1.1"
    canonical_headers = (
        f"content-type:{content_type}\nhost:{_CE_HOST}\n"
        f"x-amz-date:{amz_date}\nx-amz-target:{target}\n"
    )
    signed_headers = "content-type;host;x-amz-date;x-amz-target"
    payload_hash = hashlib.sha256(body).hexdigest()
    canonical_request = f"POST\n/\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    scope = f"{datestamp}/{_CE_REGION}/{_CE_SERVICE}/aws4_request"
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n"
        f"{hashlib.sha256(canonical_request.encode()).hexdigest()}"
    )

    def _hmac(key, msg):
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    k_date = _hmac(("AWS4" + secret_key).encode(), datestamp)
    k_region = _hmac(k_date, _CE_REGION)
    k_service = _hmac(k_region, _CE_SERVICE)
    signing_key = _hmac(k_service, "aws4_request")
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    authorization = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return {
        "Content-Type": content_type,
        "X-Amz-Date": amz_date,
        "X-Amz-Target": target,
        "Authorization": authorization,
    }


class BedrockCostClient(_BaseCostClient):
    """AWS Cost Explorer connector for Amazon Bedrock spend."""

    base_url = f"https://{_CE_HOST}"

    def __init__(self, admin_key: str, **kwargs):
        super().__init__(admin_key, **kwargs)
        try:
            creds = json.loads(admin_key)
        except (ValueError, TypeError) as exc:
            raise ProviderError(
                "Bedrock credential must be JSON: "
                '{"access_key_id":..., "secret_access_key":..., "region":..., "tag":...}'
            ) from exc
        self._access = creds.get("access_key_id") or creds.get("access_key")
        self._secret = creds.get("secret_access_key") or creds.get("secret_key")
        self._tag = creds.get("tag", "feature")

    def fetch_costs(self, period: dt.date) -> list[CostRecord]:
        start = month_start(period)
        end = month_query_end(start)  # month-to-date, never into the future
        body = json.dumps(
            {
                "TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},
                "Granularity": "MONTHLY",
                "Metrics": ["UnblendedCost"],
                "Filter": {"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Bedrock"]}},
                "GroupBy": [{"Type": "TAG", "Key": self._tag}],
            }
        ).encode()
        headers = _sigv4_headers(
            self._secret, self._access, _CE_TARGET, body, dt.datetime.now(dt.timezone.utc)
        )
        resp = self._client.post(f"{self._base}/", content=body, headers=headers)
        if resp.status_code in (401, 403):
            raise ProviderError("AWS rejected the credentials.", resp.status_code)
        if resp.status_code >= 400:
            raise ProviderError(
                f"Cost Explorer error {resp.status_code}: {resp.text[:200]}", resp.status_code
            )
        return aggregate(list(_parse_bedrock(resp.json(), start)))


def _parse_bedrock(payload: dict, period: dt.date):
    for window in payload.get("ResultsByTime", []):
        for group in window.get("Groups", []):
            keys = group.get("Keys") or [""]
            # TAG group keys look like "feature$triage"; the value is after the "$".
            raw = keys[0]
            tag_value = raw.split("$", 1)[1] if "$" in raw else raw
            metrics = group.get("Metrics", {})
            amount = _to_decimal((metrics.get("UnblendedCost") or {}).get("Amount"))
            if amount is None:
                continue
            yield CostRecord(
                provider="bedrock",
                period=period,
                amount=amount,
                currency=(metrics.get("UnblendedCost") or {}).get("Unit", "USD"),
                api_key_ref=tag_value or None,  # empty tag -> Unattributed
            )


# --------------------------------------------------------------------------
# Gateways, cloud-cost, and audio providers (added per customer demand)
# --------------------------------------------------------------------------
# Each follows the vendor's documented cost/usage API. As with the clients
# above, exact JSON shapes can't be verified offline — parsing is tolerant and
# the stable contract is the normalized CostRecord. JSON-credential connectors
# take one encrypted blob; see web/src/connectorGuides.ts for each shape.

# ElevenLabs bills credits/characters, not dollars; this transparent rate maps
# character usage to an approximate cost. Edit as plans change (drift is visible,
# never a silently-wrong number).
_ELEVENLABS_USD_PER_1K_CHARS = Decimal("0.15")


def _json_cred(admin_key: str, hint: str) -> dict:
    """Parse a JSON credential blob or raise a clear ProviderError."""
    try:
        creds = json.loads(admin_key)
    except (ValueError, TypeError) as exc:
        raise ProviderError(f"Credential must be JSON: {hint}") from exc
    if not isinstance(creds, dict):
        raise ProviderError(f"Credential must be JSON: {hint}")
    return creds


def _first_tag_value(row: list) -> Optional[str]:
    """Best-effort: the first short non-currency string in a cost-query row."""
    for v in row:
        if isinstance(v, str) and len(v) <= 64 and v.upper() not in ("USD", "EUR", "GBP"):
            return v
    return None


class AzureCostClient(_BaseCostClient):
    """Azure Cost Management for Azure OpenAI / Cognitive Services spend.

    JSON cred: {tenant_id, client_id, client_secret, subscription_id, tag?}. A
    service principal (Cost Management Reader) authenticates via client-credentials;
    we query the Cost Management API filtered to Cognitive Services and grouped by
    a cost-allocation tag, attributing each tag value to a feature (untagged ->
    Unattributed), mirroring the Bedrock connector.
    """

    base_url = "https://management.azure.com"
    _AUTH_HOST = "https://login.microsoftonline.com"

    def __init__(self, admin_key, **kwargs):
        super().__init__(admin_key, **kwargs)
        c = _json_cred(
            admin_key,
            '{"tenant_id":…, "client_id":…, "client_secret":…, "subscription_id":…, "tag":…}',
        )
        self._tenant = c.get("tenant_id")
        self._client_id = c.get("client_id")
        self._secret = c.get("client_secret")
        self._sub = c.get("subscription_id")
        self._tag = c.get("tag", "feature")

    def _token(self) -> str:
        resp = self._client.post(
            f"{self._AUTH_HOST}/{self._tenant}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._secret,
                "scope": "https://management.azure.com/.default",
            },
        )
        if resp.status_code >= 400:
            raise ProviderError(
                "Azure rejected the service-principal credentials.", resp.status_code
            )
        return resp.json().get("access_token", "")

    def fetch_costs(self, period: dt.date) -> list[CostRecord]:
        start = month_start(period)
        end = month_query_end(start)  # month-to-date, never into the future
        token = self._token()
        body = {
            "type": "ActualCost",
            "timeframe": "Custom",
            "timePeriod": {
                "from": start.isoformat(),
                "to": (end - dt.timedelta(days=1)).isoformat(),
            },
            "dataset": {
                "granularity": "None",
                "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
                "grouping": [{"type": "TagKey", "name": self._tag}],
                "filter": {
                    "dimensions": {
                        "name": "ServiceName",
                        "operator": "In",
                        "values": ["Cognitive Services", "Azure OpenAI"],
                    }
                },
            },
        }
        url = (
            f"{self._base}/subscriptions/{self._sub}"
            "/providers/Microsoft.CostManagement/query?api-version=2023-03-01"
        )
        resp = self._client.post(url, json=body, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code in (401, 403):
            raise ProviderError("Azure rejected the credentials.", resp.status_code)
        if resp.status_code >= 400:
            raise ProviderError(
                f"Azure Cost Management error {resp.status_code}: {resp.text[:200]}",
                resp.status_code,
            )
        return aggregate(list(_parse_azure(resp.json(), start)))


def _parse_azure(payload: dict, period: dt.date):
    props = payload.get("properties", payload)
    for row in props.get("rows", []):
        if not isinstance(row, list):
            continue
        amount = next((_to_decimal(v) for v in row if _to_decimal(v) is not None), None)
        if amount is None:
            continue
        yield CostRecord(
            provider="azure",
            period=period,
            amount=amount,
            api_key_ref=_first_tag_value(row)
            or None,  # tag value -> feature; empty -> Unattributed
        )


class LiteLLMCostClient(_BaseCostClient):
    """Self-hosted LiteLLM proxy spend report (GET /global/spend/report).

    JSON cred: {base_url, master_key}. The master key authorizes the proxy's admin
    spend endpoints; we read the per-key/model dollar spend it has already computed.
    """

    def __init__(self, admin_key, **kwargs):
        creds = _json_cred(admin_key, '{"base_url":"https://litellm.acme.com","master_key":"sk-…"}')
        base = (creds.get("base_url") or "").rstrip("/")
        super().__init__(creds.get("master_key") or "", base_url=base, **kwargs)

    def fetch_costs(self, period: dt.date) -> list[CostRecord]:
        start = month_start(period)
        end = month_query_end(start)  # month-to-date, never into the future
        resp = self._get(
            "/global/spend/report",
            params={"start_date": start.isoformat(), "end_date": end.isoformat()},
            headers={"Authorization": f"Bearer {self._key}"},
        )
        return aggregate(list(_parse_litellm(resp.json(), start)))


def _parse_litellm(payload, period: dt.date):
    rows = (
        payload
        if isinstance(payload, list)
        else (payload.get("data") or payload.get("spend") or payload.get("results") or [])
    )
    for item in rows:
        if not isinstance(item, dict):
            continue
        key_ref = (
            item.get("api_key") or item.get("team") or item.get("team_id") or item.get("team_alias")
        )
        breakdown = item.get("metadata") or item.get("models") or item.get("breakdown")
        if isinstance(breakdown, list) and breakdown:
            for b in breakdown:
                if not isinstance(b, dict):
                    continue
                amt = _to_decimal(b.get("spend") or b.get("total_spend") or b.get("cost"))
                if amt is None:
                    continue
                yield CostRecord(
                    provider="litellm",
                    period=period,
                    amount=amt,
                    api_key_ref=key_ref,
                    model=b.get("model") or b.get("model_name"),
                )
            continue
        amount = _to_decimal(item.get("spend") or item.get("total_spend") or item.get("cost"))
        if amount is None:
            continue
        yield CostRecord(
            provider="litellm",
            period=period,
            amount=amount,
            api_key_ref=key_ref,
            model=item.get("model") or item.get("model_name"),
        )


class VercelGatewayCostClient(_BaseCostClient):
    """Vercel AI Gateway Custom Reporting API (cost by model/project/tag).

    JSON cred: {token, team_id?, url?}. A Vercel access token authenticates; the
    reporting API is in beta, so the endpoint can be overridden via ``url``.
    """

    DEFAULT_URL = "https://api.vercel.com/v1/ai-gateway/usage"

    def __init__(self, admin_key, **kwargs):
        creds = _json_cred(admin_key, '{"token":"…","team_id":"…(optional)"}')
        self._token = creds.get("token") or ""
        self._team = creds.get("team_id")
        self._url = creds.get("url") or self.DEFAULT_URL
        super().__init__(self._token, **kwargs)

    def fetch_costs(self, period: dt.date) -> list[CostRecord]:
        start = month_start(period)
        end = month_query_end(start)  # month-to-date, never into the future
        params = {"start": start.isoformat(), "end": end.isoformat()}
        if self._team:
            params["teamId"] = self._team
        resp = http_get_with_retry(
            self._client,
            self._url,
            params=params,
            headers={"Authorization": f"Bearer {self._token}"},
        )
        if resp.status_code in (401, 403):
            raise ProviderError("Vercel rejected the access token.", resp.status_code)
        if resp.status_code >= 400:
            raise ProviderError(
                f"Vercel error {resp.status_code}: {resp.text[:200]}", resp.status_code
            )
        return aggregate(list(_parse_vercel(resp.json(), start)))


def _parse_vercel(payload: dict, period: dt.date):
    rows = (
        payload.get("data")
        or payload.get("usage")
        or payload.get("results")
        or payload.get("rows")
        or []
    )
    for item in rows:
        if not isinstance(item, dict):
            continue
        amount = _to_decimal(item.get("cost") or item.get("amount") or item.get("spend"))
        if amount is None:
            continue
        yield CostRecord(
            provider="vercel",
            period=period,
            amount=amount,
            project=item.get("project") or item.get("projectId") or item.get("tag"),
            api_key_ref=item.get("keyId") or item.get("apiKey"),
            model=item.get("model"),
        )


class ModalCostClient(_BaseCostClient):
    """Modal compute spend by app (billing usage report).

    JSON cred: {token_id, token_secret, url?}. Modal bills GPU/CPU time, reported
    per app; we attribute each app to a feature like a project. Beta endpoint, so
    ``url`` can override the default.
    """

    DEFAULT_URL = "https://api.modal.com/v1/billing/usage"

    def __init__(self, admin_key, **kwargs):
        creds = _json_cred(admin_key, '{"token_id":"ak-…","token_secret":"as-…"}')
        self._id = creds.get("token_id") or ""
        self._secret = creds.get("token_secret") or ""
        self._url = creds.get("url") or self.DEFAULT_URL
        super().__init__(self._secret, **kwargs)

    def fetch_costs(self, period: dt.date) -> list[CostRecord]:
        start = month_start(period)
        end = month_query_end(start)  # month-to-date, never into the future
        resp = http_get_with_retry(
            self._client,
            self._url,
            params={"start_date": start.isoformat(), "end_date": end.isoformat()},
            headers={"Modal-Key": self._id, "Modal-Secret": self._secret},
        )
        if resp.status_code in (401, 403):
            raise ProviderError("Modal rejected the token.", resp.status_code)
        if resp.status_code >= 400:
            raise ProviderError(
                f"Modal error {resp.status_code}: {resp.text[:200]}", resp.status_code
            )
        return aggregate(list(_parse_modal(resp.json(), start)))


def _parse_modal(payload: dict, period: dt.date):
    rows = (
        payload.get("data")
        or payload.get("usage")
        or payload.get("apps")
        or payload.get("results")
        or []
    )
    for item in rows:
        if not isinstance(item, dict):
            continue
        amount = _to_decimal(item.get("cost") or item.get("amount") or item.get("spend"))
        if amount is None:
            continue
        yield CostRecord(
            provider="modal",
            period=period,
            amount=amount,
            project=item.get("app") or item.get("app_name") or item.get("name"),
        )


class ElevenLabsCostClient(_BaseCostClient):
    """ElevenLabs character usage -> approximate cost. Auth: xi-api-key.

    ElevenLabs bills credits/characters, not dollars, so we read character usage
    for the month and price it at a transparent per-1k-character rate.
    """

    base_url = "https://api.elevenlabs.io"

    def fetch_costs(self, period: dt.date) -> list[CostRecord]:
        start = month_start(period)
        end = month_query_end(start)  # month-to-date, never into the future
        start_ms = int(
            dt.datetime(start.year, start.month, start.day, tzinfo=dt.timezone.utc).timestamp()
            * 1000
        )
        end_ms = int(
            dt.datetime(end.year, end.month, end.day, tzinfo=dt.timezone.utc).timestamp() * 1000
        )
        resp = self._get(
            "/v1/usage/character-stats",
            params={"start_unix": start_ms, "end_unix": end_ms, "aggregation_interval": "month"},
            headers={"xi-api-key": self._key},
        )
        return aggregate(list(_parse_elevenlabs(resp.json(), start)))


def _parse_elevenlabs(payload: dict, period: dt.date):
    usage = payload.get("usage") or {}
    total_chars = 0
    for series in usage.values():
        if isinstance(series, list):
            total_chars += sum(int(v) for v in series if isinstance(v, (int, float)))
    if total_chars <= 0:
        total_chars = int(payload.get("character_count") or 0)
    if total_chars <= 0:
        return
    amount = (_ELEVENLABS_USD_PER_1K_CHARS * Decimal(total_chars) / Decimal("1000")).quantize(
        Decimal("0.0001")
    )
    yield CostRecord(provider="elevenlabs", period=period, amount=amount, model="elevenlabs-tts")


class _AnalyticsGatewayClient(_BaseCostClient):
    """Base for gateways whose analytics API reports per-model dollar cost for a
    window. JSON cred: {api_key, url?}. Subclasses set the default URL, the auth
    header, and the query params. Endpoints are beta/best-effort and overridable.
    """

    DEFAULT_URL = ""
    provider = ""

    def __init__(self, admin_key, **kwargs):
        creds = _json_cred(admin_key, '{"api_key":"…"}')
        self._api_key = creds.get("api_key") or ""
        self._url = creds.get("url") or self.DEFAULT_URL
        super().__init__(self._api_key, **kwargs)

    def _auth_headers(self) -> dict:
        raise NotImplementedError

    def fetch_costs(self, period: dt.date) -> list[CostRecord]:
        start = month_start(period)
        end = month_query_end(start)  # month-to-date, never into the future
        resp = http_get_with_retry(
            self._client,
            self._url,
            params={"start_date": start.isoformat(), "end_date": end.isoformat()},
            headers=self._auth_headers(),
        )
        if resp.status_code in (401, 403):
            raise ProviderError(f"{self.provider} rejected the API key.", resp.status_code)
        if resp.status_code >= 400:
            raise ProviderError(
                f"{self.provider} error {resp.status_code}: {resp.text[:200]}", resp.status_code
            )
        return aggregate(list(_parse_analytics_cost(resp.json(), self.provider, start)))


class PortkeyCostClient(_AnalyticsGatewayClient):
    """Portkey analytics Get-Cost-Data API. Auth via x-portkey-api-key."""

    DEFAULT_URL = "https://api.portkey.ai/v1/analytics/graphs/cost"
    provider = "portkey"

    def _auth_headers(self) -> dict:
        return {"x-portkey-api-key": self._api_key}


class HeliconeCostClient(_AnalyticsGatewayClient):
    """Helicone cost query API. Auth via Bearer key."""

    DEFAULT_URL = "https://api.helicone.ai/v1/cost/query"
    provider = "helicone"

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_key}"}


def _parse_analytics_cost(payload, provider: str, period: dt.date):
    rows = (
        payload.get("data")
        or payload.get("results")
        or payload.get("cost")
        or (payload if isinstance(payload, list) else [])
    )
    if isinstance(rows, dict):
        rows = [rows]
    for item in rows:
        if not isinstance(item, dict):
            continue
        amount = _to_decimal(
            item.get("cost") or item.get("total_cost") or item.get("amount") or item.get("spend")
        )
        if amount is None:
            continue
        yield CostRecord(
            provider=provider,
            period=period,
            amount=amount,
            model=item.get("model"),
            api_key_ref=item.get("metadata") or item.get("user") or item.get("api_key"),
        )


def make_cost_client(provider: str, admin_key: str) -> _BaseCostClient:
    if provider == "anthropic":
        return AnthropicCostClient(admin_key)
    if provider == "openai":
        return OpenAICostClient(admin_key)
    if provider == "google":
        return GoogleCostClient(admin_key)
    if provider == "bedrock":
        return BedrockCostClient(admin_key)
    if provider == "azure":
        return AzureCostClient(admin_key)
    if provider == "litellm":
        return LiteLLMCostClient(admin_key)
    if provider == "vercel":
        return VercelGatewayCostClient(admin_key)
    if provider == "modal":
        return ModalCostClient(admin_key)
    if provider == "elevenlabs":
        return ElevenLabsCostClient(admin_key)
    if provider == "portkey":
        return PortkeyCostClient(admin_key)
    if provider == "helicone":
        return HeliconeCostClient(admin_key)
    if provider in _HOSTED_PROVIDERS:
        base_url, path = _HOSTED_PROVIDERS[provider]
        return HostedUsageCostClient(provider, admin_key, path, base_url=base_url)
    raise ValueError(f"Unknown inference provider: {provider}")
