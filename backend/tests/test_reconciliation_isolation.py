"""Regression guards: reconciliation must be invisible to everything else.

These are the tests that would fail if the module ever stopped being additive —
if it wrote to a cost table, changed a total, or became something an existing
query depends on.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest
from meter import dashboard
from meter.reconciliation import engine, flag, imports

MAY = dt.date(2026, 5, 1)
SOURCE = Path(__file__).resolve().parent.parent / "meter"

STATEMENT = (
    "date,model,category,cost,currency\n"
    "2026-05-01,claude-sonnet-4-6,usage,283.00,USD\n"
    "2026-05-01,,tax,22.64,USD\n"
)


def _seed_tracked(app_env, tenant_id: str) -> None:
    app_env.execute(
        """
        INSERT INTO inference_cost_daily
            (tenant_id, provider, model, amount, day, source, confidence)
        VALUES (%s, 'anthropic', 'claude-sonnet-4-6', 100, %s, 'cost_api', 'high')
        """,
        (tenant_id, MAY),
    )
    app_env.execute(
        """
        INSERT INTO inference_cost (tenant_id, provider, model, amount, period, source, confidence)
        VALUES (%s, 'anthropic', 'claude-sonnet-4-6', 100, %s, 'cost_api', 'high')
        """,
        (tenant_id, MAY),
    )
    app_env.commit()


def _snapshot(app_env) -> dict:
    """Everything an existing screen reads, as one comparable value."""

    def scalar(sql: str):
        return app_env.execute(sql).fetchone()

    return {
        "daily": scalar("SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM inference_cost_daily"),
        "monthly": scalar("SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM inference_cost"),
        "features": scalar("SELECT COUNT(*) FROM feature"),
        "build": scalar("SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM build_cost"),
        "signals": scalar("SELECT COUNT(*) FROM feature_signal"),
        "bill_recon": scalar("SELECT COUNT(*) FROM bill_reconciliation"),
        "alerts": scalar("SELECT COUNT(*) FROM alert_rule"),
    }


def test_a_full_reconciliation_changes_no_existing_row(app_env, tenant_id):
    _seed_tracked(app_env, tenant_id)
    before = _snapshot(app_env)

    flag.update(tenant_id, enabled=True, actor="a@b.com")
    imported = imports.commit(
        tenant_id, provider="anthropic", filename="may.csv", content=STATEMENT, actor="a@b.com"
    )
    run = engine.calculate(tenant_id, import_id=imported["id"], actor="a@b.com")
    # A real discrepancy — the case most likely to tempt a module into "fixing" it.
    assert run["status"] == "discrepancy" and run["usage_difference"] == 183.0

    assert _snapshot(app_env) == before


def test_the_overview_reads_the_same_numbers_either_way(app_env, tenant_id):
    _seed_tracked(app_env, tenant_id)
    before = dashboard.dashboard(tenant_id)

    flag.update(tenant_id, enabled=True, actor="a@b.com")
    imported = imports.commit(
        tenant_id, provider="anthropic", filename="may.csv", content=STATEMENT, actor="a@b.com"
    )
    engine.calculate(tenant_id, import_id=imported["id"], actor="a@b.com")

    assert dashboard.dashboard(tenant_id) == before


def test_turning_the_module_off_again_changes_nothing(app_env, tenant_id):
    _seed_tracked(app_env, tenant_id)
    flag.update(tenant_id, enabled=True, actor="a@b.com")
    imported = imports.commit(
        tenant_id, provider="anthropic", filename="may.csv", content=STATEMENT, actor="a@b.com"
    )
    engine.calculate(tenant_id, import_id=imported["id"], actor="a@b.com")
    snapshot = _snapshot(app_env)
    overview = dashboard.dashboard(tenant_id)

    flag.update(tenant_id, enabled=False, actor="a@b.com")

    assert _snapshot(app_env) == snapshot
    assert dashboard.dashboard(tenant_id) == overview


def test_an_installation_that_never_configures_it_is_unaffected(app_env, tenant_id):
    # No recon_settings row exists at all: the module is off, and asking is safe.
    _seed_tracked(app_env, tenant_id)
    assert flag.is_enabled(tenant_id) is False
    assert dashboard.dashboard(tenant_id)["totals"]["inference_cost"] == 100.0
    assert app_env.execute("SELECT COUNT(*) FROM recon_settings").fetchone()[0] == 0


#: An import of the reconciliation package, however it is spelled.
_IMPORTS_MODULE = re.compile(
    r"^\s*(from\s+\.?\S*reconciliation|import\s+\S*reconciliation)", re.MULTILINE
)
#: A statement that would change data.
_WRITE = re.compile(
    r"\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|DROP\s+|TRUNCATE)\b", re.IGNORECASE
)


def test_only_api_py_imports_the_reconciliation_package():
    # The dependency runs one way. If this fails, something outside the module
    # has taken a dependency on it and the isolation is gone.
    offenders = [
        path.name
        for path in SOURCE.glob("*.py")
        if path.name != "api.py" and _IMPORTS_MODULE.search(path.read_text())
    ]
    assert offenders == [], f"reconciliation is imported by {offenders}"


def test_api_registers_the_router_and_does_nothing_else_with_it():
    lines = [
        line.strip()
        for line in (SOURCE / "api.py").read_text().splitlines()
        if "reconciliation_api" in line and not line.strip().startswith("#")
    ]
    assert lines == [
        "from .reconciliation import api as reconciliation_api",
        "app.include_router(reconciliation_api.build_router(_current_user))",
    ]


def test_no_existing_query_reads_a_reconciliation_table():
    offenders = [p.name for p in SOURCE.glob("*.py") if "recon_" in p.read_text()]
    assert offenders == [], f"recon_ tables are read by {offenders}"


def test_the_boundary_onto_existing_cost_data_only_reads():
    """Enforced by reading the boundary's own source, with prose stripped out."""
    text = (SOURCE / "reconciliation" / "tracked.py").read_text()
    code = re.sub(r'"""[\s\S]*?"""', "", text)  # docstrings
    code = re.sub(r"#.*", "", code)  # comments
    assert not _WRITE.search(code), "tracked.py must only ever SELECT"

    # And no other file in the module names an existing cost table at all, so
    # there is no second, unreviewed path to one.
    for path in (SOURCE / "reconciliation").glob("*.py"):
        if path.name == "tracked.py":
            continue
        body = re.sub(r'"""[\s\S]*?"""', "", path.read_text())
        for table in ("inference_cost", "build_cost", "feature_signal", "bill_reconciliation"):
            assert table not in body, f"{path.name} names {table}"


@pytest.mark.parametrize(
    "table",
    [
        "recon_settings",
        "recon_import",
        "recon_line_item",
        "recon_run",
        "recon_match",
        "recon_audit",
    ],
)
def test_every_reconciliation_table_enforces_tenant_isolation(app_env, table):
    row = app_env.execute(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s", (table,)
    ).fetchone()
    assert row == (True, True), f"{table} must have RLS enabled AND forced"
    policies = app_env.execute(
        "SELECT COUNT(*) FROM pg_policies WHERE tablename = %s", (table,)
    ).fetchone()[0]
    assert policies >= 1
