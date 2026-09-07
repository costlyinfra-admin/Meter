"""SSO/SCIM seat sourcing: map IdP app rosters to per-developer build cost.

An identity provider (Okta) knows which users are assigned which app. A
``seat_source`` maps an IdP application to a priced coding tool+plan; syncing
pulls the roster, resolves each identity to a GitHub login, prices the seat, and
feeds the EXISTING PR-authorship allocator (build.allocate_and_store) — so seats
land on features automatically, no CSV.

Identity bridge: the IdP keys on email/username, our attribution on GitHub login.
We resolve against the set of known PR authors (login or email local-part,
case-insensitive). Unresolved seats are still counted as cost but land in
Unattributed — never silently dropped, never silently misattributed.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

from . import build, credentials, entra, okta, seatpricing
from .db import app_dsn, connect, tenant_tx

#: Identity providers we can read seat rosters from (provider -> credential type).
IDP_PROVIDERS = {"okta", "entra"}


class SeatSourceError(Exception):
    pass


def _idp_client(provider: str, secret: str):
    """Build the right IdP client; both expose list_app_users() -> normalized roster."""
    if provider == "okta":
        domain, token = okta.parse_okta_credential(secret)
        return okta.OktaClient(domain, token)
    if provider == "entra":
        return entra.EntraClient(**entra.parse_entra_credential(secret))
    raise SeatSourceError(f"Unknown identity provider: {provider}")


def register_seat_source(
    tenant_id: str, provider: str, app_id: str, app_label: str, tool: str, plan: str
) -> dict:
    """Create/update a mapping: IdP app -> priced coding tool+plan."""
    if tool not in seatpricing.KNOWN_TOOLS or not seatpricing.is_seat_priced(tool, plan):
        raise SeatSourceError(
            f"No seat price for {tool!r}/{plan!r}. Known: "
            + ", ".join(
                f"{t}/{p}"
                for t in sorted(seatpricing.KNOWN_TOOLS)
                for p in seatpricing.known_plans(t)
            )
        )
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        row = conn.execute(
            """
            INSERT INTO seat_source (tenant_id, provider, app_id, app_label, tool, plan)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, provider, app_id)
            DO UPDATE SET app_label = EXCLUDED.app_label, tool = EXCLUDED.tool, plan = EXCLUDED.plan
            RETURNING id, provider, app_id, app_label, tool, plan
            """,
            (tenant_id, provider, app_id, app_label, tool, plan),
        ).fetchone()
    return _source_dict(row)


def list_seat_sources(tenant_id: str) -> list[dict]:
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        rows = conn.execute(
            "SELECT id, provider, app_id, app_label, tool, plan FROM seat_source ORDER BY app_label"
        ).fetchall()
    return [_source_dict(r) for r in rows]


def _source_dict(row) -> dict:
    return {
        "id": str(row[0]),
        "provider": row[1],
        "app_id": row[2],
        "app_label": row[3],
        "tool": row[4],
        "plan": row[5],
    }


def _resolve_login(okta_user: dict, actors_lower: dict) -> str:
    """Resolve an Okta app-user to a developer id (GitHub login when matchable)."""
    profile = okta_user.get("profile") or {}
    creds = okta_user.get("credentials") or {}
    candidates: list[str] = []
    for key in ("gitHubUsername", "githubUsername", "github_username", "login"):
        if profile.get(key):
            candidates.append(str(profile[key]))
    for value in (profile.get("email"), creds.get("userName")):
        if value:
            candidates.append(str(value))

    for cand in candidates:
        for form in (cand, cand.split("@")[0]):
            actor = actors_lower.get(form.lower())
            if actor:
                return actor  # matched a known PR author -> attributes to features
    # Unmatched: keep a stable identity (email local-part) -> lands in Unattributed.
    return candidates[0].split("@")[0] if candidates else "unknown"


def sync_idp_seats(tenant_id: str, period: dt.date) -> dict:
    """Pull every registered seat source's roster (any IdP) and allocate to features."""
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        sources = conn.execute(
            "SELECT provider, app_id, app_label, tool, plan FROM seat_source"
        ).fetchall()
        actors_lower = {
            r[0].lower(): r[0]
            for r in conn.execute(
                "SELECT DISTINCT actor FROM feature_signal WHERE actor IS NOT NULL"
            ).fetchall()
        }

    by_provider: dict[str, list] = defaultdict(list)
    for provider, app_id, app_label, tool, plan in sources:
        by_provider[provider].append((app_id, app_label, tool, plan))

    spends: list[build.DeveloperSpend] = []
    per_source: list[dict] = []
    for provider, srcs in by_provider.items():
        secret = credentials.get_secret(tenant_id, provider)
        if not secret:
            raise SeatSourceError(f"Connect {provider} before syncing its seats.")
        with _idp_client(provider, secret) as client:
            for app_id, app_label, tool, plan in srcs:
                users = client.list_app_users(app_id)
                price = seatpricing.seat_price(tool, plan)
                for user in users:
                    spends.append(
                        build.DeveloperSpend(_resolve_login(user, actors_lower), tool, price)
                    )
                per_source.append(
                    {
                        "provider": provider,
                        "app_label": app_label or app_id,
                        "tool": tool,
                        "plan": plan,
                        "seats": len(users),
                        "seat_price": float(price),
                    }
                )

    if spends:
        summary = build.allocate_and_store(tenant_id, spends, period)
    else:
        summary = build.build_summary(tenant_id, period)
    summary["sources"] = per_source
    summary["total_seats"] = sum(s["seats"] for s in per_source)
    return summary
