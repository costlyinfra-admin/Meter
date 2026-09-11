"""Anthropic Claude Code Analytics: per-developer spend -> build cost."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import httpx
import pytest
from meter import claudecode, credentials, discovery
from meter.github import PullRequest

PERIOD = dt.date(2026, 5, 1)


def _pr(number, title, branch, author):
    return PullRequest(number, "acme/core", title, "", branch, author, "2026-05-01T00:00:00Z", "")


FIXTURE_PRS = [
    _pr(1, "Threat triage automation", "feature/threat-triage", "alice"),
    _pr(2, "Threat scoring model", "feature/threat-scoring", "alice"),
    _pr(3, "Report generator", "feature/report-gen", "bob"),
]


class _FakeDiscoveryGitHub:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def list_repos(self, owner):
        return ["acme/core"]

    def fetch_merged_prs(self, owner, since):
        return FIXTURE_PRS


@pytest.fixture
def discovered(tenant_id, monkeypatch):
    monkeypatch.setattr(discovery, "_make_github_client", lambda token: _FakeDiscoveryGitHub())
    discovery.run_discovery(tenant_id, "acme", "tok")
    return tenant_id


def test_client_aggregates_per_user_cost_and_paginates():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["x-api-key"] == "sk-ant-admin"
        assert "/v1/organizations/usage_report/claude_code" in str(request.url)
        page = request.url.params.get("page")
        if page is None:
            # Two daily records for alice (sums), one for bob; cost shapes vary.
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "actor": {"email_address": "alice@acme.com"},
                            "estimated_cost": {"amount": "12.50", "currency": "USD"},
                        },
                        {
                            "actor": {"email_address": "alice@acme.com"},
                            "model_breakdown": [
                                {"estimated_cost": {"amount": "4.50"}},
                                {"estimated_cost": {"amount": "0.50"}},
                            ],
                        },
                    ],
                    "has_more": True,
                    "next_page": "cursor2",
                },
            )
        return httpx.Response(
            200,
            json={
                "data": [
                    {"actor": {"email_address": "bob@acme.com"}, "estimated_cost_cents": 8400}
                ],
                "has_more": False,
                "next_page": None,
            },
        )

    client = claudecode.ClaudeCodeClient(
        "sk-ant-admin", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    by_email = {m["email"]: m["amount"] for m in client.fetch_member_spend(PERIOD)}
    assert len(calls) == 2  # followed pagination
    assert by_email["alice@acme.com"] == Decimal("17.50")  # 12.50 + 4.50 + 0.50
    assert by_email["bob@acme.com"] == Decimal("84")  # 8400 cents


def test_client_rejects_bad_admin_key():
    def handler(_request):
        return httpx.Response(401, json={"error": "unauthorized"})

    client = claudecode.ClaudeCodeClient(
        "bad", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(claudecode.ClaudeCodeError) as exc:
        client.fetch_member_spend(PERIOD)
    assert exc.value.status == 401


def test_import_allocates_claude_code_spend_to_features(discovered, monkeypatch):
    credentials.save_credential(discovered, "anthropic", "sk-ant-admin")

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def fetch_member_spend(self, period):
            return [
                {"email": "alice@acme.com", "amount": Decimal("117")},  # -> Threat
                {"email": "bob@acme.com", "amount": Decimal("84")},  # -> Report
                {"email": "idle@acme.com", "amount": Decimal("0")},  # skipped
                {"email": "contractor@vendor.com", "amount": Decimal("30")},  # Unattributed
            ]

    monkeypatch.setattr(claudecode, "_make_client", lambda key: _FakeClient())

    summary = claudecode.import_claude_code_spend(discovered, PERIOD)
    assert summary["members"] == 4
    assert summary["spending_members"] == 3

    features = {f["name"]: f for f in summary["features"]}
    assert features["Threat"]["amount"] == 117.0  # alice
    assert features["Threat"]["by_tool"] == {"claude_code": 117.0}
    assert features["Reports"]["amount"] == 84.0  # bob
    assert summary["unattributed"] == 30.0  # contractor
    assert summary["total"] == 231.0


def test_import_requires_anthropic_connected(tenant_id):
    with pytest.raises(claudecode.ClaudeCodeError):
        claudecode.import_claude_code_spend(tenant_id, PERIOD)


# ---- Several Anthropic organisations --------------------------------------
def test_two_organisations_spend_is_summed_per_developer(discovered, monkeypatch):
    # A company billed through two Anthropic organisations has developers in
    # both. build.allocate_and_store clears the tool's month before rewriting
    # it, so allocating per organisation would have the second write delete the
    # first — the first org's developers would look like they spent nothing.
    credentials.save_credential(discovered, "anthropic", "sk-acme", label="Acme")
    credentials.save_credential(discovered, "anthropic", "sk-labs", label="Acme Labs")

    class _PerOrgClient:
        def __init__(self, key):
            self._key = key

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def fetch_member_spend(self, period):
            if self._key == "sk-acme":
                return [
                    {"email": "alice@acme.com", "amount": Decimal("100")},
                    {"email": "bob@acme.com", "amount": Decimal("84")},
                ]
            # alice has a seat in both organisations; the company pays for both.
            return [{"email": "alice@acme.com", "amount": Decimal("17")}]

    monkeypatch.setattr(claudecode, "_make_client", _PerOrgClient)

    summary = claudecode.import_claude_code_spend(discovered, PERIOD)
    # Two people, not three rows: alice is one developer across two orgs.
    assert summary["members"] == 2
    assert summary["spending_members"] == 2

    features = {f["name"]: f for f in summary["features"]}
    assert features["Threat"]["amount"] == 117.0  # alice: 100 + 17
    assert features["Reports"]["amount"] == 84.0  # bob, from one org only
