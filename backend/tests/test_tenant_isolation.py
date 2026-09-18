"""The M1 acceptance test: a query for tenant A never returns tenant B's rows.

Two tenants are seeded by the bootstrap role (which bypasses RLS). All
cross-tenant assertions run as the non-privileged app role, where Row-Level
Security is in force.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from meter import db
from meter.db import tenant_tx
from meter.sampledata import create_tenant, insert_sample_data

# Tables that carry tenant_id and must be isolated.
TENANT_TABLES = [
    "feature",
    "feature_signal",
    "build_cost",
    "inference_cost",
    "bill_reconciliation",
    "feature_usage",
    "product",
    "product_repo",
]


@pytest.fixture
def two_tenants(admin_conn):
    """Seed two separate tenants; return their ids as (tenant_a, tenant_b)."""
    with admin_conn.transaction():
        tenant_a = create_tenant(admin_conn, "Tenant A")
        insert_sample_data(admin_conn, tenant_a)
        tenant_b = create_tenant(admin_conn, "Tenant B")
        insert_sample_data(admin_conn, tenant_b)
    return tenant_a, tenant_b


def test_tenant_query_never_returns_other_tenants_rows(app_conninfo, two_tenants):
    tenant_a, tenant_b = two_tenants

    with psycopg.connect(app_conninfo) as app:
        # In tenant A's context, every row of every table belongs to A.
        with tenant_tx(app, tenant_a):
            for table in TENANT_TABLES:
                rows = app.execute(f"SELECT tenant_id FROM {table}").fetchall()
                assert rows, f"expected sample rows for {table}"
                assert all(r[0] == tenant_a for r in rows), f"leak in {table}"

        # Symmetric: tenant B sees only B.
        with tenant_tx(app, tenant_b):
            for table in TENANT_TABLES:
                rows = app.execute(f"SELECT tenant_id FROM {table}").fetchall()
                assert rows, f"expected sample rows for {table}"
                assert all(r[0] == tenant_b for r in rows), f"leak in {table}"

        # The tenant table itself is isolated to the current tenant's own row.
        with tenant_tx(app, tenant_a):
            ids = [r[0] for r in app.execute("SELECT id FROM tenant").fetchall()]
            assert ids == [tenant_a]


def test_default_deny_when_no_tenant_set(app_conninfo, two_tenants):
    """With no tenant context, the app role sees nothing — never everything."""
    with psycopg.connect(app_conninfo) as app:
        for table in TENANT_TABLES:
            rows = app.execute(f"SELECT * FROM {table}").fetchall()
            assert rows == [], f"{table} leaked rows with no tenant set"


def test_cannot_write_into_another_tenant(app_conninfo, two_tenants):
    """RLS WITH CHECK blocks inserting a row tagged for a different tenant."""
    tenant_a, tenant_b = two_tenants
    with psycopg.connect(app_conninfo) as app:
        with pytest.raises(psycopg.errors.Error):
            with tenant_tx(app, tenant_a):
                # tenant context is A, but we try to write a row for B
                app.execute(
                    "INSERT INTO feature (tenant_id, name) VALUES (%s, %s)",
                    (tenant_b, "smuggled"),
                )


# ---------------------------------------------------------------------------
# The resolution path.
#
# Every test above connects with `app_conninfo` — the `meter_app` role, spelled
# out. That proves the *policies* work, but it hardcodes the very thing that can
# go wrong in a real deployment: which role the app actually ends up as.
# `app_dsn()` falls back to DATABASE_URL when nothing else is configured, and if
# that URL is the owner or a superuser, RLS never applies and the policies above
# are silently inert. These two tests cover that gap.
# ---------------------------------------------------------------------------


def test_default_connection_is_governed_by_rls(app_env, two_tenants):
    """`db.connect()` with no explicit DSN must land on an RLS-governed role."""
    tenant_a, tenant_b = two_tenants

    with db.connect() as app:
        with tenant_tx(app, tenant_a):
            for table in TENANT_TABLES:
                owners = {r[0] for r in app.execute(f"SELECT tenant_id FROM {table}")}
                assert owners == {tenant_a}, (
                    f"{table} leaked another tenant's rows through app_dsn()"
                )

            # Tenant B exists and has sample rows in every table above, yet is
            # wholly invisible from A's context — including its own tenant row.
            visible = [r[0] for r in app.execute("SELECT id FROM tenant")]
            assert visible == [tenant_a]
            assert tenant_b not in visible

    # Default-deny: with no tenant context at all, the default connection sees
    # nothing — never everything.
    with db.connect() as fresh:
        for table in TENANT_TABLES:
            assert fresh.execute(f"SELECT * FROM {table}").fetchall() == [], (
                f"{table} leaked rows through app_dsn() with no tenant set"
            )


def test_demo_script_points_the_api_at_the_app_role():
    """`make demo` must exercise the same RLS path as production.

    The demo's Postgres comes from `initdb`, so DATABASE_URL is the cluster
    superuser and owner — a role RLS never applies to. Unless the script also
    exports DATABASE_APP_URL, `app_dsn()` falls back to it and the local
    environment a contributor would use to check invariant 6 gives a false pass.
    """
    script = (Path(__file__).resolve().parents[2] / "scripts" / "demo.sh").read_text()

    assert "export DATABASE_APP_URL=" in script, (
        "scripts/demo.sh must export DATABASE_APP_URL, or the demo API connects "
        "as the table owner and tenant isolation is silently inert"
    )
    assert f"user={db.APP_ROLE}" in script, (
        f"scripts/demo.sh must point DATABASE_APP_URL at the {db.APP_ROLE} role"
    )
