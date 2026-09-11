"""Acceptance: the seed script loads — migrations + one demo tenant with data.

Drives seed.main() exactly as the CLI would, pointing DATABASE_URL at the
ephemeral test database.
"""

from __future__ import annotations

import psycopg


def _tenant_id(conn, name):
    return conn.execute("SELECT id FROM tenant WHERE name = %s", (name,)).fetchone()


def test_seed_loads_extended_demo_tenant(postgresql, admin_conninfo, monkeypatch):
    # Point the seed script's admin connection at the throwaway test DB.
    monkeypatch.setenv("DATABASE_URL", admin_conninfo)
    # The prompt demo is encrypted content, so it needs an app key to wrap its
    # data key. The other two tests run without one on purpose.
    monkeypatch.setenv("APP_SECRET_KEY", "unit-test-secret-key")

    import seed

    seed.main()

    with psycopg.connect(admin_conninfo) as conn:  # admin bypasses RLS
        tenant = _tenant_id(conn, seed.DEMO_TENANT_NAME)
        assert tenant is not None, "demo tenant was not created"

        # Extended demo: 4 base + 4 new + 1 hosted-OSS + 1 self-hosted + 3 non-AI.
        feature_count = conn.execute(
            "SELECT count(*) FROM feature WHERE tenant_id = %s", (tenant[0],)
        ).fetchone()[0]
        assert feature_count == 13

        # ~2 years of monthly history -> many distinct periods, spanning 2024-2026.
        periods = conn.execute(
            "SELECT min(period), max(period), count(DISTINCT period) "
            "FROM inference_cost WHERE tenant_id = %s",
            (tenant[0],),
        ).fetchone()
        assert periods[0].year == 2024
        assert periods[1].year == 2026
        assert periods[2] >= 12  # at least a year's worth of distinct months

        # Unattributed spend landed in the bucket (feature_id NULL), not dropped.
        unattributed = conn.execute(
            "SELECT count(*) FROM inference_cost WHERE tenant_id = %s AND feature_id IS NULL",
            (tenant[0],),
        ).fetchone()[0]
        assert unattributed >= 1

        # Open-source coverage: a self-hosted GPU pool with allocated (self_host)
        # inference cost, plus a fine-tuning run recorded as build cost.
        assert (
            conn.execute(
                "SELECT count(*) FROM compute_pool WHERE tenant_id = %s", (tenant[0],)
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM inference_cost WHERE tenant_id = %s AND source = 'self_host'",
                (tenant[0],),
            ).fetchone()[0]
            >= 1
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM build_cost WHERE tenant_id = %s AND tool = 'fine_tune'",
                (tenant[0],),
            ).fetchone()[0]
            == 1
        )

        # Consented prompt capture, seeded whole: the demo has no SDK and no
        # provider key, so without this Optimize -> Prompts is an empty screen.
        assert (
            conn.execute(
                "SELECT count(*) FROM prompt_sample WHERE tenant_id = %s", (tenant[0],)
            ).fetchone()[0]
            >= 20  # prompt_optimize.MIN_SAMPLES, or no rewrite can be offered
        )
        # A rewrite is only 'recommended' because an evaluation said so.
        assert (
            conn.execute(
                "SELECT status FROM prompt_candidate WHERE tenant_id = %s", (tenant[0],)
            ).fetchone()[0]
            == "recommended"
        )
        run = conn.execute(
            "SELECT id, status, decision, cases_done FROM prompt_evaluation WHERE tenant_id = %s",
            (tenant[0],),
        ).fetchone()
        assert run[1:] == ("completed", "recommended", 24)
        cases = conn.execute(
            "SELECT count(*) FROM prompt_evaluation_case WHERE evaluation_id = %s", (run[0],)
        ).fetchone()[0]
        assert cases == run[3]  # every case it claims to have run is there to read


def test_seed_is_idempotent_without_reset(postgresql, admin_conninfo, monkeypatch):
    # No APP_SECRET_KEY on purpose: seeding a demo must still load without one.
    monkeypatch.setenv("DATABASE_URL", admin_conninfo)
    import seed

    seed.main()
    with psycopg.connect(admin_conninfo) as conn:
        first = _tenant_id(conn, seed.DEMO_TENANT_NAME)[0]

    seed.main()  # second run is a no-op (demo user already exists)
    with psycopg.connect(admin_conninfo) as conn:
        tenants = conn.execute(
            "SELECT count(*) FROM tenant WHERE name = %s", (seed.DEMO_TENANT_NAME,)
        ).fetchone()[0]
        assert tenants == 1
        still = _tenant_id(conn, seed.DEMO_TENANT_NAME)[0]
        assert still == first  # same tenant, untouched


def test_seed_reset_rebuilds_demo_tenant(postgresql, admin_conninfo, monkeypatch):
    # Also keyless, so the seed's no-key path stays exercised.
    monkeypatch.setenv("DATABASE_URL", admin_conninfo)
    import seed

    seed.main()
    with psycopg.connect(admin_conninfo) as conn:
        first = _tenant_id(conn, seed.DEMO_TENANT_NAME)[0]

    seed.main(reset=True)  # wipe + rebuild
    with psycopg.connect(admin_conninfo) as conn:
        # Exactly one demo tenant remains, and it's a fresh one (old data cascaded away).
        tenants = conn.execute(
            "SELECT count(*) FROM tenant WHERE name = %s", (seed.DEMO_TENANT_NAME,)
        ).fetchone()[0]
        assert tenants == 1
        rebuilt = _tenant_id(conn, seed.DEMO_TENANT_NAME)[0]
        assert rebuilt != first  # new tenant id

        # The old tenant's rows are gone (cascade), the new one's are present.
        orphans = conn.execute(
            "SELECT count(*) FROM feature WHERE tenant_id = %s", (first,)
        ).fetchone()[0]
        assert orphans == 0
        features = conn.execute(
            "SELECT count(*) FROM feature WHERE tenant_id = %s", (rebuilt,)
        ).fetchone()[0]
        assert features == 13
