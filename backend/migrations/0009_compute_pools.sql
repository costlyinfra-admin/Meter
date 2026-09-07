-- 0009: self-hosted compute pools + per-feature usage (OSS-M2).
--
-- Self-hosted / open-source serving (vLLM, Ollama, TGI on your own GPUs or
-- on-prem) has NO per-token price — the cost is a GPU/infra bill. We model each
-- serving deployment as a `compute_pool` with a monthly infra cost, and capture
-- the per-feature token/request usage that drives allocation in `pool_usage`.
-- The allocation step (compute.py) splits a pool's monthly cost across features
-- by usage share, writing inference_cost rows with source='self_host'. Untagged
-- usage -> Unattributed (never silently dropped). The parts always sum to the
-- pool cost, so it reconciles by construction.

CREATE TABLE compute_pool (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    name           text NOT NULL,
    provider_label text NOT NULL,            -- the `provider` string the SDK sends for this pool
    monthly_cost   numeric(12, 2) NOT NULL,  -- infra $ for the pool (manual; cloud connector later)
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX compute_pool_label_key ON compute_pool (tenant_id, provider_label);
CREATE INDEX compute_pool_tenant_idx ON compute_pool (tenant_id);

CREATE TABLE pool_usage (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    pool_id       uuid NOT NULL REFERENCES compute_pool(id) ON DELETE CASCADE,
    feature_id    uuid REFERENCES feature(id) ON DELETE SET NULL,  -- NULL -> Unattributed
    model         text,
    period        date NOT NULL,
    tokens_in     bigint NOT NULL DEFAULT 0,
    tokens_out    bigint NOT NULL DEFAULT 0,
    request_count integer NOT NULL DEFAULT 0
);

CREATE INDEX pool_usage_tenant_idx ON pool_usage (tenant_id);
CREATE INDEX pool_usage_pool_period_idx ON pool_usage (tenant_id, pool_id, period);

GRANT SELECT, INSERT, UPDATE, DELETE ON compute_pool, pool_usage TO meter_app;

-- RLS: ENABLE (not FORCE) — owner-exemption model for managed Postgres, same as
-- every other tenant table after migration 0006. The app role is a non-owner and
-- is fully governed by the policy.
ALTER TABLE compute_pool ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON compute_pool
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);

ALTER TABLE pool_usage ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON pool_usage
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);
