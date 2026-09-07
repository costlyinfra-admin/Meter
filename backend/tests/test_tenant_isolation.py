"""The M1 acceptance test: a query for tenant A never returns tenant B's rows.

Two tenants are seeded by the bootstrap role (which bypasses RLS). All
cross-tenant assertions run as the non-privileged app role, where Row-Level
Security is in force.
"""

from __future__ import annotations

import psycopg
import pytest
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
