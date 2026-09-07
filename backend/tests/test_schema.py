"""Schema-shape assertions for M1 acceptance.

Confirms migrations applied cleanly (via the admin_conn fixture) and that the
two fields the design doc calls out by name exist as designed:
  * inference_cost.source  (cost_api | hook — makes the model hook-ready)
  * feature.discovery_confidence
"""

from __future__ import annotations

ALL_TABLES = [
    "tenant",
    "feature",
    "feature_signal",
    "build_cost",
    "inference_cost",
    "bill_reconciliation",
    "feature_usage",
]


def _column_exists(conn, table: str, column: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
        """,
        (table, column),
    ).fetchone()
    return row is not None


def test_all_tables_exist(admin_conn):
    for table in ALL_TABLES:
        assert _column_exists(admin_conn, table, "id"), f"missing table {table}"


def test_inference_cost_source_exists(admin_conn):
    assert _column_exists(admin_conn, "inference_cost", "source")


def test_feature_discovery_confidence_exists(admin_conn):
    assert _column_exists(admin_conn, "feature", "discovery_confidence")


def test_every_tenant_table_has_tenant_id(admin_conn):
    for table in ALL_TABLES:
        if table == "tenant":
            continue  # the anchor keys on `id`
        assert _column_exists(admin_conn, table, "tenant_id"), f"{table} lacks tenant_id"


def test_rls_enabled_on_tenant_tables(admin_conn):
    # RLS is enabled on every tenant table. (FORCE is intentionally relaxed in
    # migration 0006 so the admin/owner role can run auth+migrations on a managed
    # Postgres without a superuser; the app role is a non-owner, so policies still
    # apply to it — see test_tenant_isolation.py.)
    rows = admin_conn.execute(
        "SELECT relname FROM pg_class WHERE relname = ANY(%s) AND relrowsecurity",
        (ALL_TABLES,),
    ).fetchall()
    secured = {r[0] for r in rows}
    assert secured == set(ALL_TABLES), f"RLS not enabled on: {set(ALL_TABLES) - secured}"


def test_migrations_are_idempotent(admin_conn, admin_conninfo):
    """Re-running apply_migrations on an already-migrated DB is a no-op."""
    from meter.migrations import apply_migrations

    again = apply_migrations(admin_conninfo)
    assert again == []
