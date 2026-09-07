-- Meter M1 — tenant isolation via Postgres Row-Level Security (RLS).
--
-- WHY RLS (and not just "filter by tenant_id in every query"):
--   Invariant 6 says per-tenant isolation must be *enforced*. RLS pushes the
--   guarantee into the database itself: even a buggy query that forgets a
--   WHERE clause cannot return another tenant's rows. Defense in depth for a
--   product sold to security teams.
--
-- HOW IT WORKS:
--   * The app connects as a dedicated, NON-privileged role (meter_app).
--     Superusers/owners bypass RLS; this role does not — so policies bite.
--   * Each request sets a transaction-local variable `app.current_tenant`
--     (see backend/meter/db.py: tenant_tx). Policies compare it to the
--     row's tenant_id.
--   * With no tenant set, current_setting(..., true) is NULL -> the policy
--     matches no rows. Default-deny: forget to set the tenant and you see
--     nothing, never everything.
--   * Migrations and seeding run as the bootstrap/superuser role, which
--     bypasses RLS — that is how we load multiple tenants' data.

-- ---------------------------------------------------------------------------
-- Application role (idempotent — roles are cluster-global).
-- LOGIN with no password is fine for local/CI (trust auth); in production this
-- role is given a password / managed credential. It is intentionally minimal:
-- no superuser, no BYPASSRLS, no schema ownership.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'meter_app') THEN
        CREATE ROLE meter_app LOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO meter_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON
    tenant, feature, feature_signal, build_cost,
    inference_cost, bill_reconciliation, feature_usage
TO meter_app;

-- ---------------------------------------------------------------------------
-- Enable + FORCE RLS and add a tenant-isolation policy on every tenant table.
-- FORCE so the guarantee holds even if the app role ever owns a table.
-- The `tenant` table keys on id; all others key on tenant_id.
-- ---------------------------------------------------------------------------

-- tenant: a tenant can see/act on only its own row.
ALTER TABLE tenant ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON tenant
    USING (id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);

-- feature
ALTER TABLE feature ENABLE ROW LEVEL SECURITY;
ALTER TABLE feature FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON feature
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);

-- feature_signal
ALTER TABLE feature_signal ENABLE ROW LEVEL SECURITY;
ALTER TABLE feature_signal FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON feature_signal
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);

-- build_cost
ALTER TABLE build_cost ENABLE ROW LEVEL SECURITY;
ALTER TABLE build_cost FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON build_cost
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);

-- inference_cost
ALTER TABLE inference_cost ENABLE ROW LEVEL SECURITY;
ALTER TABLE inference_cost FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON inference_cost
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);

-- bill_reconciliation
ALTER TABLE bill_reconciliation ENABLE ROW LEVEL SECURITY;
ALTER TABLE bill_reconciliation FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON bill_reconciliation
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);

-- feature_usage
ALTER TABLE feature_usage ENABLE ROW LEVEL SECURITY;
ALTER TABLE feature_usage FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON feature_usage
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);
