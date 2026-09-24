"""The MCP server's database role: what it can read, and what it cannot do.

`meter_read` (migration 0060) is what turns "this server does not write" into
"this server cannot write". These tests are the reason the grant list in that
migration can be trusted: one runs every tool as the role, so a tool that needs
a table nobody granted fails here rather than in a customer's terminal; the rest
assert the things the role must never be able to do.
"""

from __future__ import annotations

import datetime as dt

import psycopg
import pytest
from meter import db
from meter.mcp import tools
from meter.sampledata import insert_sample_data

PERIOD = dt.date(2026, 5, 1)
MONTH = "2026-05"


@pytest.fixture
def seeded(tenant_id, app_env):
    insert_sample_data(app_env, tenant_id, extended=True)
    app_env.commit()
    return tenant_id


@pytest.fixture
def as_read_role(monkeypatch, read_conninfo):
    """Make every service connection in this test use the SELECT-only role,
    exactly as `python -m meter.mcp` does at startup."""
    monkeypatch.setattr(db, "_read_only", True)
    monkeypatch.setenv("DATABASE_READ_URL", read_conninfo)
    assert db.app_dsn() == read_conninfo


@pytest.fixture
def read_conn(app_env, read_conninfo):
    # app_env first: it applies the migrations that create the role.
    with psycopg.connect(read_conninfo) as conn:
        yield conn


# ---------------------------------------------------------------------------
# Every tool works under the role — which is what keeps the grant list honest
# ---------------------------------------------------------------------------
def test_every_tool_runs_as_the_read_only_role(seeded, as_read_role):
    # A missing GRANT shows up here as InsufficientPrivilege, naming the table.
    for grouping in tools.GROUPINGS:
        tools.call_tool("get_cost_summary", {"start": MONTH, "group_by": grouping}, seeded)

    found = tools.call_tool("find_optimization_opportunities", {"period": MONTH}, seeded)
    assert found["opportunities"], "the seeded tenant has findings to read"

    first = found["opportunities"][0]
    tools.call_tool(
        "find_optimization_opportunities",
        {"period": MONTH, "feature_id": first["feature_id"]},
        seeded,
    )
    detail = tools.call_tool(
        "get_optimization_details", {"opportunity_id": first["opportunity_id"]}, seeded
    )
    assert detail["opportunity"]["lever"] == first["lever"]


def test_the_numbers_are_the_same_as_the_app_sees(seeded, as_read_role, read_conninfo):
    # A narrower role must not mean narrower data: a column the role cannot read
    # would silently change a total rather than raise.
    from meter import dashboard

    under_read = tools.call_tool("get_cost_summary", {"start": MONTH}, seeded)
    db._read_only = False
    try:
        expected = dashboard.dashboard(seeded, PERIOD)
    finally:
        db._read_only = True
    assert under_read["totals"] == expected["totals"]
    assert under_read["feature_count"] == len(expected["features"])


# ---------------------------------------------------------------------------
# What the role cannot do
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "statement",
    [
        # Deliberately a row RLS would ALLOW: a foreign tenant_id fails the
        # policy check with the same SQLSTATE as a missing grant, which would
        # make this pass whether or not the role could write.
        "INSERT INTO feature (tenant_id, name) "
        "SELECT current_setting('app.current_tenant')::uuid, 'x'",
        "UPDATE inference_cost SET amount = 0",
        "DELETE FROM usage_signal",
        "UPDATE optimization_action SET lever = 'x'",
        "DELETE FROM ai_trace",
    ],
)
def test_it_cannot_write(read_conn, tenant_id, statement):
    with read_conn.transaction():
        read_conn.execute("SELECT set_config('app.current_tenant', %s, true)", (str(tenant_id),))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            read_conn.execute(statement)


@pytest.mark.parametrize(
    "table",
    [
        "app_user",  # password hashes
        "hook_token",  # the SDK's ingest credential
        "mcp_token",  # its own credential, and everyone else's
    ],
)
def test_it_cannot_read_a_credential(read_conn, table):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        read_conn.execute(f"SELECT * FROM {table}")  # noqa: S608 — a test parameter


def test_it_can_see_that_a_connector_exists_but_not_what_is_in_it(read_conn, tenant_id):
    # The Overview asks EXISTS(...) and nothing more, so the role gets the
    # columns that answer that and not the encrypted credential beside them.
    read_conn.execute("SELECT set_config('app.current_tenant', %s, true)", (str(tenant_id),))
    read_conn.execute("SELECT EXISTS (SELECT 1 FROM connector_credential)")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        read_conn.execute("SELECT ciphertext FROM connector_credential")


def test_tenant_isolation_still_applies_to_it(read_conn, app_env, tenant_id):
    other = str(
        app_env.execute("INSERT INTO tenant (name) VALUES ('Other') RETURNING id").fetchone()[0]
    )
    insert_sample_data(app_env, other)
    app_env.commit()

    read_conn.execute("SELECT set_config('app.current_tenant', %s, true)", (str(tenant_id),))
    visible = read_conn.execute("SELECT count(*) FROM feature").fetchone()[0]
    assert visible == 0, "this tenant was seeded with nothing"

    read_conn.execute("SELECT set_config('app.current_tenant', %s, true)", (other,))
    assert read_conn.execute("SELECT count(*) FROM feature").fetchone()[0] > 0


def test_with_no_tenant_set_it_sees_nothing(read_conn, app_env, tenant_id):
    insert_sample_data(app_env, tenant_id)
    app_env.commit()
    # Default deny: forgetting the tenant shows you nothing, never everything.
    assert read_conn.execute("SELECT count(*) FROM feature").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# read_only() is a property of the process
# ---------------------------------------------------------------------------
def test_read_only_is_one_way(app_env, monkeypatch, read_conninfo):
    monkeypatch.setenv("DATABASE_READ_URL", read_conninfo)
    monkeypatch.setattr(db, "_read_only", False)
    assert db.app_dsn() != read_conninfo
    db.read_only()
    try:
        assert db.is_read_only()
        assert db.app_dsn() == read_conninfo
        # There is no way back — no read_write(), by design.
        assert not hasattr(db, "read_write")
    finally:
        db._read_only = False


def test_it_refuses_to_guess_a_read_credential(app_env, monkeypatch):
    monkeypatch.delenv("DATABASE_READ_URL", raising=False)
    monkeypatch.delenv("METER_READ_DB_PASSWORD", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://someone@localhost/meter")
    # Never fall back to a role that can write.
    with pytest.raises(RuntimeError, match="Refusing to fall back"):
        db.read_dsn()
