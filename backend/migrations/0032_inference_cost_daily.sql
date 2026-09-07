-- 0032: daily-granularity inference cost (additive).
--
-- Provider cost/usage APIs return DAILY buckets, but inference_cost stores only a
-- monthly rollup (period pinned to the 1st). This table keeps the same connector
-- spend at DAY resolution so we can show daily trends and compute an exact
-- month-to-date. It is written in the SAME ingest transaction as inference_cost;
-- summing a month of these rows equals the monthly inference_cost row (a rollup
-- test enforces it). inference_cost stays the reconciled monthly authority — this
-- is an additive detail table, not a replacement.
--
-- Connector rows only for now (source cost_api / cost_api_est). Hook and self-host
-- spend remain monthly in inference_cost.
CREATE TABLE inference_cost_daily (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    feature_id     uuid REFERENCES feature(id) ON DELETE SET NULL,
    provider       text NOT NULL,
    model          text,
    api_key_ref    text,
    amount         numeric(14, 4) NOT NULL,
    currency       text NOT NULL DEFAULT 'USD',
    day            date NOT NULL,              -- the actual usage/billing day
    tokens_in      bigint,
    tokens_out     bigint,
    request_count  bigint,
    cached_tokens_in bigint,
    workspace_id   text,
    workspace_name text,
    api_key_id     text,
    api_key_name   text,
    environment    text
        CHECK (environment IS NULL OR environment IN
               ('production', 'development', 'internal', 'ignore', 'unclassified')),
    source         text NOT NULL
        CHECK (source IN ('cost_api', 'cost_api_est', 'hook', 'self_host')),
    confidence     text NOT NULL CHECK (confidence IN ('high', 'med', 'low')),
    created_at     timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE, DELETE ON inference_cost_daily TO meter_app;

ALTER TABLE inference_cost_daily ENABLE ROW LEVEL SECURITY;
ALTER TABLE inference_cost_daily FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON inference_cost_daily
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);

-- Range scans by day (trends, MTD) and the idempotent per-(provider, month) delete.
CREATE INDEX inference_cost_daily_day_idx      ON inference_cost_daily (tenant_id, day);
CREATE INDEX inference_cost_daily_feature_idx  ON inference_cost_daily (tenant_id, feature_id, day);
CREATE INDEX inference_cost_daily_provider_idx ON inference_cost_daily (tenant_id, provider, day);
