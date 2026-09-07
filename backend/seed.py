"""Seed script — one demo tenant with sample data, so the UI has something to render.

Usage (with DATABASE_URL set to a Postgres instance):

    python -m seed            # apply migrations, then seed the demo tenant
    python -m seed --reset    # wipe the existing demo tenant first, then re-seed

Credentials are configurable via env (defaults shown):
    DEMO_TENANT_NAME="Acme Security"
    DEMO_USER_EMAIL="demo@costlyinfra.com"
    DEMO_USER_PASSWORD="meter-demo"

Runs as the bootstrap/admin role. Without --reset it is idempotent: if the demo
user already exists, it does nothing. With --reset (or DEMO_RESET=1) it deletes
the existing demo tenant — and, via ON DELETE CASCADE, all of its data — then
seeds a fresh copy. Reset is scoped strictly to the demo tenant; no other tenant
is touched.
"""

from __future__ import annotations

import argparse
import os

from meter.auth import hash_password
from meter.db import admin_dsn, connect
from meter.migrations import apply_migrations
from meter.sampledata import create_tenant, insert_sample_data

DEMO_TENANT_NAME = os.environ.get("DEMO_TENANT_NAME", "Acme Security")
DEMO_USER_EMAIL = os.environ.get("DEMO_USER_EMAIL", "demo@costlyinfra.com").strip().lower()
DEMO_USER_PASSWORD = os.environ.get("DEMO_USER_PASSWORD", "meter-demo")


def main(reset: bool = False) -> None:
    applied = apply_migrations()
    if applied:
        print("Applied migrations:", ", ".join(applied))

    with connect(admin_dsn()) as conn:
        existing = conn.execute(
            "SELECT tenant_id FROM app_user WHERE email = %s", (DEMO_USER_EMAIL,)
        ).fetchone()
        if existing:
            if not reset:
                print(f"Demo user {DEMO_USER_EMAIL!r} already exists — nothing to seed.")
                print("Re-run with --reset to wipe and rebuild the demo tenant.")
                return
            with conn.transaction():
                # Deleting the tenant cascades to every tenant-scoped table
                # (feature, costs, usage, signals, app_user, hook_token, ...).
                conn.execute("DELETE FROM tenant WHERE id = %s", (existing[0],))
            print(f"Reset: removed existing demo tenant {existing[0]} and all its data.")

        with conn.transaction():
            tenant_id = create_tenant(conn, DEMO_TENANT_NAME)
            summary = insert_sample_data(conn, tenant_id, extended=True)
            # A login for the demo tenant so the seeded data is viewable in the UI.
            conn.execute(
                "INSERT INTO app_user (tenant_id, email, password_hash) VALUES (%s, %s, %s)",
                (tenant_id, DEMO_USER_EMAIL, hash_password(DEMO_USER_PASSWORD)),
            )

    print(
        f"Seeded tenant {DEMO_TENANT_NAME!r} ({tenant_id}) "
        f"with {summary['features']} features and ~2 years of build/inference history."
    )
    print(f"Demo login: {DEMO_USER_EMAIL} / {DEMO_USER_PASSWORD}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed (or reset) the demo tenant.")
    parser.add_argument(
        "--reset",
        action="store_true",
        default=os.environ.get("DEMO_RESET", "").lower() in ("1", "true", "yes"),
        help="Wipe the existing demo tenant before seeding (default from DEMO_RESET).",
    )
    args = parser.parse_args()
    main(reset=args.reset)
