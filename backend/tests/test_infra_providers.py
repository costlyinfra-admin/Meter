"""Platform billing connectors: DigitalOcean, MongoDB Atlas, Cloudflare, Snowflake.

Each is checked for the two things that make a cost connector trustworthy: it
asks the vendor for the right window, and it never invents a number the vendor
did not give it.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
from decimal import Decimal

import httpx
import pytest
from meter.infra_providers import (
    CloudflareCostClient,
    DigitalOceanCostClient,
    MongoAtlasCostClient,
    SnowflakeCostClient,
)
from meter.providers import ProviderError

WINDOW = (dt.date(2026, 5, 1), dt.date(2026, 6, 1))


def _client(cls, cred, handler):
    return cls(json.dumps(cred), client=httpx.Client(transport=httpx.MockTransport(handler)))


# ---------------------------------------------------------------------------
# DigitalOcean
# ---------------------------------------------------------------------------
_DO_CRED = {"token": "dop_v1_notarealtoken"}


def _do(handler, **over):
    return _client(DigitalOceanCostClient, {**_DO_CRED, **over}, handler)


def test_digitalocean_reads_invoice_items_and_attributes_by_project():
    def handler(request):
        assert request.headers["Authorization"] == f"Bearer {_DO_CRED['token']}"
        if "/invoices/" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "invoice_items": [
                        {
                            "product": "Droplets",
                            "description": "s-2vcpu-4gb",
                            "group_description": "web",
                            "amount": "48.00",
                            "project_name": "prod",
                            "start_time": "2026-05-01T00:00:00Z",
                        },
                        {
                            "product": "Brand New DO Thing",
                            "amount": "3.00",
                            "start_time": "2026-05-02T00:00:00Z",
                        },
                        # Zero-dollar rows carry no information.
                        {"product": "Free", "amount": "0", "start_time": "2026-05-01T00:00:00Z"},
                    ]
                },
            )
        return httpx.Response(
            200, json={"invoices": [{"invoice_uuid": "u1", "invoice_period": "2026-05"}]}
        )

    items = _do(handler).fetch_items(*WINDOW)

    assert [(i.service, i.amount, i.tag_value) for i in items] == [
        ("Droplets", Decimal("48.00"), "prod"),
        # An unfamiliar product is kept; an untagged one is Unattributed.
        ("Brand New DO Thing", Decimal("3.00"), None),
    ]
    assert items[0].period == dt.date(2026, 5, 1)
    assert items[0].usage_type == "s-2vcpu-4gb"


def test_digitalocean_only_fetches_invoices_inside_the_window():
    fetched = []

    def handler(request):
        if "/invoices/" in str(request.url):
            fetched.append(str(request.url).rsplit("/", 1)[-1].split("?")[0])
            return httpx.Response(200, json={"invoice_items": []})
        return httpx.Response(
            200,
            json={
                "invoices": [
                    {"invoice_uuid": "old", "invoice_period": "2025-01"},
                    {"invoice_uuid": "wanted", "invoice_period": "2026-05"},
                    {"invoice_uuid": "future", "invoice_period": "2027-01"},
                ]
            },
        )

    _do(handler).fetch_items(*WINDOW)
    # Each invoice detail is a separate billed request; fetching a year of them
    # to throw them away would be slow and rude.
    assert fetched == ["wanted"]


def test_digitalocean_requires_a_token():
    with pytest.raises(ProviderError, match="read-only API token"):
        DigitalOceanCostClient("{}")


def test_digitalocean_rejection_says_what_the_token_needs():
    def handler(_request):
        return httpx.Response(401, json={"id": "Unauthorized"})

    with pytest.raises(ProviderError) as exc:
        _do(handler).fetch_items(*WINDOW)
    assert "read scope" in str(exc.value)
    assert exc.value.status == 401
    assert _DO_CRED["token"] not in str(exc.value)


# ---------------------------------------------------------------------------
# MongoDB Atlas
# ---------------------------------------------------------------------------
_ATLAS_PRIVATE = "9f3c1e77-aaaa-bbbb-cccc-0123456789ab"
_ATLAS_CRED = {"public_key": "abcdefgh", "private_key": _ATLAS_PRIVATE, "org_id": "org1"}


def _atlas(handler, **over):
    return _client(MongoAtlasCostClient, {**_ATLAS_CRED, **over}, handler)


def _atlas_handler(line_items, invoices=None):
    def handler(request):
        assert request.headers["Accept"].startswith("application/vnd.atlas")
        if request.url.path.endswith("/invoices"):
            return httpx.Response(
                200,
                json={"results": invoices or [{"id": "inv1", "startDate": "2026-05-01T00:00:00Z"}]},
            )
        return httpx.Response(200, json={"currency": "USD", "lineItems": line_items})

    return handler


def test_atlas_converts_cents_to_dollars_and_keeps_the_cluster():
    handler = _atlas_handler(
        [
            {
                "sku": "ATLAS_AWS_INSTANCE_M30",
                "clusterName": "prod-0",
                "groupName": "Search",
                "region": "US_EAST_1",
                "startDate": "2026-05-04T00:00:00Z",
                "totalPriceCents": 123456,
            },
            {"sku": "ATLAS_BACKUP", "startDate": "2026-05-04T00:00:00Z", "totalPriceCents": 0},
        ]
    )
    (item,) = _atlas(handler).fetch_items(*WINDOW)

    # Atlas bills in cents. Reporting 123456 dollars would be off by 100x.
    assert item.amount == Decimal("1234.56")
    assert (item.service, item.usage_type, item.tag_value) == (
        "ATLAS_AWS_INSTANCE_M30",
        "prod-0",
        "Search",
    )
    assert item.region == "US_EAST_1"
    assert item.period == dt.date(2026, 5, 4)


def test_atlas_skips_invoices_outside_the_window():
    fetched = []

    def handler(request):
        if request.url.path.endswith("/invoices"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"id": "old", "startDate": "2025-01-01T00:00:00Z"},
                        {"id": "wanted", "startDate": "2026-05-01T00:00:00Z"},
                    ]
                },
            )
        fetched.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, json={"lineItems": []})

    _atlas(handler).fetch_items(*WINDOW)
    assert fetched == ["wanted"]


@pytest.mark.parametrize("missing", ["public_key", "private_key", "org_id"])
def test_atlas_configuration_is_validated_before_any_call(missing):
    cred = {k: v for k, v in _ATLAS_CRED.items() if k != missing}
    with pytest.raises(ProviderError, match=missing):
        MongoAtlasCostClient(json.dumps(cred))


def test_atlas_rejection_names_the_role_and_leaks_no_key():
    def handler(_request):
        return httpx.Response(401, json={"error": 401})

    with pytest.raises(ProviderError) as exc:
        _atlas(handler).fetch_items(*WINDOW)
    assert "Organization Billing Viewer" in str(exc.value)
    assert _ATLAS_PRIVATE not in str(exc.value)


# ---------------------------------------------------------------------------
# Cloudflare
# ---------------------------------------------------------------------------
_CF_TOKEN = "cf_token_Xq7aB9cDz2EfGh4IjKlMnOpQrStUv"
_CF_CRED = {"api_token": _CF_TOKEN, "account_id": "acct-1"}


def _cf(handler, **over):
    return _client(CloudflareCostClient, {**_CF_CRED, **over}, handler)


def test_cloudflare_reads_subscription_prices_for_the_current_period():
    def handler(request):
        assert request.headers["Authorization"] == f"Bearer {_CF_TOKEN}"
        assert "/accounts/acct-1/subscriptions" in str(request.url)
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": [
                    {
                        "id": "s1",
                        "price": 20,
                        "frequency": "monthly",
                        "current_period_start": "2026-05-01T00:00:00Z",
                        "rate_plan": {
                            "id": "PARTNERS_PRO",
                            "public_name": "Pro Plan",
                            "currency": "USD",
                        },
                        "zone": {"name": "acme.com"},
                    },
                    {
                        "id": "s2",
                        "price": 5,
                        "current_period_start": "2026-05-01T00:00:00Z",
                        "product": {"name": "Workers AI"},
                        "rate_plan": {"id": "workers_ai", "currency": "USD"},
                    },
                ],
            },
        )

    items = _cf(handler).fetch_items(*WINDOW)
    assert [(i.service, i.amount, i.tag_value) for i in items] == [
        ("Pro Plan", Decimal("20"), "acme.com"),
        ("Workers AI", Decimal("5"), "Workers AI"),
    ]


def test_cloudflare_does_not_backdate_a_subscription_into_our_window():
    # A subscription describes its CURRENT period. Recording its price against a
    # month it did not cover would be inventing history.
    def handler(_request):
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": [
                    {
                        "id": "s1",
                        "price": 25,
                        "current_period_start": "2020-01-01T00:00:00Z",
                        "rate_plan": {"id": "OLD"},
                    }
                ],
            },
        )

    assert _cf(handler).fetch_items(*WINDOW) == []


def test_cloudflare_surfaces_its_own_error_envelope():
    # Cloudflare answers auth failures with HTTP 200 and success:false, so a
    # status-only check would read a failure as an empty bill.
    def handler(_request):
        return httpx.Response(
            200,
            json={
                "success": False,
                "errors": [{"code": 9106, "message": "Authentication failed (status: 400)"}],
            },
        )

    with pytest.raises(ProviderError) as exc:
        _cf(handler).fetch_items(*WINDOW)
    assert "Authentication failed" in str(exc.value)
    assert exc.value.status == 401  # so the API answers 400, not 502


def test_cloudflare_requires_a_token_and_an_account():
    with pytest.raises(ProviderError, match="api_token and account_id"):
        CloudflareCostClient(json.dumps({"api_token": "x"}))


def test_cloudflare_rejection_leaks_no_token():
    def handler(_request):
        return httpx.Response(403, json={"success": False, "errors": [{"message": "denied"}]})

    with pytest.raises(ProviderError) as exc:
        _cf(handler).fetch_items(*WINDOW)
    assert "Billing:Read" in str(exc.value)
    assert _CF_TOKEN not in str(exc.value)


# ---------------------------------------------------------------------------
# Snowflake
# ---------------------------------------------------------------------------
_SF_KEY = None


def _sf_keypair():
    global _SF_KEY
    if _SF_KEY is None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        _SF_KEY = (key, pem)
    return _SF_KEY


def _sf_cred(**over):
    _, pem = _sf_keypair()
    return {"account": "MYORG-ACME", "user": "meter_svc", "private_key": pem, **over}


def _sf(handler, **over):
    return _client(SnowflakeCostClient, _sf_cred(**over), handler)


def _sf_rows(rows):
    return {
        "resultSetMetaData": {
            "rowType": [
                {"name": "USAGE_DATE"},
                {"name": "ACCOUNT_NAME"},
                {"name": "SERVICE_TYPE"},
                {"name": "CURRENCY"},
                {"name": "USAGE_IN_CURRENCY"},
            ]
        },
        "data": rows,
    }


def test_snowflake_signs_an_assertion_snowflake_would_accept():
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    key, _ = _sf_keypair()
    seen = {}

    def handler(request):
        assert request.headers["X-Snowflake-Authorization-Token-Type"] == "KEYPAIR_JWT"
        seen["host"] = request.url.host
        seen["jwt"] = request.headers["Authorization"].split(" ", 1)[1]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_sf_rows([]))

    _sf(handler).fetch_items(*WINDOW)

    # The account identifier becomes the hostname.
    assert seen["host"] == "myorg-acme.snowflakecomputing.com"
    header_b64, claims_b64, sig_b64 = seen["jwt"].split(".")

    def unpad(s):
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

    key.public_key().verify(
        unpad(sig_b64), f"{header_b64}.{claims_b64}".encode(), padding.PKCS1v15(), hashes.SHA256()
    )
    claims = json.loads(unpad(claims_b64))
    # Snowflake matches the issuer's fingerprint against the key registered on
    # the user; a wrong one is rejected even though the signature is valid.
    der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    expected = "SHA256:" + base64.b64encode(hashlib.sha256(der).digest()).decode()
    assert claims["iss"] == f"MYORG-ACME.METER_SVC.{expected}"
    assert claims["sub"] == "MYORG-ACME.METER_SVC"
    # The window travels as bound values, not string-formatted into the SQL.
    assert seen["body"]["bindings"] == {
        "1": {"type": "TEXT", "value": "2026-05-01"},
        "2": {"type": "TEXT", "value": "2026-06-01"},
    }
    assert "USAGE_IN_CURRENCY_DAILY" in seen["body"]["statement"]


def test_snowflake_reads_currency_not_credits():
    def handler(_request):
        return httpx.Response(
            200,
            json=_sf_rows(
                [
                    ["2026-05-04", "PROD", "WAREHOUSE_METERING", "USD", "412.75"],
                    ["2026-05-04", "PROD", "AI_SERVICES", "USD", "96.20"],
                    ["2026-05-05", "PROD", "STORAGE", "USD", "0"],
                ]
            ),
        )

    items = _sf(handler).fetch_items(*WINDOW)
    assert [(i.service, i.amount) for i in items] == [
        ("WAREHOUSE_METERING", Decimal("412.75")),
        ("AI_SERVICES", Decimal("96.20")),
    ]
    assert items[0].period == dt.date(2026, 5, 4)
    assert items[0].tag_value == "PROD"


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"account": ""}, "account, user and private_key"),
        ({"user": ""}, "account, user and private_key"),
        # The account identifier becomes the hostname, so it is validated.
        ({"account": "evil.example.com/../"}, "account identifier"),
        ({"account": "a b"}, "account identifier"),
    ],
)
def test_snowflake_configuration_is_validated_before_any_call(overrides, message):
    with pytest.raises(ProviderError, match=message):
        SnowflakeCostClient(json.dumps(_sf_cred(**overrides)))


def test_snowflake_rejects_an_unreadable_key_as_a_credential_error():
    client = _sf(lambda r: httpx.Response(200, json=_sf_rows([])), private_key="not-a-pem")
    with pytest.raises(ProviderError) as exc:
        client.fetch_items(*WINDOW)
    assert "private key could not be read" in str(exc.value)
    assert exc.value.status == 401  # a 400 for the customer, not a 502


def test_snowflake_rejection_names_the_role():
    def handler(_request):
        return httpx.Response(401, json={"message": "nope"})

    with pytest.raises(ProviderError) as exc:
        _sf(handler).fetch_items(*WINDOW)
    assert "ORGADMIN" in str(exc.value)


def test_snowflake_names_the_host_when_the_account_does_not_exist():
    # A wrong account identifier resolves to a Snowflake wildcard host serving an
    # HTML error page. The customer should get their typo back, not markup.
    def handler(_request):
        return httpx.Response(
            404,
            headers={"content-type": "text/html"},
            text="<!DOCTYPE html><html><head><title>File not Found</title></head>" + "x" * 500,
        )

    with pytest.raises(ProviderError) as exc:
        _sf(handler).fetch_items(*WINDOW)

    message = str(exc.value)
    assert "myorg-acme.snowflakecomputing.com" in message
    assert "ORGNAME-ACCOUNTNAME" in message
    assert "<" not in message  # no markup reaches the customer


def test_snowflake_does_not_echo_an_html_error_body():
    def handler(_request):
        return httpx.Response(
            500, headers={"content-type": "text/html"}, text="<html>" + "x" * 5000 + "</html>"
        )

    with pytest.raises(ProviderError) as exc:
        _sf(handler).fetch_items(*WINDOW)
    assert "bytes of HTML" in str(exc.value)
    assert "xxxx" not in str(exc.value)


def test_snowflake_treats_a_non_json_success_as_an_error_not_an_empty_bill():
    # A 200 that is not query results must never look like "you spent nothing".
    def handler(_request):
        return httpx.Response(200, text="<html>maintenance</html>")

    with pytest.raises(ProviderError, match="unexpected response"):
        _sf(handler).fetch_items(*WINDOW)


# ---------------------------------------------------------------------------
# Vercel — billing charges in FOCUS 1.3
# ---------------------------------------------------------------------------
_V_TOKEN = "vercel_tok_Xq7aB9cDz2EfGh4IjKlMnOpQrStUv"
_V_CRED = {"token": _V_TOKEN, "team_id": "team_abc", "tag": "feature"}


def _v(handler, **over):
    from meter.infra_providers import VercelCloudCostClient

    return _client(VercelCloudCostClient, {**_V_CRED, **over}, handler)


def _charge(**over):
    row = {
        "ChargePeriodStart": "2026-05-04T00:00:00Z",
        "BilledCost": "142.50",
        "BillingCurrency": "USD",
        "ServiceName": "Edge Functions",
        "ChargeCategory": "Usage",
        "Tags": {"feature": "triage"},
    }
    row.update(over)
    return row


def test_vercel_reads_focus_charges_with_real_tags():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        assert request.headers["Authorization"] == f"Bearer {_V_TOKEN}"
        return httpx.Response(
            200,
            json={
                "charges": [
                    _charge(SkuId="edge-invocations", SubAccountName="acme-web", RegionId="iad1"),
                    _charge(ServiceName="Brand New Vercel Thing", BilledCost="88.00", Tags={}),
                ]
            },
        )

    items = _v(handler).fetch_items(*WINDOW)

    assert "teamId=team_abc" in seen["url"]
    assert [(i.service, i.amount, i.tag_value) for i in items] == [
        # FOCUS carries a Tags map, so this is a real cost-allocation tag —
        # not a project name standing in for one.
        ("Edge Functions", Decimal("142.50"), "triage"),
        ("Brand New Vercel Thing", Decimal("88.00"), None),
    ]
    assert (items[0].usage_type, items[0].account_id, items[0].region) == (
        "edge-invocations",
        "acme-web",
        "iad1",
    )


def test_vercel_keeps_credits_because_they_are_real_money():
    def handler(_request):
        return httpx.Response(
            200,
            json={
                "charges": [
                    _charge(),
                    _charge(ServiceName="Credit", BilledCost="-25.00", ChargeCategory="Credit"),
                    _charge(ServiceName="Free tier", BilledCost="0"),
                ]
            },
        )

    items = _v(handler).fetch_items(*WINDOW)
    # A credit reduces the bill. Dropping it would overstate spend.
    assert [i.amount for i in items] == [Decimal("142.50"), Decimal("-25.00")]
    assert items[1].dimensions["chargecategory"] == "Credit"


def test_vercel_enforces_the_window_on_our_side():
    # The query asks for a range, but the window is applied again after parsing:
    # a parameter-name change upstream must never silently widen what we record.
    def handler(_request):
        return httpx.Response(
            200,
            json={
                "charges": [
                    _charge(),
                    _charge(ChargePeriodStart="2020-01-01T00:00:00Z", ServiceName="Ancient"),
                ]
            },
        )

    items = _v(handler).fetch_items(*WINDOW)
    assert [i.service for i in items] == ["Edge Functions"]


def test_vercel_matches_focus_columns_case_insensitively():
    # FOCUS is PascalCase, but implementations differ on casing and none of them
    # differ on meaning.
    def handler(_request):
        return httpx.Response(
            200,
            json=[
                {
                    "chargePeriodStart": "2026-05-04",
                    "billedCost": "10.00",
                    "serviceName": "Blob",
                    "billingCurrency": "usd",
                    "tags": {"Feature": "reports"},
                }
            ],
        )

    (item,) = _v(handler).fetch_items(*WINDOW)
    assert (item.service, item.amount, item.currency, item.tag_value) == (
        "Blob",
        Decimal("10.00"),
        "USD",
        "reports",
    )


def test_vercel_follows_pagination_and_stops_on_a_repeated_cursor():
    pages = [
        {"charges": [_charge()], "pagination": {"next": "cur2"}},
        # A cursor that does not advance would otherwise loop to the hard cap.
        {
            "charges": [_charge(ServiceName="Blob", BilledCost="12.00")],
            "pagination": {"next": "cur2"},
        },
    ]
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=pages[min(len(calls) - 1, 1)])

    items = _v(handler).fetch_items(*WINDOW)
    assert len(calls) == 2
    assert [i.service for i in items] == ["Edge Functions", "Blob"]


def test_vercel_prefers_billed_cost_but_falls_back_rather_than_dropping():
    def handler(_request):
        return httpx.Response(
            200,
            json={
                "charges": [
                    {
                        "ChargePeriodStart": "2026-05-04",
                        "EffectiveCost": "7.00",
                        "ServiceName": "Blob",
                    }
                ]
            },
        )

    (item,) = _v(handler).fetch_items(*WINDOW)
    # BilledCost is absent; a real number is still a real number.
    assert item.amount == Decimal("7.00")


def test_vercel_treats_an_unrecognised_envelope_as_empty_not_invented():
    def handler(_request):
        return httpx.Response(200, json={"somethingElse": {"nested": [_charge()]}})

    # An envelope we do not recognise yields nothing, which shows up as an empty
    # sync — never as a number guessed out of an unfamiliar shape.
    assert _v(handler).fetch_items(*WINDOW) == []


def test_vercel_says_the_endpoint_needs_a_pro_or_enterprise_team():
    def handler(_request):
        return httpx.Response(403, json={"error": {"code": "forbidden"}})

    with pytest.raises(ProviderError) as exc:
        _v(handler).fetch_items(*WINDOW)
    assert "Pro and Enterprise" in str(exc.value)
    assert exc.value.status == 401
    assert _V_TOKEN not in str(exc.value)


@pytest.mark.parametrize(
    "overrides, message",
    [({"token": None}, "access token"), ({"metric": "MadeUpCost"}, "metric must be")],
)
def test_vercel_configuration_is_validated_before_any_call(overrides, message):
    from meter.infra_providers import VercelCloudCostClient

    cred = {k: v for k, v in {**_V_CRED, **overrides}.items() if v is not None}
    with pytest.raises(ProviderError, match=message):
        VercelCloudCostClient(json.dumps(cred))
