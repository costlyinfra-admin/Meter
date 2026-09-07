-- 0019: usage_signal — the measured-optimization signal store (opt spec §5, M-opt-1).
--
-- The metering SDK's optional optimize mode emits privacy-safe *signals* about the
-- SHAPE of traffic — never prompt or response text:
--   * 'duplicate' — the same request fingerprint was seen again (an avoidable repeat)
--   * 'prefix'    — a large static prompt prefix repeated across many calls, uncached
-- Fingerprints are salted per-tenant hashes. This table holds signals only; dollars
-- are always derived at read time from pricing.py, so a price change reprices old
-- opportunities automatically (invariant 3: no black-box numbers).
--
-- Cost accounting is untouched: a 'duplicate' event is also a normal metered call
-- and still lands in inference_cost; a 'prefix' event is a flushed summary whose
-- calls were already metered individually, so it never contributes cost here.

CREATE TABLE usage_signal (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    feature_id    uuid REFERENCES feature(id) ON DELETE SET NULL,   -- NULL => Unattributed
    provider      text NOT NULL,
    model         text,
    period        date NOT NULL,                                    -- monthly bucket, like the rest
    signal_kind   text NOT NULL CHECK (signal_kind IN ('duplicate', 'prefix')),
    fingerprint   text NOT NULL,                                    -- request_fp or prefix_fp (salted hash)
    call_count    bigint NOT NULL DEFAULT 0,     -- duplicates: repeats; prefix: total calls sharing it
    prefix_tokens bigint,                        -- prefix kind only: representative prefix size
    tokens_in     bigint NOT NULL DEFAULT 0,     -- sums, to price a representative call
    tokens_out    bigint NOT NULL DEFAULT 0,
    cached_count  bigint NOT NULL DEFAULT 0,     -- calls already served from cache
    updated_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, feature_id, provider, model, period, signal_kind, fingerprint)
);

CREATE INDEX usage_signal_lookup_idx
    ON usage_signal (tenant_id, feature_id, period, signal_kind);

GRANT SELECT, INSERT, UPDATE, DELETE ON usage_signal TO meter_app;

ALTER TABLE usage_signal ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON usage_signal
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);
