"""Cursor Admin API: per-member usage spend -> per-developer build cost."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import httpx
import pytest
from meter import credentials, cursorspend, discovery
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


def test_client_pages_spend_with_basic_auth():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        # Basic auth, API key as username (empty password).
        assert request.headers["Authorization"].startswith("Basic ")
        assert request.method == "POST"
        import json as _json

        page = _json.loads(request.content)["page"]
        if page == 1:
            return httpx.Response(
                200,
                json={
                    "totalPages": 2,
                    "teamMemberSpend": [
                        {"email": "alice@acme.com", "name": "Alice", "spendCents": 11700},
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "totalPages": 2,
                "teamMemberSpend": [{"email": "bob@acme.com", "spendCents": 8400}],
            },
        )

    client = cursorspend.CursorAdminClient(
        "key_abc", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    members = client.fetch_member_spend()
    assert len(calls) == 2  # followed pagination
    assert members[0] == {"email": "alice@acme.com", "name": "Alice", "amount": Decimal("117")}
    assert members[1]["amount"] == Decimal("84")


def test_client_rejects_bad_key():
    def handler(_request):
        return httpx.Response(401, json={"error": "unauthorized"})

    client = cursorspend.CursorAdminClient(
        "bad", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(cursorspend.CursorError) as exc:
        client.fetch_member_spend()
    assert exc.value.status == 401


def test_import_cursor_spend_allocates_actual_dollars(discovered, monkeypatch):
    credentials.save_credential(discovered, "cursor", "key_abc")

    class _FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def fetch_member_spend(self):
            return [
                {"email": "alice@acme.com", "name": "Alice", "amount": Decimal("117")},
                {"email": "bob@acme.com", "name": "Bob", "amount": Decimal("84")},
                {"email": "idle@acme.com", "name": "Idle", "amount": Decimal("0")},  # skipped
                {"email": "ghost@vendor.com", "name": "?", "amount": Decimal("30")},  # Unattributed
            ]

    monkeypatch.setattr(cursorspend, "_make_cursor_client", lambda key: _FakeCursor())

    summary = cursorspend.import_cursor_spend(discovered, PERIOD)
    assert summary["members"] == 4
    assert summary["spending_members"] == 3  # zero-spend member excluded

    features = {f["name"]: f for f in summary["features"]}
    # Actual usage dollars, not seat-price estimates.
    assert features["Threat"]["amount"] == 117.0  # alice
    assert features["Reports"]["amount"] == 84.0  # bob
    assert summary["unattributed"] == 30.0  # ghost@vendor.com
    assert summary["total"] == 231.0


def test_import_requires_connected_cursor(tenant_id):
    with pytest.raises(cursorspend.CursorError):
        cursorspend.import_cursor_spend(tenant_id, PERIOD)
