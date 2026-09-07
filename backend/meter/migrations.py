"""A tiny, transparent migration runner.

Applies the ``*.sql`` files in ``backend/migrations/`` in filename order, once
each, recording applied versions in a ``schema_migrations`` table.

We apply files with ``psql --single-transaction`` rather than splitting SQL in
Python: psql handles dollar-quoted blocks (the DO $$ ... $$ role block) and
multi-statement scripts correctly, and each file plus its bookkeeping INSERT
runs in one atomic transaction. Runtime app queries use psycopg (see db.py);
only schema changes go through psql.

``schema_migrations`` deliberately has no tenant_id and no RLS: it is operational
metadata touched only by the bootstrap/admin role, not tenant data.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import psycopg

from .db import admin_dsn, find_pg_binary

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

_TRACKING_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    "version text PRIMARY KEY, "
    "applied_at timestamptz NOT NULL DEFAULT now())"
)


def _applied_versions(conninfo: str) -> set[str]:
    with psycopg.connect(conninfo) as conn:
        with conn.cursor() as cur:
            cur.execute(_TRACKING_TABLE_SQL)
            conn.commit()
            cur.execute("SELECT version FROM schema_migrations")
            return {row[0] for row in cur.fetchall()}


def _run_psql_file(psql: str, conninfo: str, path: Path, version: str) -> None:
    subprocess.run(
        [
            psql,
            "--quiet",
            "--no-psqlrc",
            "-v",
            "ON_ERROR_STOP=1",
            "--single-transaction",
            "-d",
            conninfo,
            "-f",
            str(path),
            "-c",
            f"INSERT INTO schema_migrations (version) VALUES ({_sql_literal(version)})",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _sql_literal(value: str) -> str:
    # Migration versions are filenames we control (no quotes); still, escape
    # defensively for the single inlined value psql receives.
    return "'" + value.replace("'", "''") + "'"


def apply_migrations(conninfo: str | None = None) -> list[str]:
    """Apply all pending migrations. Returns the versions newly applied (in order)."""
    conninfo = conninfo or admin_dsn()
    psql = find_pg_binary("psql")
    applied = _applied_versions(conninfo)

    newly_applied: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = path.stem
        if version in applied:
            continue
        try:
            _run_psql_file(psql, conninfo, path, version)
        except subprocess.CalledProcessError as exc:  # surface the real SQL error
            raise RuntimeError(f"Migration {version} failed:\n{exc.stderr or exc.stdout}") from exc
        newly_applied.append(version)
    return newly_applied


if __name__ == "__main__":
    done = apply_migrations()
    if done:
        print("Applied migrations:", ", ".join(done))
    else:
        print("No pending migrations; schema is up to date.")
