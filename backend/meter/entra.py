"""Read-only Microsoft Entra ID (Azure AD) client for SSO seat rosters.

The second identity provider (after Okta). Entra is the Microsoft enterprise
system of record for app assignments. We read the users assigned to an enterprise
application (its service principal's ``appRoleAssignedTo``) via Microsoft Graph,
then resolve + price them through the same seat engine as Okta.

Auth is OAuth2 client credentials (an app registration). Credentials are stored as
one encrypted JSON blob: {"tenant_id", "client_id", "client_secret"}. Rosters are
normalized to the same {"profile": {...}} shape Okta returns so the identity
resolver and sync are provider-agnostic. JSON shapes can't be verified offline —
parsing is tolerant; the stable contract is the normalized roster.
"""

from __future__ import annotations

import json
from typing import Optional

import httpx

from .retrying import http_get_with_retry

_GRAPH = "https://graph.microsoft.com/v1.0"
_LOGIN = "https://login.microsoftonline.com"


class EntraError(Exception):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def parse_entra_credential(secret: str) -> dict:
    """Return {tenant_id, client_id, client_secret} from the JSON credential blob."""
    try:
        creds = json.loads(secret)
    except (ValueError, TypeError) as exc:
        raise EntraError(
            'Entra credential must be JSON: {"tenant_id":..., "client_id":..., "client_secret":...}'
        ) from exc
    tenant_id = creds.get("tenant_id") or creds.get("tenant")
    client_id = creds.get("client_id")
    client_secret = creds.get("client_secret")
    if not (tenant_id and client_id and client_secret):
        raise EntraError("Entra credential needs tenant_id, client_id, and client_secret.")
    return {"tenant_id": tenant_id, "client_id": client_id, "client_secret": client_secret}


class EntraClient:
    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        *,
        client: Optional[httpx.Client] = None,
        access_token: Optional[str] = None,
    ):
        self._tenant = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._client = client or httpx.Client(timeout=30.0)
        self._owns_client = client is None
        self._access = access_token

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        if self._owns_client:
            self._client.close()

    def _ensure_token(self) -> None:
        if self._access:
            return
        resp = self._client.post(
            f"{_LOGIN}/{self._tenant}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "scope": "https://graph.microsoft.com/.default",
            },
        )
        if resp.status_code >= 400:
            raise EntraError(f"Entra token request failed ({resp.status_code}).", resp.status_code)
        self._access = (resp.json() or {}).get("access_token")
        if not self._access:
            raise EntraError("Entra did not return an access token.")

    def _get(self, url: str) -> httpx.Response:
        self._ensure_token()
        resp = http_get_with_retry(
            self._client, url, headers={"Authorization": f"Bearer {self._access}"}
        )
        if resp.status_code in (401, 403):
            raise EntraError("Entra/Graph rejected the credentials.", resp.status_code)
        if resp.status_code >= 400:
            raise EntraError(f"Graph error {resp.status_code}: {resp.text[:200]}", resp.status_code)
        return resp

    def _get_all(self, url: str) -> list[dict]:
        items: list[dict] = []
        while url:
            data = self._get(url).json() or {}
            items.extend(data.get("value", []))
            url = data.get("@odata.nextLink", "")
        return items

    def list_app_users(self, app_id: str) -> list[dict]:
        """Users assigned to an enterprise app, normalized to the Okta roster shape.

        ``app_id`` is the enterprise application's service-principal object id.
        """
        assignments = self._get_all(f"{_GRAPH}/servicePrincipals/{app_id}/appRoleAssignedTo")
        users: list[dict] = []
        for assignment in assignments:
            if assignment.get("principalType") != "User":
                continue  # group assignments need expansion; skip in v1
            pid = assignment.get("principalId")
            if not pid:
                continue
            user = self._get(
                f"{_GRAPH}/users/{pid}?$select=userPrincipalName,mail,displayName"
            ).json()
            email = user.get("mail") or user.get("userPrincipalName") or ""
            users.append(
                {"profile": {"email": email, "login": email.split("@")[0] if email else None}}
            )
        return users
