"""Microsoft Entra ID (Graph) seat-roster client."""

from __future__ import annotations

import json

import httpx
import pytest
from meter import entra


def test_entra_credential_parsing():
    cfg = entra.parse_entra_credential(
        json.dumps({"tenant_id": "t1", "client_id": "c1", "client_secret": "s1"})
    )
    assert cfg == {"tenant_id": "t1", "client_id": "c1", "client_secret": "s1"}
    with pytest.raises(entra.EntraError):
        entra.parse_entra_credential("not-json")
    with pytest.raises(entra.EntraError):
        entra.parse_entra_credential(json.dumps({"tenant_id": "t1"}))  # missing fields


def test_entra_lists_app_users_via_graph():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST" and "oauth2/v2.0/token" in url:
            return httpx.Response(200, json={"access_token": "gtoken", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer gtoken"  # graph calls are authed
        if "appRoleAssignedTo" in url:
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"principalType": "User", "principalId": "u1"},
                        {"principalType": "Group", "principalId": "g1"},  # skipped in v1
                    ]
                },
            )
        if "/users/u1" in url:
            return httpx.Response(
                200, json={"userPrincipalName": "alice@acme.com", "mail": "alice@acme.com"}
            )
        return httpx.Response(404, json={})

    client = entra.EntraClient(
        "tenant", "client", "secret", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    users = client.list_app_users("sp-id")
    assert len(users) == 1  # group assignment skipped
    assert users[0]["profile"]["email"] == "alice@acme.com"
    assert users[0]["profile"]["login"] == "alice"
