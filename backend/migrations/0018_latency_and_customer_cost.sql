-- 0018: surface two signals the metering SDK (v0.2) now sends — call latency and
-- per-customer attribution (from the optional `metadata.customer_id`).
--
-- Latency lives on the existing hook inference rows (avg = latency_ms_sum /
-- request_count). Customer cost is a parallel monthly rollup keyed by customer,
-- populated from hook events only (metered calls) — it's a "who consumed the
-- spend" view alongside the "what/where" of feature/provider cost, never blended
-- into the authoritative bill.

-- Latency: nullable, only hook rows populate it.
ALTER TABLE inference_cost ADD COLUMN latency_ms_sum bigint;

-- Per-customer metered spend (from SDK metadata.customer_id).
CREATE TABLE customer_cost (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    customer_id   text NOT NULL,          -- the tenant's own customer identifier
    period        date NOT NULL,          -- monthly bucket, like the rest
    amount        numeric(14, 4) NOT NULL DEFAULT 0,
    request_count bigint NOT NULL DEFAULT 0,
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX customer_cost_key ON customer_cost (tenant_id, customer_id, period);
CREATE INDEX customer_cost_period_idx ON customer_cost (tenant_id, period);

GRANT SELECT, INSERT, UPDATE, DELETE ON customer_cost TO meter_app;

ALTER TABLE customer_cost ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON customer_cost
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);
