-- 0022: optimization_action — the projected-vs-realized reconciliation loop
-- (opt spec §11, M-opt-6). Same "reconcile against reality" ethos as bill
-- reconciliation: a projected saving becomes a REALIZED one only after the user
-- applies the fix and we measure the actual month-over-month drop in that lever's
-- avoidable spend.
--
-- When a user marks a measured opportunity "applied", we freeze the projection
-- (projected_monthly) with the period it was applied in (applied_on). In a later
-- period we recompute that lever's current avoidable spend; realized = projected −
-- current. This proves ROI to the CFO and tunes future projections.

CREATE TABLE optimization_action (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    feature_id        uuid NOT NULL REFERENCES feature(id) ON DELETE CASCADE,
    lever             text NOT NULL CHECK (lever IN ('duplicate_calls', 'prompt_caching')),
    applied_on        date NOT NULL,                    -- the period it was marked applied
    projected_monthly numeric(14, 4) NOT NULL,          -- frozen projection at apply time
    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, feature_id, lever)               -- one live action per lever per feature
);

CREATE INDEX optimization_action_feature_idx ON optimization_action (tenant_id, feature_id);

GRANT SELECT, INSERT, UPDATE, DELETE ON optimization_action TO meter_app;

ALTER TABLE optimization_action ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON optimization_action
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);
