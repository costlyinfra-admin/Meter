-- Meter — relax FORCE RLS for managed-Postgres compatibility.
--
-- We originally FORCEd RLS so even a table's owner is subject to its policies.
-- On a managed Postgres (e.g. Neon) the bootstrap/admin path has no SUPERUSER to
-- bypass RLS for auth/migrations/seeding. The portable equivalent is the standard
-- "a table's OWNER is exempt from its RLS" rule — so we drop FORCE.
--
-- Result:
--   * admin DSN (the database owner) is exempt -> runs auth + migrations + seed,
--   * app role `meter_app` is a NON-owner -> STILL fully governed by the
--     tenant-isolation policies.
-- Application-level per-tenant isolation is unchanged; only the trusted admin
-- role's exemption mechanism changes (superuser-bypass -> owner-exemption).

ALTER TABLE tenant NO FORCE ROW LEVEL SECURITY;
ALTER TABLE feature NO FORCE ROW LEVEL SECURITY;
ALTER TABLE feature_signal NO FORCE ROW LEVEL SECURITY;
ALTER TABLE build_cost NO FORCE ROW LEVEL SECURITY;
ALTER TABLE inference_cost NO FORCE ROW LEVEL SECURITY;
ALTER TABLE bill_reconciliation NO FORCE ROW LEVEL SECURITY;
ALTER TABLE feature_usage NO FORCE ROW LEVEL SECURITY;
ALTER TABLE app_user NO FORCE ROW LEVEL SECURITY;
ALTER TABLE connector_credential NO FORCE ROW LEVEL SECURITY;
ALTER TABLE hook_token NO FORCE ROW LEVEL SECURITY;
