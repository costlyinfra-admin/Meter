"""Anthropic Claude Code Analytics API: per-developer Claude Code spend (build).

Claude Code is billed through Anthropic, and the Admin API's Claude Code
analytics endpoint reports per-user (by email) usage and estimated cost. We pull
each developer's cost for the period, resolve their email to a GitHub login (the
same identity bridge as the IdP seats), and feed the EXISTING PR-authorship
allocator as build cost (tool='claude_code') — automatically, no CSV.

Reuses the stored Anthropic admin key (the one the inference connector uses) — no
new credential. Re-running replaces the period's prior 'claude_code' build rows
(idempotent). Zero-spend developers are skipped; unmatched emails are still
costed and land in Unattributed.

As with the other external connectors, the JSON shape can't be verified offline —
parsing is tolerant and the stable contract is the per-(email) cost total.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Optional

import httpx

from . import build, credentials, seats
from .db import app_dsn, connect, tenant_tx
from .providers import month_start, next_month
from .retrying import http_get_with_retry

_BASE = "https://api.anthropic.com"
_PATH = "/v1/organizations/usage_report/claude_code"
_PAGE_LIMIT = 30


class ClaudeCodeError(Exception):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def _to_dollars(value) -> Optional[Decimal]:
    """Coerce an estimated-cost field (dict {amount}, number, or string) to USD."""
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("amount")
        if value is None:
            return None
    try:
        return Decimal(str(value))
    except (ValueError, ArithmeticError):
        return None


def _record_email(record: dict) -> str:
    actor = record.get("actor") or {}
    return actor.get("email_address") or actor.get("email") or record.get("email_address") or ""


def _record_cost(record: dict) -> Decimal:
    cents = record.get("estimated_cost_cents")
    if cents is not None:
        try:
            return Decimal(int(cents)) / 100
        except (ValueError, ArithmeticError):
            pass
    direct = _to_dollars(record.get("estimated_cost"))
    if direct is not None:
        return direct
    total = Decimal("0")
    for breakdown in record.get("model_breakdown") or []:
        if isinstance(breakdown, dict):
            total += _to_dollars(breakdown.get("estimated_cost")) or Decimal("0")
    return total


class ClaudeCodeClient:
    def __init__(self, admin_key: str, *, client: Optional[httpx.Client] = None):
        self._key = admin_key
        self._client = client or httpx.Client(timeout=30.0)
        self._owns_client = client is None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        if self._owns_client:
            self._client.close()

    def fetch_member_spend(self, period: dt.date) -> list[dict]:
        """Per-developer Claude Code cost for the period: [{email, amount}]."""
        start = month_start(period)
        end = next_month(start)
        totals: dict[str, Decimal] = {}
        page: Optional[str] = None
        while True:
            params: dict = {
                "starting_at": start.isoformat(),
                "ending_at": end.isoformat(),
                "limit": _PAGE_LIMIT,
            }
            if page:
                params["page"] = page
            resp = http_get_with_retry(
                self._client,
                f"{_BASE}{_PATH}",
                params=params,
                headers={"x-api-key": self._key, "anthropic-version": "2023-06-01"},
            )
            if resp.status_code in (401, 403):
                raise ClaudeCodeError("Anthropic rejected the admin key.", resp.status_code)
            if resp.status_code >= 400:
                raise ClaudeCodeError(
                    f"Anthropic API error {resp.status_code}: {resp.text[:200]}", resp.status_code
                )
            data = resp.json() or {}
            for record in data.get("data", []):
                if not isinstance(record, dict):
                    continue
                email = _record_email(record)
                if not email:
                    continue
                totals[email] = totals.get(email, Decimal("0")) + _record_cost(record)
            if data.get("has_more") and data.get("next_page"):
                page = data["next_page"]
                continue
            return [{"email": email, "amount": amount} for email, amount in totals.items()]


def _make_client(admin_key: str):  # seam so tests can inject a fake
    return ClaudeCodeClient(admin_key)


def import_claude_code_spend(tenant_id: str, period: dt.date) -> dict:
    """Pull per-developer Claude Code spend and allocate it to features."""
    admin_key = credentials.get_secret(tenant_id, "anthropic")
    if not admin_key:
        raise ClaudeCodeError("Connect Anthropic (admin key) before syncing Claude Code spend.")

    start = month_start(period)
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        actors_lower = {
            r[0].lower(): r[0]
            for r in conn.execute(
                "SELECT DISTINCT actor FROM feature_signal WHERE actor IS NOT NULL"
            ).fetchall()
        }

    with _make_client(admin_key) as client:
        members = client.fetch_member_spend(period)

    spends = [
        build.DeveloperSpend(
            seats._resolve_login({"profile": {"email": m["email"]}}, actors_lower),
            "claude_code",
            m["amount"],
        )
        for m in members
        if m["amount"] > 0
    ]

    if spends:
        summary = build.allocate_and_store(tenant_id, spends, period)
    else:
        # Nothing spent -> clear any prior Claude Code rows for the period.
        with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
            conn.execute(
                "DELETE FROM build_cost WHERE period = %s AND tool = 'claude_code'", (start,)
            )
        summary = build.build_summary(tenant_id, period)

    summary["members"] = len(members)
    summary["spending_members"] = len(spends)
    return summary
