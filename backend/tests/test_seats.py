"""SSO/SCIM seat sourcing: Okta roster -> per-developer build cost -> features."""

from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest
from meter import credentials, discovery, okta, seats
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


def test_resolve_login_matches_known_actor():
    actors = {"alice": "alice", "bob": "bob"}
    # email local-part matches a known GitHub author.
    assert seats._resolve_login({"profile": {"email": "alice@acme.com"}}, actors) == "alice"
    # explicit github username attribute wins.
    assert seats._resolve_login({"profile": {"gitHubUsername": "bob"}}, actors) == "bob"
    # unknown identity -> kept (email local-part) but won't match a feature.
    assert seats._resolve_login({"profile": {"email": "zoe@acme.com"}}, actors) == "zoe"


def test_register_seat_source_rejects_unpriced_tool(tenant_id):
    with pytest.raises(seats.SeatSourceError):
        seats.register_seat_source(tenant_id, "okta", "app1", "Mystery", "mysterytool", "pro")


def test_sync_idp_seats_allocates_to_features(discovered, monkeypatch):
    # Okta credential (JSON blob) + a seat source mapping an app to Cursor Business.
    credentials.save_credential(
        discovered, "okta", json.dumps({"domain": "acme.okta.com", "token": "SSWS-xyz"})
    )
    seats.register_seat_source(discovered, "okta", "0oaCursor", "Cursor", "cursor", "business")

    roster = [
        {"profile": {"email": "alice@acme.com"}},  # -> alice (Threat)
        {"profile": {"login": "bob"}},  # -> bob (Report)
        {"profile": {"email": "contractor@vendor.com"}},  # unmatched -> Unattributed
    ]

    class _FakeOkta:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def list_app_users(self, app_id):
            assert app_id == "0oaCursor"
            return roster

    monkeypatch.setattr(seats.okta, "OktaClient", lambda domain, token: _FakeOkta())

    summary = seats.sync_idp_seats(discovered, PERIOD)

    assert summary["total_seats"] == 3
    assert summary["sources"][0]["tool"] == "cursor"
    assert summary["sources"][0]["seat_price"] == 40.0  # Cursor business

    features = {f["name"]: f for f in summary["features"]}
    assert features["Threat"]["amount"] == 40.0  # alice's seat
    assert features["Threat"]["by_tool"] == {"cursor": 40.0}
    assert features["Reports"]["amount"] == 40.0  # bob's seat
    assert summary["unattributed"] == 40.0  # the unmatched contractor's seat
    assert summary["total"] == 120.0  # 3 seats x $40


def test_sync_idp_seats_supports_entra(discovered, monkeypatch):
    # The same seat engine works for Microsoft Entra ID (normalized roster shape).
    credentials.save_credential(
        discovered, "entra", json.dumps({"tenant_id": "t", "client_id": "c", "client_secret": "s"})
    )
    seats.register_seat_source(
        discovered, "entra", "sp-tabnine", "Tabnine", "tabnine", "enterprise"
    )

    class _FakeEntra:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def list_app_users(self, app_id):
            assert app_id == "sp-tabnine"
            return [{"profile": {"email": "alice@acme.com"}}]  # -> alice (Threat)

    monkeypatch.setattr(seats.entra, "EntraClient", lambda **kw: _FakeEntra())

    summary = seats.sync_idp_seats(discovered, PERIOD)
    assert summary["sources"][0]["provider"] == "entra"
    features = {f["name"]: f for f in summary["features"]}
    assert features["Threat"]["by_tool"] == {"tabnine": 39.0}  # Tabnine enterprise seat


def test_okta_credential_parsing_and_client_auth():
    domain, token = okta.parse_okta_credential(
        json.dumps({"domain": "https://acme.okta.com/", "token": "t"})
    )
    assert domain == "acme.okta.com"
    assert token == "t"
    with pytest.raises(okta.OktaError):
        okta.parse_okta_credential("not-json")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "SSWS t"
        assert "/api/v1/apps/app1/users" in str(request.url)
        return httpx.Response(200, json=[{"profile": {"login": "alice"}}])

    client = okta.OktaClient(
        "acme.okta.com", "t", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    users = client.list_app_users("app1")
    assert users[0]["profile"]["login"] == "alice"


def test_two_okta_directories_both_contribute_seats(discovered, monkeypatch):
    # A company with two Okta tenants has people in both. Build cost is rewritten
    # per tool and month, so reading one directory would price only half the
    # seats and the other half would look free.
    credentials.save_credential(
        discovered, "okta", json.dumps({"domain": "acme.okta.com", "token": "SSWS-a"}), label="Acme"
    )
    credentials.save_credential(
        discovered, "okta", json.dumps({"domain": "labs.okta.com", "token": "SSWS-b"}), label="Labs"
    )
    seats.register_seat_source(discovered, "okta", "0oaCursor", "Cursor", "cursor", "business")

    rosters = {
        "acme.okta.com": [{"profile": {"email": "alice@acme.com"}}],
        "labs.okta.com": [{"profile": {"login": "bob"}}],
    }

    class _FakeOkta:
        def __init__(self, domain):
            self._domain = domain

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def list_app_users(self, app_id):
            return rosters[self._domain]

    monkeypatch.setattr(seats.okta, "OktaClient", lambda domain, token: _FakeOkta(domain))

    summary = seats.sync_idp_seats(discovered, PERIOD)

    # One seat in each directory: both are paid for.
    assert summary["total_seats"] == 2
    # Still ONE source row, not one per user.
    assert len(summary["sources"]) == 1
    assert summary["sources"][0]["seats"] == 2
    features = {f["name"]: f for f in summary["features"]}
    assert features["Threat"]["amount"] == 40.0  # alice, Cursor business
    assert features["Reports"]["amount"] == 40.0  # bob
