"""Provider cost clients — parsing + read-only behavior, via mock transport."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import httpx
import pytest
from meter.providers import (
    AnthropicCostClient,
    OpenAICostClient,
    ProviderError,
    aggregate,
    month_query_end,
    month_start,
    next_month,
)


def test_month_helpers():
    assert month_start(dt.date(2026, 5, 17)) == dt.date(2026, 5, 1)
    assert next_month(dt.date(2026, 12, 1)) == dt.date(2027, 1, 1)


def test_month_query_end_caps_current_month_to_date():
    today = dt.date(2026, 8, 20)
    # A fully-elapsed month queries the whole month (first of next month).
    assert month_query_end(dt.date(2026, 5, 1), today=today) == dt.date(2026, 6, 1)
    # The current (in-progress) month is capped at tomorrow — month-to-date, never
    # the future Sept 1 that made the current month import nothing.
    assert month_query_end(dt.date(2026, 8, 1), today=today) == dt.date(2026, 8, 21)


def test_anthropic_current_month_queries_month_to_date_not_future():
    # Simulate the real Cost Report: a FUTURE ending_at returns nothing; only a
    # month-to-date ending_at returns the current month's spend. This is exactly
    # why the current month previously imported no rows.
    today = dt.date.today()
    cap = (today + dt.timedelta(days=1)).isoformat()
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        end = request.url.params.get("ending_at")
        captured["ending_at"] = end
        if end > cap:  # future -> the real API yields no data
            return httpx.Response(200, json={"data": []})
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "results": [
                            {
                                "workspace_id": "ws_triage",
                                "description": "claude-sonnet-4-6",
                                "amount": "4200.00",  # cents -> $42.00
                                "currency": "USD",
                            }
                        ]
                    }
                ]
            },
        )

    client = AnthropicCostClient(
        "sk-ant-admin", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    records = client.fetch_costs(today)  # the CURRENT month
    # ending_at is capped to tomorrow, so month-to-date cost imports (not empty),
    # and it reconciles with the Cost Report's returned dollars.
    assert captured["ending_at"] == cap
    assert sum((r.amount for r in records), Decimal("0")) == Decimal("42.00")


def test_aggregate_sums_same_key_project_model():
    from meter.providers import CostRecord

    recs = [
        CostRecord("openai", dt.date(2026, 5, 1), Decimal("10"), project="p1", model="gpt-4o"),
        CostRecord("openai", dt.date(2026, 5, 1), Decimal("5"), project="p1", model="gpt-4o"),
        CostRecord("openai", dt.date(2026, 5, 1), Decimal("7"), project="p2", model="gpt-4o"),
    ]
    out = {(r.project): r.amount for r in aggregate(recs)}
    assert out["p1"] == Decimal("15")
    assert out["p2"] == Decimal("7")


def test_anthropic_parses_cost_report():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "sk-ant-admin"
        assert request.method == "GET"  # read-only
        # cost_report only supports workspace_id + description grouping (NOT model).
        assert request.url.params.get_list("group_by[]") == ["workspace_id", "description"]
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "results": [
                            {
                                "workspace_id": "ws_triage",
                                "description": "claude-sonnet-4-6",
                                "amount": "4200.00",
                                "currency": "USD",
                            },
                            {
                                "workspace_id": "ws_shared",
                                "description": "claude-haiku-4-5",
                                "amount": "980.00",
                            },
                        ]
                    }
                ]
            },
        )

    client = AnthropicCostClient(
        "sk-ant-admin", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    records = client.fetch_costs(dt.date(2026, 5, 10))
    by_ws = {r.project: r for r in records}
    # Anthropic reports cents: "4200.00" cents == $42.00, "980.00" cents == $9.80.
    assert by_ws["ws_triage"].amount == Decimal("42.00")
    assert by_ws["ws_shared"].amount == Decimal("9.80")
    assert by_ws["ws_triage"].period == dt.date(2026, 5, 1)
    # The description line-item survives as the model label.
    assert by_ws["ws_shared"].model == "claude-haiku-4-5"


def test_anthropic_cost_report_paginates_the_full_month():
    # Reproduces the reported failure: the Cost Report returns DAILY buckets across
    # multiple pages (default 7/page). Without an explicit limit + pagination, only
    # the first page (early month) is read, so late-month workspaces like
    # "automations" are dropped and the total is a fraction of the real bill.
    pages = {
        None: {
            "data": [
                {
                    "results": [
                        {
                            "workspace_id": "marketing-aeo",
                            "description": "claude",
                            "amount": "767.00",  # cents -> $7.67 (all early month)
                            "currency": "USD",
                        }
                    ]
                }
            ],
            "has_more": True,
            "next_page": "page2",
        },
        "page2": {
            "data": [
                {
                    "results": [
                        {
                            "workspace_id": "automations",
                            "description": "claude",
                            "amount": "64747.00",  # cents -> $647.47 (surges late month)
                            "currency": "USD",
                        }
                    ]
                }
            ],
            "has_more": False,
        },
    }
    seen_limits: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_limits.append(request.url.params.get("limit"))
        return httpx.Response(200, json=pages[request.url.params.get("page")])

    client = AnthropicCostClient(
        "sk-ant-admin", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    by_ws = {r.project: r.amount for r in client.fetch_costs(dt.date(2026, 8, 10))}
    # BOTH the early- and late-month workspaces are captured (not just page 1).
    assert by_ws["marketing-aeo"] == Decimal("7.67")
    assert by_ws["automations"] == Decimal("647.47")
    assert seen_limits == ["31", "31"]  # full-month buckets requested on every page


def test_openai_parses_costs():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer sk-openai-admin"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "results": [
                            {
                                "project_id": "proj_reports",
                                "line_item": "gpt-4o",
                                "amount": {"value": "1850.00", "currency": "USD"},
                            },
                        ]
                    }
                ]
            },
        )

    client = OpenAICostClient(
        "sk-openai-admin", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    records = client.fetch_costs(dt.date(2026, 5, 1))
    assert records[0].project == "proj_reports"
    assert records[0].amount == Decimal("1850.00")


def test_anthropic_usage_parses_cache_creation_by_ttl():
    # Anthropic reports cache WRITES as a nested object keyed by cache TTL (they
    # price differently: 5m at 1.25x input, 1h at 2x). A flat-field-only parser
    # missed them entirely, so cache writes never showed up.
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "starting_at": "2026-08-04T00:00:00Z",
                        "results": [
                            {
                                "workspace_id": "ws",
                                "api_key_id": "k",
                                "model": "claude-sonnet-4-6",
                                "uncached_input_tokens": 1000,
                                "cache_creation": {
                                    "ephemeral_5m_input_tokens": 400,
                                    "ephemeral_1h_input_tokens": 100,
                                },
                                "cache_read_input_tokens": 250,
                                "output_tokens": 300,
                            }
                        ],
                    }
                ]
            },
        )

    client = AnthropicCostClient(
        "sk-ant-admin", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    (u,) = client.fetch_usage(dt.date(2026, 8, 1))
    assert (u.cache_write_5m, u.cache_write_1h) == (400, 100)
    assert u.cache_write_tokens == 500  # the total
    # Total input = uncached + both cache writes + cache read.
    assert u.tokens_in == 1000 + 400 + 100 + 250
    assert u.cached_tokens_in == 250


def test_anthropic_usage_accepts_a_flat_cache_creation_field():
    # Simpler/older shape: one flat field, treated as the default 5-minute TTL.
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "results": [
                            {
                                "workspace_id": "ws",
                                "cache_creation_input_tokens": 700,
                                "cache_read_input_tokens": 0,
                                "uncached_input_tokens": 300,
                                "output_tokens": 50,
                            }
                        ]
                    }
                ]
            },
        )

    client = AnthropicCostClient(
        "sk-ant-admin", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    (u,) = client.fetch_usage(dt.date(2026, 8, 1))
    assert (u.cache_write_5m, u.cache_write_1h) == (700, 0)
    assert u.cache_write_tokens == 700


def test_anthropic_cost_report_stamps_the_bucket_day():
    # Each daily bucket's date is captured, so cost can be persisted per day.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "starting_at": "2026-05-04T00:00:00Z",
                        "results": [
                            {"workspace_id": "ws", "description": "c", "amount": "1000.00"}
                        ],
                    },
                    {
                        "starting_at": "2026-05-05T00:00:00Z",
                        "results": [{"workspace_id": "ws", "description": "c", "amount": "500.00"}],
                    },
                ]
            },
        )

    client = AnthropicCostClient(
        "sk-ant-admin", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    by_day = {r.period: r.amount for r in client.fetch_costs(dt.date(2026, 5, 1))}
    assert by_day[dt.date(2026, 5, 4)] == Decimal("10.00")  # cents -> dollars, per day
    assert by_day[dt.date(2026, 5, 5)] == Decimal("5.00")


def test_openai_costs_paginate_the_full_month():
    # Same class of bug as Anthropic: the Costs API paginates daily buckets, so a
    # long month must be paged through rather than truncated to the first page.
    pages = {
        None: {
            "data": [
                {
                    "results": [
                        {
                            "project_id": "proj_a",
                            "line_item": "gpt-4o",
                            "amount": {"value": "100.00", "currency": "USD"},
                        }
                    ]
                }
            ],
            "has_more": True,
            "next_page": "p2",
        },
        "p2": {
            "data": [
                {
                    "results": [
                        {
                            "project_id": "proj_b",
                            "line_item": "gpt-4o",
                            "amount": {"value": "250.00", "currency": "USD"},
                        }
                    ]
                }
            ],
            "has_more": False,
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[request.url.params.get("page")])

    client = OpenAICostClient(
        "sk-openai-admin", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    by_proj = {r.project: r.amount for r in client.fetch_costs(dt.date(2026, 5, 1))}
    assert by_proj["proj_a"] == Decimal("100.00")
    assert by_proj["proj_b"] == Decimal("250.00")  # page 2 not dropped


def test_anthropic_amount_is_cents_converted_to_dollars():
    # Anthropic's cost_report `amount` is in the currency's lowest unit (cents) as a
    # decimal string. Per the Cost API contract, "123.45" USD represents $1.2345 and
    # a raw "28886" is $288.86 — NOT $28,886.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "results": [
                            {"workspace_id": "ws_doc", "amount": "123.45", "currency": "USD"},
                            {"workspace_id": "ws_client", "amount": "28886", "currency": "USD"},
                        ]
                    }
                ]
            },
        )

    client = AnthropicCostClient(
        "sk-ant-admin", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    by_ws = {r.project: r.amount for r in client.fetch_costs(dt.date(2026, 5, 1))}
    assert by_ws["ws_doc"] == Decimal("1.2345")  # documented example
    assert by_ws["ws_client"] == Decimal("288.86")  # 28886 cents, not $28,886


def test_anthropic_captures_cache_read_tokens():
    # When the report includes usage, cache_read_input_tokens is captured (§8).
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "results": [
                            {
                                "workspace_id": "ws_triage",
                                "model": "claude-sonnet-4-6",
                                "amount": "4200.00",
                                "input_tokens": 10_000_000,
                                "output_tokens": 500_000,
                                "cache_read_input_tokens": 800_000,
                            }
                        ]
                    }
                ]
            },
        )

    client = AnthropicCostClient(
        "sk-ant-admin", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    rec = client.fetch_costs(dt.date(2026, 5, 1))[0]
    assert rec.tokens_in == 10_000_000
    assert rec.cached_tokens_in == 800_000


def test_openai_captures_cached_tokens():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "results": [
                            {
                                "project_id": "proj_reports",
                                "line_item": "gpt-4o",
                                "amount": {"value": "1850.00", "currency": "USD"},
                                "input_tokens": 6_000_000,
                                "input_tokens_details": {"cached_tokens": 480_000},
                            }
                        ]
                    }
                ]
            },
        )

    client = OpenAICostClient(
        "sk-openai-admin", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    rec = client.fetch_costs(dt.date(2026, 5, 1))[0]
    assert rec.tokens_in == 6_000_000
    assert rec.cached_tokens_in == 480_000


def test_google_gemini_parses_cost_by_project():
    from meter.providers import GoogleCostClient

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer goog-token"
        assert request.method == "GET"  # read-only
        return httpx.Response(
            200,
            json={
                "data": [
                    {"project_id": "proj_triage", "model": "gemini-2.5-pro", "cost": "300.00"},
                    # No cost -> price the tokens (flash: $0.30/M in).
                    {
                        "project_id": "proj_reports",
                        "model": "gemini-2.5-flash",
                        "prompt_tokens": 1_000_000,
                        "completion_tokens": 0,
                    },
                ]
            },
        )

    client = GoogleCostClient(
        "goog-token", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    by_project = {r.project: r for r in client.fetch_costs(dt.date(2026, 5, 1))}
    assert by_project["proj_triage"].amount == Decimal("300.00")  # reported $
    assert by_project["proj_reports"].amount == Decimal("0.3000")  # priced by us


def test_hosted_oss_uses_reported_dollar_cost():
    from meter.providers import make_cost_client

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer or-key"
        assert request.method == "GET"  # read-only
        assert "openrouter.ai" in str(request.url)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "model": "meta-llama-3.1-70b-instruct",
                        "cost": "123.45",
                        "requests": 5000,
                        "api_key": "key:prod",
                    }
                ]
            },
        )

    client = make_cost_client("openrouter", "or-key")
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    records = client.fetch_costs(dt.date(2026, 5, 10))
    assert records[0].amount == Decimal("123.45")  # reported $ wins
    assert records[0].model == "meta-llama-3.1-70b-instruct"
    assert records[0].request_count == 5000


def test_hosted_oss_prices_tokens_when_no_dollar_cost():
    from meter.providers import HostedUsageCostClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                # No cost field -> price the tokens via our (provider, model) table.
                # together meta-llama-3.1-70b-instruct = $0.88/M in + out.
                "data": [
                    {
                        "model": "meta-llama-3.1-70b-instruct",
                        "prompt_tokens": 1_000_000,
                        "completion_tokens": 1_000_000,
                    }
                ]
            },
        )

    client = HostedUsageCostClient(
        "together",
        "tg-key",
        "/v1/usage",
        base_url="https://api.together.xyz",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    records = client.fetch_costs(dt.date(2026, 5, 1))
    assert records[0].amount == Decimal("1.7600")  # 0.88 + 0.88, computed by us


def test_bedrock_reads_cost_explorer_by_tag():
    import json

    from meter.providers import BedrockCostClient

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"  # Cost Explorer is a POST API
        assert "ce.us-east-1.amazonaws.com" in str(request.url)
        assert request.headers["Authorization"].startswith("AWS4-HMAC-SHA256")
        assert request.headers["X-Amz-Target"].endswith("GetCostAndUsage")
        return httpx.Response(
            200,
            json={
                "ResultsByTime": [
                    {
                        "Groups": [
                            {
                                "Keys": ["feature$triage"],
                                "Metrics": {"UnblendedCost": {"Amount": "4200.00", "Unit": "USD"}},
                            },
                            {  # untagged Bedrock spend -> Unattributed
                                "Keys": ["feature$"],
                                "Metrics": {"UnblendedCost": {"Amount": "300.00", "Unit": "USD"}},
                            },
                        ]
                    }
                ]
            },
        )

    creds = json.dumps(
        {
            "access_key_id": "AKIA",
            "secret_access_key": "secret",
            "region": "us-east-1",
            "tag": "feature",
        }
    )
    client = BedrockCostClient(creds, client=httpx.Client(transport=httpx.MockTransport(handler)))
    records = client.fetch_costs(dt.date(2026, 5, 1))
    by_tag = {r.api_key_ref: r for r in records}
    assert by_tag["triage"].amount == Decimal("4200.00")
    assert by_tag["triage"].provider == "bedrock"
    assert by_tag[None].amount == Decimal("300.00")  # untagged -> no key -> Unattributed


def test_bedrock_requires_json_credentials():
    from meter.providers import BedrockCostClient, ProviderError

    with pytest.raises(ProviderError):
        BedrockCostClient("not-json")


def test_provider_401_raises():
    def handler(_request):
        return httpx.Response(401, json={"error": "bad key"})

    client = AnthropicCostClient("bad", client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ProviderError) as exc:
        client.fetch_costs(dt.date(2026, 5, 1))
    assert exc.value.status == 401


def test_litellm_reads_spend_report():
    import json

    from meter.providers import make_cost_client

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"  # read-only
        assert "litellm.acme.com/global/spend/report" in str(request.url)
        assert request.headers["Authorization"] == "Bearer sk-master"
        return httpx.Response(
            200,
            json=[
                {"api_key": "key:prod", "model": "gpt-4o", "spend": "120.50"},
                {"api_key": "key:dev", "model": "claude-sonnet-4-6", "spend": "9.50"},
            ],
        )

    client = make_cost_client(
        "litellm", json.dumps({"base_url": "https://litellm.acme.com", "master_key": "sk-master"})
    )
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    records = client.fetch_costs(dt.date(2026, 5, 10))
    by_key = {r.api_key_ref: r.amount for r in records}
    assert by_key["key:prod"] == Decimal("120.50")
    assert by_key["key:dev"] == Decimal("9.50")


def test_elevenlabs_prices_character_usage():
    from meter.providers import make_cost_client

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["xi-api-key"] == "xi-key"
        return httpx.Response(200, json={"time": [1], "usage": {"All": [10_000]}})

    client = make_cost_client("elevenlabs", "xi-key")
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    records = client.fetch_costs(dt.date(2026, 5, 1))
    # 10k characters at $0.15 / 1k chars = $1.50, computed transparently by us.
    assert records[0].amount == Decimal("1.5000")
    assert records[0].provider == "elevenlabs"


def test_azure_parses_cost_query_by_tag():
    import json

    from meter.providers import make_cost_client

    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth2" in str(request.url):
            return httpx.Response(200, json={"access_token": "tok"})
        assert "Microsoft.CostManagement/query" in str(request.url)
        return httpx.Response(
            200,
            json={
                "properties": {
                    "columns": [{"name": "Cost"}, {"name": "feature"}, {"name": "Currency"}],
                    "rows": [[820.0, "triage", "USD"], [60.0, "", "USD"]],
                }
            },
        )

    client = make_cost_client(
        "azure",
        json.dumps(
            {
                "tenant_id": "t",
                "client_id": "c",
                "client_secret": "s",
                "subscription_id": "sub",
                "tag": "feature",
            }
        ),
    )
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    records = client.fetch_costs(dt.date(2026, 5, 1))
    by_tag = {r.api_key_ref: r.amount for r in records}
    assert by_tag["triage"] == Decimal("820.0")
    assert by_tag[None] == Decimal("60.0")  # untagged -> Unattributed


def test_new_json_connectors_require_json():
    from meter.providers import (
        AzureCostClient,
        LiteLLMCostClient,
        ModalCostClient,
        VercelGatewayCostClient,
    )

    for cls in (AzureCostClient, LiteLLMCostClient, VercelGatewayCostClient, ModalCostClient):
        with pytest.raises(ProviderError):
            cls("not-json")


def test_portkey_reads_analytics_cost():
    import json

    from meter.providers import make_cost_client

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"  # read-only
        assert request.headers["x-portkey-api-key"] == "pk-123"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"model": "gpt-4o", "cost": "84.20"},
                    {"model": "claude", "cost": "5.80"},
                ]
            },
        )

    client = make_cost_client("portkey", json.dumps({"api_key": "pk-123"}))
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    records = client.fetch_costs(dt.date(2026, 5, 1))
    by_model = {r.model: r.amount for r in records}
    assert by_model["gpt-4o"] == Decimal("84.20")
    assert by_model["claude"] == Decimal("5.80")


def test_groq_prices_tokens_via_hosted_pattern():
    from meter.providers import make_cost_client

    def handler(_request: httpx.Request) -> httpx.Response:
        # No dollar cost -> priced from tokens. groq llama-3.1-8b-instant = 0.05/0.08 per M.
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "model": "llama-3.1-8b-instant",
                        "prompt_tokens": 1_000_000,
                        "completion_tokens": 1_000_000,
                    }
                ]
            },
        )

    client = make_cost_client("groq", "gsk-key")
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    records = client.fetch_costs(dt.date(2026, 5, 1))
    assert records[0].amount == Decimal("0.1300")  # 0.05 + 0.08, computed by us


# ---------------------------------------------------------------------------
# Anthropic Usage Report + org metadata (workspace/api-key identity).
# ---------------------------------------------------------------------------
def test_anthropic_usage_report_groups_and_paginates():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["x-api-key"] == "sk-ant-admin"
        assert request.method == "GET"  # read-only
        assert "/v1/organizations/usage_report/messages" in str(request.url)
        groups = request.url.params.get_list("group_by[]")
        assert groups == ["workspace_id", "api_key_id", "model", "service_tier"]
        if request.url.params.get("page") is None:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "results": [
                                {
                                    "workspace_id": "wrkspc_mcs",
                                    "api_key_id": "apikey_a",
                                    "model": "claude-sonnet-4-6",
                                    "service_tier": "standard",
                                    "uncached_input_tokens": 900,
                                    "cache_read_input_tokens": 100,
                                    "output_tokens": 500,
                                    "request_count": 7,
                                }
                            ]
                        }
                    ],
                    "has_more": True,
                    "next_page": "cursor2",
                },
            )
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "results": [
                            {
                                "workspace_id": "wrkspc_sos",
                                "api_key_id": "apikey_b",
                                "model": "claude-haiku-4-5",
                                "input_tokens": 200,
                                "output_tokens": 50,
                            }
                        ]
                    }
                ],
                "has_more": False,
                "next_page": None,
            },
        )

    client = AnthropicCostClient(
        "sk-ant-admin", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    usage = client.fetch_usage(dt.date(2026, 5, 10))
    assert len(calls) == 2  # followed pagination
    first = next(u for u in usage if u.api_key_id == "apikey_a")
    assert first.workspace_id == "wrkspc_mcs"
    assert first.tokens_in == 1000  # uncached 900 + cache_read 100
    assert first.cached_tokens_in == 100
    assert first.tokens_out == 500
    assert first.request_count == 7
    assert first.service_tier == "standard"
    second = next(u for u in usage if u.api_key_id == "apikey_b")
    assert second.tokens_in == 200  # single input_tokens field honored


def test_anthropic_fetch_workspaces_resolves_names_and_paginates():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/v1/organizations/workspaces" in str(request.url)
        if request.url.params.get("after_id") is None:
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "wrkspc_mcs", "name": "mcs-dev"}],
                    "has_more": True,
                    "last_id": "wrkspc_mcs",
                },
            )
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "wrkspc_sos", "name": "sos-dev"},
                    {"id": "wrkspc_ti", "name": "threatintel-dev"},
                ],
                "has_more": False,
                "last_id": "wrkspc_ti",
            },
        )

    client = AnthropicCostClient(
        "sk-ant-admin", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    workspaces = client.fetch_workspaces()
    assert workspaces == {
        "wrkspc_mcs": "mcs-dev",
        "wrkspc_sos": "sos-dev",
        "wrkspc_ti": "threatintel-dev",
    }


def test_anthropic_fetch_api_keys_resolves_names_and_workspace():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/v1/organizations/api_keys" in str(request.url)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "apikey_a", "name": "service-a-prod", "workspace_id": "wrkspc_mcs"},
                    {"id": "apikey_b", "name": "experimental", "workspace_id": "wrkspc_mcs"},
                ],
                "has_more": False,
                "last_id": "apikey_b",
            },
        )

    client = AnthropicCostClient(
        "sk-ant-admin", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    keys = client.fetch_api_keys()
    assert keys["apikey_a"] == {"name": "service-a-prod", "workspace_id": "wrkspc_mcs"}
    assert keys["apikey_b"]["name"] == "experimental"
