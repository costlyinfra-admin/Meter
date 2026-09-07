"""Cursor Admin API: per-developer usage-based spend (build cost, Phase 4).

Cursor for Teams exposes an Admin API keyed by a team API key. Unlike the
seat-based tools, Cursor bills usage beyond the base seat, and its
``POST /teams/spend`` endpoint reports each member's ACTUAL spend — exact
dollars, not a seats x list-price estimate. We pull that roster of (member,
spend), resolve each member to a GitHub login (same identity bridge as the IdP
seats), and feed the existing PR-authorship allocator.

Re-running replaces the period's prior 'cursor' build rows — including any
seat-price estimate from an IdP sync — so the most precise source wins.

Auth is HTTP Basic with the API key as the username (per Cursor's docs). As with
every external connector, the JSON shape can't be verified offline — parsing is
tolerant and the stable contract is the normalized (email, amount) list.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Optional

import httpx

from . import build, credentials, seats
from .db import app_dsn, connect, tenant_tx

_BASE = "https://api.cursor.com"
_PAGE_SIZE = 100


class CursorError(Exception):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class CursorAdminClient:
    def __init__(self, api_key: str, *, client: Optional[httpx.Client] = None):
        self._key = api_key
        self._client = client or httpx.Client(timeout=30.0)
        self._owns_client = client is None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        if self._owns_client:
            self._client.close()

    def fetch_member_spend(self) -> list[dict]:
        """Per-member spend for the current billing cycle: [{email, name, amount}]."""
        members: list[dict] = []
        page = 1
        while True:
            resp = self._client.post(
                f"{_BASE}/teams/spend",
                json={"page": page, "pageSize": _PAGE_SIZE},
                auth=(self._key, ""),
            )
            if resp.status_code in (401, 403):
                raise CursorError("Cursor rejected the admin API key.", resp.status_code)
            if resp.status_code >= 400:
                raise CursorError(
                    f"Cursor API error {resp.status_code}: {resp.text[:200]}", resp.status_code
                )
            data = resp.json() or {}
            rows = data.get("teamMemberSpend") or data.get("members") or []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                email = row.get("email") or row.get("userEmail") or ""
                cents = row.get("spendCents")
                amount = (
                    Decimal(int(cents)) / 100
                    if cents is not None
                    else Decimal(str(row.get("spend") or "0"))
                )
                members.append({"email": email, "name": row.get("name"), "amount": amount})
            total_pages = int(data.get("totalPages") or 1)
            if page >= total_pages or not rows:
                return members
            page += 1


def _make_cursor_client(api_key: str):  # seam so tests can inject a fake
    return CursorAdminClient(api_key)


def import_cursor_spend(tenant_id: str, period: dt.date) -> dict:
    """Pull per-member Cursor spend and allocate it to features. Idempotent."""
    secret = credentials.get_secret(tenant_id, "cursor")
    if not secret:
        raise CursorError("Connect Cursor (admin API key) before syncing spend.")

    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        actors_lower = {
            r[0].lower(): r[0]
            for r in conn.execute(
                "SELECT DISTINCT actor FROM feature_signal WHERE actor IS NOT NULL"
            ).fetchall()
        }

    with _make_cursor_client(secret) as client:
        members = client.fetch_member_spend()

    spends = [
        build.DeveloperSpend(
            seats._resolve_login({"profile": {"email": m["email"]}}, actors_lower),
            "cursor",
            m["amount"],
        )
        for m in members
        if m["amount"] > 0  # zero-spend members add no cost, only noise
    ]

    if spends:
        summary = build.allocate_and_store(tenant_id, spends, period)
    else:
        summary = build.build_summary(tenant_id, period)
    summary["members"] = len(members)
    summary["spending_members"] = len(spends)
    return summary
