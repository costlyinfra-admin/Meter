-- 0041: an organization's AI budget, and the demo tenant's fixed "as of" date.
--
-- Two unrelated-looking things in one migration because they exist for the same
-- feature: the Overview's Budget & forecast card. Before this, both the budget
-- and the forecast were invented in the frontend.
--
--   org_budget       — the real, persisted budget. One per organization: a
--                      monthly or annual amount in the org's currency, effective
--                      from a date. Removing the budget deletes the row, and the
--                      product then says it has no budget rather than guessing
--                      one. Tenant-scoped and RLS-isolated like every other
--                      tenant table.
--
--   tenant.demo_as_of— NULL for every real organization, which is what makes the
--                      forecast read the clock. The seeded demo tenant sets it to
--                      a date inside its own historical dataset so that "how far
--                      through the month are we" has an answer there. A production
--                      tenant can never pick up a demo value by accident: the
--                      column is NULL and the code falls through to the clock.

CREATE TABLE org_budget (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- One budget per organization. A second one is a schema error, not a race.
    tenant_id      uuid NOT NULL UNIQUE REFERENCES tenant(id) ON DELETE CASCADE,
    amount         numeric(16, 4) NOT NULL CHECK (amount > 0),
    cadence        text NOT NULL CHECK (cadence IN ('monthly', 'annual')),
    -- Denormalized from tenant.currency on write, so a budget always records the
    -- currency it was set in even if the org's reporting currency later changes.
    currency       text NOT NULL,
    effective_from date NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    updated_by     text
);

GRANT SELECT, INSERT, UPDATE, DELETE ON org_budget TO meter_app;

ALTER TABLE org_budget ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON org_budget
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);

-- The demo's "today". NULL everywhere else, which is the production path.
ALTER TABLE tenant ADD COLUMN demo_as_of date;

COMMENT ON COLUMN tenant.demo_as_of IS
    'Demo tenants only: the fixed date to treat as today, so a historical dataset '
    'has an open month. NULL (every real organization) means use the real clock.';
