"""Platform and managed-service billing connectors.

`providers.py` holds the model-provider cost APIs and the three hyperscaler
clients (AWS/Azure/GCP), which share SigV4 and a lot of history. This module
holds the rest of the infrastructure tab: the SaaS platforms and managed
services a product runs on, each of which publishes its own billing API.

They have nothing in common with each other technically — a Digest-authenticated
REST invoice API, a bearer-token subscription list, a key-pair-JWT SQL endpoint —
so they are separate clients. What they share is the contract: every one returns
a list of ``CloudCostItem``, which is what the ingest and the classifier consume.

**One rule governs what lives here: the vendor must report DOLLARS.** Several
platforms publish only usage — compute-hours, storage-GB, request counts — and a
connector that multiplied those by a price list would be printing a number this
product cannot defend. Invariant 5 says provider cost APIs are authoritative on
dollars; a modelled figure is not a bill. Those platforms are deliberately absent
rather than approximated.

Response shapes follow each vendor's documented API. Where a shape could not be
verified against a live account, the parser is written to require the fields it
actually needs (an amount and a date) and to skip a row it cannot read, rather
than to guess a value into existence.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import re
import time
from collections.abc import Iterable
from decimal import Decimal
from typing import Optional

import httpx

from .providers import (
    CloudCostItem,
    ProviderError,
    _BaseCostClient,
    _json_cred,
    _to_decimal,
)

# ---------------------------------------------------------------------------
# DigitalOcean — invoice line items
# ---------------------------------------------------------------------------
# The cleanest of the four: DigitalOcean issues a real invoice per month and
# exposes its items, each with a product, a description, a project and an
# amount. Auth is a read-only personal access token.
#
# DigitalOcean has no arbitrary cost-allocation tags, so feature attribution
# uses the PROJECT, which is the grouping customers actually organise by. That
# is a real mapping the customer controls, not one we inferred.
_DO_API = "https://api.digitalocean.com"


class DigitalOceanCostClient(_BaseCostClient):
    """Read-only invoice reader for DigitalOcean.

    JSON cred: ``{"token": "dop_v1_…"}`` — a personal access token with READ
    scope. Read scope cannot create, resize or destroy anything.
    """

    base_url = _DO_API

    def __init__(self, admin_key: str, **kwargs):
        super().__init__(admin_key, **kwargs)
        c = _json_cred(admin_key, '{"token":"dop_v1_…"}')
        self._token = c.get("token") or c.get("api_token") or c.get("access_token")
        if not self._token:
            raise ProviderError("DigitalOcean credentials need a read-only API token.")
        self.tag = (c.get("tag") or "project").strip() or "project"
        self.metric = "invoice"
        self.granularity = "MONTHLY"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        resp = self._client.get(f"{self._base}{path}", params=params, headers=self._headers())
        if resp.status_code in (401, 403):
            raise ProviderError(
                "DigitalOcean rejected the token. It needs to be a valid personal "
                "access token with read scope.",
                401,
            )
        if resp.status_code >= 400:
            raise ProviderError(
                f"DigitalOcean error {resp.status_code}: {resp.text[:200]}", resp.status_code
            )
        return resp.json()

    def fetch_items(self, start: dt.date, end: dt.date) -> list[CloudCostItem]:
        """Every invoice item billed in ``[start, end)``."""
        items: list[CloudCostItem] = []
        for invoice in self._invoices_in(start, end):
            uuid = invoice.get("invoice_uuid")
            if not uuid:
                continue
            period = _do_invoice_period(invoice)
            if period is None:
                continue
            detail = self._get(f"/v2/customers/my/invoices/{uuid}", {"per_page": 200})
            items.extend(_parse_do_items(detail, period, self.tag))
        return items

    def _invoices_in(self, start: dt.date, end: dt.date) -> Iterable[dict]:
        """Invoices whose billing month falls inside the window, following pages."""
        page = 1
        for _ in range(50):  # hard stop; 50 pages of invoices is years of history
            data = self._get("/v2/customers/my/invoices", {"page": page, "per_page": 50})
            batch = data.get("invoices") or []
            if not batch:
                return
            for invoice in batch:
                period = _do_invoice_period(invoice)
                if period is not None and start <= period < end:
                    yield invoice
            if not ((data.get("links") or {}).get("pages") or {}).get("next"):
                return
            page += 1


def _do_invoice_period(invoice: dict) -> Optional[dt.date]:
    """The month an invoice covers, from its "2026-05" invoice_period."""
    raw = str(invoice.get("invoice_period") or "").strip()
    if not raw:
        return None
    try:
        parts = raw.split("-")
        return dt.date(int(parts[0]), int(parts[1]), 1)
    except (ValueError, IndexError):
        return None


def _parse_do_items(detail: dict, period: dt.date, tag_key: str) -> list[CloudCostItem]:
    out: list[CloudCostItem] = []
    for row in detail.get("invoice_items") or []:
        if not isinstance(row, dict):
            continue
        amount = _to_decimal(row.get("amount"))
        if amount is None or amount == 0:
            continue
        # An item's own start_time is more precise than the invoice month.
        day = _iso_day(row.get("start_time")) or period
        project = row.get("project_name") or None
        out.append(
            CloudCostItem(
                period=day,
                amount=amount,
                currency="USD",  # DigitalOcean bills in USD only
                service=str(row.get("product") or ""),
                usage_type=row.get("description") or None,
                operation=row.get("group_description") or None,
                account_id=project,
                region=row.get("region") or None,
                tag_key=tag_key,
                tag_value=project,
                dimensions={
                    "product": row.get("product"),
                    "description": row.get("description"),
                    "group_description": row.get("group_description"),
                    "project_name": project,
                    "category": row.get("category"),
                    f"TAG:{tag_key}": project,
                },
            )
        )
    return out


# ---------------------------------------------------------------------------
# MongoDB Atlas — invoice line items
# ---------------------------------------------------------------------------
# Atlas bills per organisation and exposes each invoice's line items with a SKU,
# the project and cluster they belong to, and a price in CENTS. Auth is the
# long-standing programmatic API key pair over HTTP Digest.
#
# Feature attribution uses the PROJECT (Atlas "group"), which is how Atlas
# customers separate workloads — again a mapping the customer controls.
_ATLAS_API = "https://cloud.mongodb.com"
#: Atlas versions its API by Accept header rather than by URL.
_ATLAS_ACCEPT = "application/vnd.atlas.2023-01-01+json"
_CENTS = Decimal(100)


class MongoAtlasCostClient(_BaseCostClient):
    """Read-only invoice reader for MongoDB Atlas.

    JSON cred: ``{"public_key":…, "private_key":…, "org_id":…}``. The API key
    needs only the Organization Billing Viewer role.
    """

    base_url = _ATLAS_API

    def __init__(self, admin_key: str, **kwargs):
        super().__init__(admin_key, **kwargs)
        c = _json_cred(admin_key, '{"public_key":…, "private_key":…, "org_id":…}')
        self._public = c.get("public_key")
        self._private = c.get("private_key")
        self._org = c.get("org_id") or c.get("orgId")
        missing = [
            k
            for k, v in (
                ("public_key", self._public),
                ("private_key", self._private),
                ("org_id", self._org),
            )
            if not v
        ]
        if missing:
            raise ProviderError(
                "MongoDB Atlas credentials need " + ", ".join(missing) + " (a programmatic "
                "API key with the Organization Billing Viewer role)."
            )
        self.tag = (c.get("tag") or "project").strip() or "project"
        self.metric = "invoice"
        self.granularity = "MONTHLY"

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        resp = self._client.get(
            f"{self._base}{path}",
            params=params,
            headers={"Accept": _ATLAS_ACCEPT},
            # Atlas uses HTTP Digest, not bearer tokens.
            auth=httpx.DigestAuth(self._public, self._private),
        )
        if resp.status_code in (401, 403):
            raise ProviderError(
                "MongoDB Atlas rejected the API key. It needs the Organization Billing "
                "Viewer role, and your Meter egress IP may need to be on its access list.",
                401,
            )
        if resp.status_code >= 400:
            raise ProviderError(
                f"MongoDB Atlas error {resp.status_code}: {resp.text[:200]}", resp.status_code
            )
        return resp.json()

    def fetch_items(self, start: dt.date, end: dt.date) -> list[CloudCostItem]:
        items: list[CloudCostItem] = []
        for invoice in self._invoices_in(start, end):
            invoice_id = invoice.get("id")
            if not invoice_id:
                continue
            detail = self._get(f"/api/atlas/v2/orgs/{self._org}/invoices/{invoice_id}")
            items.extend(_parse_atlas_items(detail, self.tag))
        return items

    def _invoices_in(self, start: dt.date, end: dt.date) -> Iterable[dict]:
        page = 1
        for _ in range(50):
            data = self._get(
                f"/api/atlas/v2/orgs/{self._org}/invoices",
                {"pageNum": page, "itemsPerPage": 100},
            )
            batch = data.get("results") or []
            if not batch:
                return
            for invoice in batch:
                day = _iso_day(invoice.get("startDate"))
                if day is not None and start <= day < end:
                    yield invoice
            if len(batch) < 100:
                return
            page += 1


def _parse_atlas_items(detail: dict, tag_key: str) -> list[CloudCostItem]:
    out: list[CloudCostItem] = []
    for row in detail.get("lineItems") or []:
        if not isinstance(row, dict):
            continue
        cents = _to_decimal(row.get("totalPriceCents"))
        if cents is None or cents == 0:
            continue
        day = _iso_day(row.get("startDate"))
        if day is None:
            continue
        project = row.get("groupName") or row.get("groupId") or None
        sku = str(row.get("sku") or "")
        out.append(
            CloudCostItem(
                period=day,
                amount=cents / _CENTS,
                currency=str(detail.get("currency") or "USD").upper(),
                # The SKU is Atlas's service name, e.g. "ATLAS_AWS_INSTANCE_M30".
                service=sku,
                usage_type=row.get("clusterName") or None,
                operation=row.get("note") or None,
                account_id=project,
                region=row.get("region") or None,
                tag_key=tag_key,
                tag_value=project,
                dimensions={
                    "sku": sku,
                    "clusterName": row.get("clusterName"),
                    "groupName": row.get("groupName"),
                    "groupId": row.get("groupId"),
                    "unit": row.get("unit"),
                    f"TAG:{tag_key}": project,
                },
            )
        )
    return out


# ---------------------------------------------------------------------------
# Cloudflare — account subscriptions
# ---------------------------------------------------------------------------
# Cloudflare does not publish per-resource cost. What it publishes is what you
# are subscribed to and what each subscription costs per period, which for a
# Cloudflare customer IS the bill: a zone plan, Workers Paid, R2, Images.
#
# The consequence, and the setup guide says so plainly: a subscription describes
# its CURRENT period. There is no historical series to backfill, so a first sync
# records this period only and history accumulates from the nightly run onward.
_CF_API = "https://api.cloudflare.com/client/v4"
#: Cloudflare bills these per period; the period start is the line item's date.
_CF_FREQUENCY_MONTHS = {"weekly": 0, "monthly": 1, "quarterly": 3, "yearly": 12}


class CloudflareCostClient(_BaseCostClient):
    """Read-only subscription reader for Cloudflare.

    JSON cred: ``{"api_token":…, "account_id":…}``. The token needs only
    Billing:Read (Account) — it cannot read traffic, logs, or zone content.
    """

    base_url = _CF_API

    def __init__(self, admin_key: str, **kwargs):
        super().__init__(admin_key, **kwargs)
        c = _json_cred(admin_key, '{"api_token":…, "account_id":…}')
        self._token = c.get("api_token") or c.get("token")
        self._account = c.get("account_id") or c.get("account")
        if not self._token or not self._account:
            raise ProviderError(
                "Cloudflare credentials need api_token and account_id (an API token "
                "with the Billing:Read permission)."
            )
        self.tag = (c.get("tag") or "product").strip() or "product"
        self.metric = "subscription"
        self.granularity = "MONTHLY"

    def fetch_items(self, start: dt.date, end: dt.date) -> list[CloudCostItem]:
        resp = self._client.get(
            f"{self._base}/accounts/{self._account}/subscriptions",
            headers={"Authorization": f"Bearer {self._token}"},
        )
        if resp.status_code in (401, 403):
            raise ProviderError(
                "Cloudflare rejected the token. It needs the Billing:Read permission "
                "on this account.",
                401,
            )
        body = resp.json() if resp.content else {}
        if resp.status_code >= 400 or not body.get("success", True):
            message = _cf_error(body) or f"HTTP {resp.status_code}"
            # 9106 is Cloudflare's authentication failure, whatever the status.
            status = 401 if "authentication" in message.lower() else resp.status_code
            raise ProviderError(f"Cloudflare error: {message}", status)
        return _parse_cf_subscriptions(body, start, end, self.tag)


def _cf_error(body: dict) -> Optional[str]:
    errors = body.get("errors") or []
    if errors and isinstance(errors[0], dict):
        return str(errors[0].get("message") or "")
    return None


def _parse_cf_subscriptions(
    body: dict, start: dt.date, end: dt.date, tag_key: str
) -> list[CloudCostItem]:
    out: list[CloudCostItem] = []
    for sub in body.get("result") or []:
        if not isinstance(sub, dict):
            continue
        amount = _to_decimal(sub.get("price"))
        if amount is None or amount == 0:
            continue
        # The period this subscription's price covers. Outside our window it is
        # not our spend to record.
        day = _iso_day(sub.get("current_period_start")) or _iso_day(sub.get("created_on"))
        if day is None or not (start <= day < end):
            continue
        rate_plan = sub.get("rate_plan") or {}
        product = (
            (sub.get("product") or {}).get("name")
            or rate_plan.get("public_name")
            or rate_plan.get("id")
            or "Cloudflare subscription"
        )
        zone = (sub.get("zone") or {}).get("name")
        out.append(
            CloudCostItem(
                period=day,
                amount=amount,
                currency=str(rate_plan.get("currency") or sub.get("currency") or "USD").upper(),
                service=str(product),
                usage_type=str(sub.get("frequency") or "") or None,
                operation=rate_plan.get("id"),
                account_id=zone,
                tag_key=tag_key,
                tag_value=zone or str(product),
                dimensions={
                    "product": product,
                    "rate_plan": rate_plan.get("id"),
                    "frequency": sub.get("frequency"),
                    "zone": zone,
                    "state": sub.get("state"),
                    f"TAG:{tag_key}": zone or str(product),
                },
            )
        )
    return out


# ---------------------------------------------------------------------------
# Snowflake — ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY
# ---------------------------------------------------------------------------
# Snowflake meters in CREDITS, and credits are not dollars — their rate depends
# on edition, region and contract. The one place Snowflake reports actual
# currency is ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY, so that is what this
# reads, over the SQL REST API with key-pair JWT auth (same reason as BigQuery:
# no vendor SDK, and `cryptography` is already a dependency).
#
# The view is ORGADMIN-only. That is Snowflake's design, not ours, and the setup
# guide says so — the alternative view (METERING_DAILY_HISTORY) reports credits,
# which we would have to price ourselves, and a modelled dollar is not a bill.
_SF_STATEMENTS = "/api/v2/statements"
_SF_JWT_TYPE = "KEYPAIR_JWT"
_SF_QUERY = """
SELECT TO_VARCHAR(USAGE_DATE) AS USAGE_DATE,
       ACCOUNT_NAME,
       SERVICE_TYPE,
       CURRENCY,
       SUM(USAGE_IN_CURRENCY) AS USAGE_IN_CURRENCY
FROM SNOWFLAKE.ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY
WHERE USAGE_DATE >= ? AND USAGE_DATE < ?
GROUP BY 1, 2, 3, 4
""".strip()


class SnowflakeCostClient(_BaseCostClient):
    """Read-only spend reader for Snowflake, over the SQL API.

    JSON cred: ``{"account":…, "user":…, "private_key":…, "org_id"?}``. The user
    needs the ORGADMIN role to read organisation usage in currency.
    """

    def __init__(self, admin_key: str, **kwargs):
        c = _json_cred(admin_key, '{"account":…, "user":…, "private_key":…}')
        account = str(c.get("account") or "").strip()
        self._user = str(c.get("user") or "").strip().upper()
        self._private_key = c.get("private_key")
        if not account or not self._user or not self._private_key:
            raise ProviderError(
                "Snowflake credentials need account, user and private_key (a key-pair "
                "user with the ORGADMIN role)."
            )
        # The account identifier goes in the hostname, so it is validated rather
        # than trusted: it is the one part of the URL the credential controls.
        if not _SF_ACCOUNT.match(account):
            raise ProviderError(
                "Snowflake `account` must be an account identifier such as "
                "ORGNAME-ACCOUNTNAME — letters, digits, dots, dashes and underscores."
            )
        self._account = account.upper()
        host = f"https://{account.lower().replace('_', '-')}.snowflakecomputing.com"
        super().__init__(admin_key, base_url=kwargs.pop("base_url", host), **kwargs)
        self._warehouse = c.get("warehouse")
        self.tag = (c.get("tag") or "account").strip() or "account"
        self.metric = "usage_in_currency"
        self.granularity = "DAILY"

    def _jwt(self, now: int) -> str:
        """Snowflake's key-pair assertion: issuer carries the key fingerprint."""
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        try:
            key = serialization.load_pem_private_key(self._private_key.encode(), password=None)
        except Exception as exc:
            raise ProviderError(
                "The Snowflake private key could not be read. Paste the PEM exactly as "
                "generated, and use an unencrypted key.",
                401,
            ) from exc
        der = key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        # Snowflake identifies the key by the SHA-256 of its DER public key,
        # base64-encoded — the same value SHOW USERS reports as RSA_PUBLIC_KEY_FP.
        fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(der).digest()).decode()
        qualified = f"{self._account}.{self._user}"
        claims = {
            "iss": f"{qualified}.{fingerprint}",
            "sub": qualified,
            "iat": now,
            "exp": now + 3600,
        }
        signing_input = (
            _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
            + "."
            + _b64url(json.dumps(claims, separators=(",", ":")).encode())
        ).encode()
        signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        return signing_input.decode() + "." + _b64url(signature)

    def fetch_items(self, start: dt.date, end: dt.date) -> list[CloudCostItem]:
        token = self._jwt(int(time.time()))
        body = {
            "statement": _SF_QUERY,
            "timeout": 120,
            "bindings": {
                "1": {"type": "TEXT", "value": start.isoformat()},
                "2": {"type": "TEXT", "value": end.isoformat()},
            },
        }
        if self._warehouse:
            body["warehouse"] = self._warehouse
        resp = self._client.post(
            f"{self._base}{_SF_STATEMENTS}",
            json=body,
            headers={
                "Authorization": f"Bearer {token}",
                "X-Snowflake-Authorization-Token-Type": _SF_JWT_TYPE,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        if resp.status_code in (401, 403):
            raise ProviderError(
                "Snowflake rejected the key-pair credentials. Check the account "
                "identifier and user, that the public key is registered on that user, "
                "and that it has the ORGADMIN role.",
                401,
            )
        if resp.status_code == 404:
            # An unknown account identifier resolves to a Snowflake wildcard host
            # that serves an HTML "File not Found" page. Left alone that reaches
            # the customer as a wall of markup for what is a one-word typo.
            raise ProviderError(
                f"Snowflake has no account at {self._host_label()}. Check the account "
                "identifier — it looks like ORGNAME-ACCOUNTNAME, under Admin → Accounts.",
                404,
            )
        if resp.status_code >= 400:
            raise ProviderError(
                f"Snowflake error {resp.status_code}: {_short_text(resp)}", resp.status_code
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            # A 200 that is not JSON is not a bill; reporting it as an empty one
            # would look exactly like "you spent nothing".
            raise ProviderError(
                "Snowflake returned an unexpected response instead of query results.", 502
            ) from exc
        return _parse_snowflake(payload, self.tag)

    def _host_label(self) -> str:
        return self._base.replace("https://", "")


def _parse_snowflake(payload: dict, tag_key: str) -> list[CloudCostItem]:
    """Rows from the SQL API, which returns every value as a string."""
    meta = payload.get("resultSetMetaData") or {}
    names = [str((c or {}).get("name", "")).upper() for c in (meta.get("rowType") or [])]
    out: list[CloudCostItem] = []
    for row in payload.get("data") or []:
        if not isinstance(row, list):
            continue
        values = {name: (row[i] if i < len(row) else None) for i, name in enumerate(names)}
        amount = _to_decimal(values.get("USAGE_IN_CURRENCY"))
        if amount is None or amount == 0:
            continue
        day = _iso_day(values.get("USAGE_DATE"))
        if day is None:
            continue
        account = values.get("ACCOUNT_NAME") or None
        service = str(values.get("SERVICE_TYPE") or "")
        out.append(
            CloudCostItem(
                period=day,
                amount=amount,
                currency=str(values.get("CURRENCY") or "USD").upper(),
                service=service,
                usage_type=service,
                account_id=account,
                tag_key=tag_key,
                tag_value=account,
                dimensions={
                    "SERVICE_TYPE": service,
                    "ACCOUNT_NAME": account,
                    f"TAG:{tag_key}": account,
                },
            )
        )
    return out


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
#: A Snowflake account identifier: ORGNAME-ACCOUNTNAME, or a legacy locator.
_SF_ACCOUNT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _short_text(resp) -> str:
    """A safe, short error body. HTML error pages are summarised, not echoed:
    a wall of markup in a connector's error message helps nobody."""
    body = (resp.text or "").strip()
    if body[:1] == "<" or "text/html" in resp.headers.get("content-type", ""):
        return f"({len(body)} bytes of HTML)"
    return body[:200]


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _iso_day(value) -> Optional[dt.date]:
    """The date part of an ISO timestamp or date string, or None."""
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None
