"""Database access helpers for Meter.

Two connection roles:
  * **bootstrap/admin** — the role that owns the schema (a superuser locally).
    Used by migrations and seeding. Bypasses RLS, so it can load many tenants.
  * **app** (`meter_app`) — the non-privileged role the running app uses.
    RLS policies apply to it; every request must set the tenant context.

The tenant context is a transaction-local Postgres setting, `app.current_tenant`.
`tenant_tx()` sets it and runs your work inside one transaction so it can never
leak across pooled/serverless connections.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from glob import glob

import psycopg
import psycopg.conninfo

#: The non-privileged role the application connects as (see migration 0002).
APP_ROLE = "meter_app"

#: The SELECT-only role (migration 0060). Same RLS policies as APP_ROLE — it is
#: a non-owner too — but no INSERT, UPDATE or DELETE anywhere, and no privilege
#: at all on credentials. Used by the MCP server, which must only read.
READ_ROLE = "meter_read"

#: Transaction-local Postgres setting that drives the RLS policies.
TENANT_GUC = "app.current_tenant"


def admin_dsn() -> str:
    """Connection string for the bootstrap/admin role (migrations, seed)."""
    return os.environ.get("DATABASE_ADMIN_URL") or os.environ["DATABASE_URL"]


def app_dsn() -> str:
    """Connection string for the non-privileged application role.

    Resolution order:
      0. The read-only role, if this process called read_only().
      1. DATABASE_APP_URL if set (explicit, used by tests and advanced setups).
      2. Otherwise, if METER_APP_DB_PASSWORD is set, derive from DATABASE_URL
         by swapping in the `meter_app` role + that password. This is the
         production default: you set one DB URL and one app-role password, and
         the app connects as the RLS-enforced role automatically.
      3. Otherwise fall back to DATABASE_URL (fine for single-user local dev).
    """
    if _read_only:
        return read_dsn()
    explicit = os.environ.get("DATABASE_APP_URL")
    if explicit:
        return explicit
    app_password = os.environ.get("METER_APP_DB_PASSWORD")
    if app_password:
        params = psycopg.conninfo.conninfo_to_dict(os.environ["DATABASE_URL"])
        params["user"] = APP_ROLE
        params["password"] = app_password
        return psycopg.conninfo.make_conninfo(**params)
    return os.environ["DATABASE_URL"]


#: Set once, at startup, by a process that must never write (see read_only()).
_read_only = False


def read_only() -> None:
    """Make every application connection in THIS process read-only.

    Called by `python -m meter.mcp` before it serves anything. The alternative
    was threading a DSN through `dashboard`, `optimize_measured` and `ai_reads`
    and every function they call, so that one caller could ask for a different
    role — which would put a "which role am I?" argument in the signature of
    code that has no business knowing. The property belongs to the process, so
    it is set on the process.

    One-way on purpose: there is no matching `read_write()`. A process that has
    declared itself read-only cannot talk itself back out of it.
    """
    global _read_only
    _read_only = True


def is_read_only() -> bool:
    return _read_only


def read_dsn() -> str:
    """Connection string for the SELECT-only role.

    Resolved like `app_dsn()`: an explicit DATABASE_READ_URL wins, otherwise
    METER_READ_DB_PASSWORD swaps the role into DATABASE_URL. With neither set
    there is no fallback to a writable role — a process that asked for
    read-only gets an error rather than a connection that can write.
    """
    explicit = os.environ.get("DATABASE_READ_URL")
    if explicit:
        return explicit
    password = os.environ.get("METER_READ_DB_PASSWORD")
    if not password:
        raise RuntimeError(
            "Read-only database access needs DATABASE_READ_URL, or "
            "METER_READ_DB_PASSWORD alongside DATABASE_URL. Refusing to fall "
            f"back to a role that can write. See migration 0060 for {READ_ROLE}."
        )
    params = psycopg.conninfo.conninfo_to_dict(os.environ["DATABASE_URL"])
    params["user"] = READ_ROLE
    params["password"] = password
    return psycopg.conninfo.make_conninfo(**params)


def connect(conninfo: str | None = None, *, autocommit: bool = False) -> psycopg.Connection:
    """Open a psycopg connection. Defaults to the app DSN from the environment."""
    return psycopg.connect(conninfo or app_dsn(), autocommit=autocommit)


@contextmanager
def tenant_tx(conn: psycopg.Connection, tenant_id) -> Iterator[psycopg.Connection]:
    """Run a block inside one transaction scoped to a single tenant.

    Sets `app.current_tenant` transaction-locally (via set_config(..., is_local=true)),
    so RLS filters every statement to ``tenant_id``. Commits on success, rolls
    back on error, and the setting is discarded with the transaction either way.
    """
    with conn.transaction():
        conn.execute("SELECT set_config(%s, %s, true)", (TENANT_GUC, str(tenant_id)))
        yield conn


def find_pg_binary(name: str) -> str:
    """Locate a Postgres CLI binary (e.g. ``psql``), tolerating keg-only installs.

    Checks PATH first, then common Homebrew (macOS) and apt (Linux/CI) locations.
    """
    found = shutil.which(name)
    if found:
        return found
    patterns = [
        f"/opt/homebrew/opt/postgresql@*/bin/{name}",
        f"/usr/local/opt/postgresql@*/bin/{name}",
        f"/usr/lib/postgresql/*/bin/{name}",
        f"/Applications/Postgres.app/Contents/Versions/*/bin/{name}",
    ]
    for pattern in patterns:
        matches = sorted(glob(pattern))
        if matches:
            return matches[-1]  # highest version
    raise FileNotFoundError(
        f"Could not find the Postgres binary '{name}'. Install Postgres (e.g. "
        f"`brew install postgresql@16`) or put it on PATH."
    )
