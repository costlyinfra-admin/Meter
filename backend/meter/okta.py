"""Read-only Okta client for SSO/SCIM seat rosters (build cost, Phase 2).

Okta is the enterprise system of record for "who is assigned which app." We read
the users assigned to an application (`GET /api/v1/apps/{appId}/users`) to get the
seat roster for a coding tool, then price + allocate it. Auth is an Okta API token
(SSWS); credentials are stored as one encrypted JSON blob: {"domain", "token"}.

As with the other external connectors, the JSON shapes can't be verified offline —
parsing is tolerant and the stable contract is the returned (login/email) roster.
"""

from __future__ import annotations

import json
import re
from typing import Optional

import httpx

from .retrying import http_get_with_retry

_PER_PAGE = 200
_NEXT_LINK = re.compile(r'<([^>]+)>;\s*rel="next"')


class OktaError(Exception):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def parse_okta_credential(secret: str) -> tuple[str, str]:
    """Return (domain, token) from the stored JSON credential blob."""
    try:
        creds = json.loads(secret)
    except (ValueError, TypeError) as exc:
        raise OktaError('Okta credential must be JSON: {"domain":..., "token":...}') from exc
    domain = (creds.get("domain") or "").strip().replace("https://", "").rstrip("/")
    token = creds.get("token") or creds.get("api_token")
    if not domain or not token:
        raise OktaError("Okta credential needs both 'domain' and 'token'.")
    return domain, token


class OktaClient:
    def __init__(self, domain: str, token: str, *, client: Optional[httpx.Client] = None):
        self._base = f"https://{domain.replace('https://', '').rstrip('/')}"
        self._token = token
        self._client = client or httpx.Client(timeout=30.0)
        self._owns_client = client is None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        if self._owns_client:
            self._client.close()

    def _get(self, url: str, params: Optional[dict] = None) -> httpx.Response:
        resp = http_get_with_retry(
            self._client,
            url,
            params=params,
            headers={
                "Authorization": f"SSWS {self._token}",
                "Accept": "application/json",
            },
        )
        if resp.status_code in (401, 403):
            raise OktaError("Okta rejected the API token.", resp.status_code)
        if resp.status_code >= 400:
            raise OktaError(
                f"Okta API error {resp.status_code}: {resp.text[:200]}", resp.status_code
            )
        return resp

    def list_app_users(self, app_id: str) -> list[dict]:
        """All users assigned to an Okta application (follows cursor pagination)."""
        users: list[dict] = []
        url = f"{self._base}/api/v1/apps/{app_id}/users"
        params: Optional[dict] = {"limit": _PER_PAGE}
        while url:
            resp = self._get(url, params)
            users.extend(resp.json() or [])
            match = _NEXT_LINK.search(resp.headers.get("link", ""))
            url = match.group(1) if match else ""
            params = None  # the next link already carries the cursor
        return users
