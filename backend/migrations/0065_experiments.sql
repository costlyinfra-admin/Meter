-- 0065: test a recommendation before changing anything (EX-1, docs/experiments-spec.md).
--
-- Three things, all content-free.
--
-- usage_signal.cache_windows_1h is cache_windows (0062) at the longer cache
-- lifetime a provider sells. A 1-hour write costs more (2x input, against
-- 1.25x for 5 minutes), but sparse traffic rewrites it far less often, so which
-- lifetime saves more depends on the gaps between calls. Only the SDK sees
-- those gaps. NULL means the reporting SDK predates the count -- not zero.
--
-- usage_simulation holds the repeated-request simulation: for each feature,
-- provider, model and scope kind, how many calls there were and how many a
-- response cache would have served at each of four freshness limits. Totals
-- only. Unlike usage_signal it carries no fingerprint at all, because the
-- question it answers -- "how often would a cache this fresh have hit" -- needs
-- none.
--
-- experiment records a test a customer ran on a recommendation: what they
-- chose (typed columns, never free text), what Meter showed them at the time,
-- and what the test found. `result` is written by Meter alone, from the
-- counters above; nothing a customer sends lands in it.

ALTER TABLE usage_signal ADD COLUMN cache_windows_1h bigint;

COMMENT ON COLUMN usage_signal.cache_windows_1h IS
    'prefix kind only: cache_windows counted at a 1-hour cache lifetime. NULL '
    'when the reporting SDK predates the count, which is not the same as zero.';

CREATE TABLE usage_simulation (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    feature_id          uuid REFERENCES feature(id) ON DELETE SET NULL,  -- NULL => Unattributed
    provider            text NOT NULL,
    model               text,
    period              date NOT NULL,
    -- Whether the caller named a customer or cache boundary. A cache nobody
    -- scoped may hand one customer's answer to another, so the two are kept
    -- apart and the scoped figure can be read on its own.
    scope_kind          text NOT NULL CHECK (scope_kind IN ('explicit', 'unscoped')),
    calls               bigint NOT NULL DEFAULT 0 CHECK (calls >= 0),
    tokens_in           bigint NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
    tokens_out          bigint NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
    -- Calls a cache keyed on the request would have served, by freshness limit.
    hits_1m             bigint NOT NULL DEFAULT 0 CHECK (hits_1m >= 0),
    hits_10m            bigint NOT NULL DEFAULT 0 CHECK (hits_10m >= 0),
    hits_1h             bigint NOT NULL DEFAULT 0 CHECK (hits_1h >= 0),
    hits_24h            bigint NOT NULL DEFAULT 0 CHECK (hits_24h >= 0),
    -- The tokens those calls used, so a hit is priced at what it cost.
    hit_tokens_in_1m    bigint NOT NULL DEFAULT 0 CHECK (hit_tokens_in_1m >= 0),
    hit_tokens_in_10m   bigint NOT NULL DEFAULT 0 CHECK (hit_tokens_in_10m >= 0),
    hit_tokens_in_1h    bigint NOT NULL DEFAULT 0 CHECK (hit_tokens_in_1h >= 0),
    hit_tokens_in_24h   bigint NOT NULL DEFAULT 0 CHECK (hit_tokens_in_24h >= 0),
    hit_tokens_out_1m   bigint NOT NULL DEFAULT 0 CHECK (hit_tokens_out_1m >= 0),
    hit_tokens_out_10m  bigint NOT NULL DEFAULT 0 CHECK (hit_tokens_out_10m >= 0),
    hit_tokens_out_1h   bigint NOT NULL DEFAULT 0 CHECK (hit_tokens_out_1h >= 0),
    hit_tokens_out_24h  bigint NOT NULL DEFAULT 0 CHECK (hit_tokens_out_24h >= 0),
    -- Request shapes the SDK forgot while their group was still open at that
    -- limit. Non-zero makes that limit's hits a lower bound.
    evicted_1m          bigint NOT NULL DEFAULT 0 CHECK (evicted_1m >= 0),
    evicted_10m         bigint NOT NULL DEFAULT 0 CHECK (evicted_10m >= 0),
    evicted_1h          bigint NOT NULL DEFAULT 0 CHECK (evicted_1h >= 0),
    evicted_24h         bigint NOT NULL DEFAULT 0 CHECK (evicted_24h >= 0),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    -- NULLS NOT DISTINCT: Unattributed calls (feature_id NULL) for one model
    -- are one row, not one row per delivery.
    CONSTRAINT usage_simulation_key UNIQUE NULLS NOT DISTINCT
        (tenant_id, feature_id, provider, model, period, scope_kind)
);

GRANT SELECT, INSERT, UPDATE, DELETE ON usage_simulation TO meter_app;

ALTER TABLE usage_simulation ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON usage_simulation
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);

CREATE TABLE experiment (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id              uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    feature_id             uuid NOT NULL REFERENCES feature(id) ON DELETE CASCADE,
    -- What can be tested grows milestone by milestone, and so do these lists:
    -- each is exactly what the application accepts today, never wider.
    lever                  text NOT NULL CHECK (lever IN ('duplicate_calls', 'prompt_caching')),
    mode                   text NOT NULL CHECK (mode IN ('simulate')),
    runs_at                text NOT NULL DEFAULT 'customer' CHECK (runs_at IN ('customer')),
    status                 text NOT NULL
                               CHECK (status IN ('waiting_for_data', 'completed', 'cancelled')),
    outcome                text CHECK (outcome IS NULL
                                       OR outcome IN ('passed', 'failed', 'inconclusive')),
    outcome_reason         text NOT NULL DEFAULT '' CHECK (length(outcome_reason) <= 500),

    -- The customer's setting, typed. Repeated requests: how old a cached
    -- answer may be, and whether to count only repeats within a named customer
    -- or cache scope. Prompt caching: which cache lifetime.
    ttl_seconds            integer CHECK (ttl_seconds IS NULL
                                          OR ttl_seconds IN (60, 600, 3600, 86400)),
    scoped_only            boolean,
    cache_ttl              text CHECK (cache_ttl IS NULL OR cache_ttl IN ('5m', '1h')),
    CHECK (
        (lever = 'duplicate_calls' AND ttl_seconds IS NOT NULL AND scoped_only IS NOT NULL
             AND cache_ttl IS NULL)
        OR (lever = 'prompt_caching' AND cache_ttl IS NOT NULL AND ttl_seconds IS NULL
             AND scoped_only IS NULL)
    ),

    -- The month the result was measured on.
    period                 date NOT NULL,
    -- What the recommendation said when the test was started, frozen, so the
    -- result can be read against it later. NULL when the lever had no finding.
    baseline_monthly       numeric(14, 4),
    baseline_savings_type  text,
    baseline_confidence    text,
    -- Numbers computed by Meter from the counters above. Never customer input.
    result                 jsonb,

    created_by             text NOT NULL CHECK (length(created_by) BETWEEN 1 AND 320),
    created_at             timestamptz NOT NULL DEFAULT now(),
    completed_at           timestamptz,
    cancelled_at           timestamptz
);

-- One test waiting for data per recommendation; history is unlimited.
CREATE UNIQUE INDEX experiment_one_waiting
    ON experiment (tenant_id, feature_id, lever) WHERE status = 'waiting_for_data';
CREATE INDEX experiment_feature_idx ON experiment (tenant_id, feature_id, lever, created_at DESC);

GRANT SELECT, INSERT, UPDATE, DELETE ON experiment TO meter_app;

ALTER TABLE experiment ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON experiment
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);

-- Numbers, like usage_signal, which the read-only MCP role already reads.
GRANT SELECT ON usage_simulation, experiment TO meter_read;
