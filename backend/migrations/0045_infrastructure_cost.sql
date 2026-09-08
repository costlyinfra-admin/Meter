-- 0045: infrastructure cost — the cloud bill, as first-class line items.
--
-- Until now every dollar Meter stored was either inference (inference_cost) or
-- build (build_cost). A company's AI product also runs on ordinary cloud
-- infrastructure — the databases, queues, storage and networking around the
-- model calls — and none of it had anywhere to live.
--
--   infra_cost      — one row per cloud billing line item, at whatever grain the
--                     provider's cost API returned. This is a CANONICAL record:
--                     the raw service name and dimensions are kept verbatim so a
--                     row can be re-classified later without re-fetching the
--                     bill, and each row carries exactly ONE primary category.
--
--   infra_sync_run  — one row per sync, so the connector card can say when it
--                     last ran and why it failed, rather than showing a state
--                     nobody recorded. Customer-facing and RLS-isolated (unlike
--                     admin_sync_log, which belongs to the internal portal).
--
-- Double counting. Amazon Bedrock spend is ALREADY ingested by the existing
-- "Amazon Bedrock (AWS cost)" inference connector, which is and remains its
-- authoritative path. Bedrock line items seen here are still stored — dropping
-- them would make this table stop being a faithful copy of the bill, and Phase 2
-- needs them to reconcile the two paths — but they are written with
-- `counted = false` and `dedupe_owner = 'bedrock'`. Every total taken from this
-- table filters on `counted`, so the same dollar can never arrive twice.

CREATE TABLE infra_cost (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    feature_id    uuid REFERENCES feature(id) ON DELETE SET NULL,

    provider      text NOT NULL,          -- 'aws' today; azure/gcp later
    -- The day (DAILY) or month anchor (MONTHLY) the line item covers, and the
    -- month it rolls up into. `month` is stored rather than derived so the
    -- common "this month's infrastructure" query needs no date_trunc.
    period        date NOT NULL,
    month         date NOT NULL,
    granularity   text NOT NULL CHECK (granularity IN ('DAILY', 'MONTHLY')),
    -- Which Cost Explorer metric these dollars are (UnblendedCost, AmortizedCost…).
    -- Kept per row: a tenant may change the setting, and the old rows must stay
    -- honest about what they measured.
    metric        text NOT NULL,

    -- Raw provider dimensions, preserved exactly as billed. `service` is the
    -- provider's own name for the service ("Amazon Elastic Compute Cloud -
    -- Compute"), never a normalized or guessed one.
    service       text NOT NULL DEFAULT '',
    usage_type    text,
    operation     text,
    account_id    text,
    region        text,
    tag_key       text,
    tag_value     text,
    -- Every group key the cost API returned, keyed by its dimension, so a row
    -- can be reprocessed against rules that did not exist when it was imported.
    dimensions    jsonb NOT NULL DEFAULT '{}'::jsonb,

    amount        numeric(14, 4) NOT NULL,
    currency      text NOT NULL DEFAULT 'USD',

    -- Exactly one primary category per line item. 'unclassified' is a real
    -- state, not a dumping ground: it means the bill gave us no service to
    -- classify by. An item is NEVER dropped for having an unknown service —
    -- unknown services default to 'infrastructure'.
    category      text NOT NULL
                      CHECK (category IN ('inference', 'self_hosted',
                                          'infrastructure', 'build', 'unclassified')),
    -- The rule that decided the category. Every number must be explainable.
    category_rule text NOT NULL,

    -- False when another connector is the authoritative source for these
    -- dollars. Totals filter on this; see the header note on Bedrock.
    counted       boolean NOT NULL DEFAULT true,
    dedupe_owner  text,

    -- How the feature was arrived at. 'direct'  = an activated cost-allocation
    -- tag value matched a configured feature signal; 'allocated' = a configured
    -- service->feature rule matched; 'unattributed' = no reliable mapping, which
    -- is the Unattributed bucket and always feature_id IS NULL.
    allocation_method text NOT NULL DEFAULT 'unattributed'
                      CHECK (allocation_method IN ('direct', 'allocated', 'unattributed')),
    confidence    text NOT NULL CHECK (confidence IN ('high', 'med', 'low')),
    source        text NOT NULL DEFAULT 'cost_api',

    -- The identity of this line item within its (tenant, provider, period).
    -- Re-importing the same day must replace, not duplicate; see the unique
    -- index below. Built from the raw dimensions, so it survives rule changes.
    item_key      text NOT NULL,

    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- Idempotency, enforced by the database rather than by hoping the writer is
-- careful: a repeated sync of the same window upserts onto these rows.
CREATE UNIQUE INDEX infra_cost_item_idx
    ON infra_cost (tenant_id, provider, period, item_key);
CREATE INDEX infra_cost_month_idx ON infra_cost (tenant_id, provider, month);
CREATE INDEX infra_cost_category_idx ON infra_cost (tenant_id, month, category) WHERE counted;

GRANT SELECT, INSERT, UPDATE, DELETE ON infra_cost TO meter_app;
ALTER TABLE infra_cost ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON infra_cost
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);


CREATE TABLE infra_sync_run (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    provider       text NOT NULL,
    -- The billing window the run asked the provider for.
    covered_from   date NOT NULL,
    covered_to     date NOT NULL,
    items          integer NOT NULL DEFAULT 0,
    amount         numeric(14, 4) NOT NULL DEFAULT 0,
    trigger        text NOT NULL DEFAULT 'manual'
                       CHECK (trigger IN ('manual', 'scheduled')),
    status         text NOT NULL CHECK (status IN ('success', 'error')),
    -- Safe message only: never a credential, never a raw provider response body.
    error_message  text,
    started_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz
);

CREATE INDEX infra_sync_run_recent_idx
    ON infra_sync_run (tenant_id, provider, started_at DESC);

GRANT SELECT, INSERT, UPDATE, DELETE ON infra_sync_run TO meter_app;
ALTER TABLE infra_sync_run ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON infra_sync_run
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);

COMMENT ON COLUMN infra_cost.counted IS
    'False when another connector owns these dollars (today: Bedrock, ingested by '
    'the Amazon Bedrock inference connector). Infrastructure totals filter on it.';
COMMENT ON COLUMN infra_cost.service IS
    'The provider''s own service name, verbatim. Never normalized or inferred.';


-- The AWS connector's credential. Same shape and storage as every other
-- connector — one encrypted JSON blob — so it inherits the existing secret
-- handling rather than inventing a second one.
ALTER TABLE connector_credential DROP CONSTRAINT connector_credential_connector_type_check;
ALTER TABLE connector_credential ADD CONSTRAINT connector_credential_connector_type_check
    CHECK (connector_type IN ('github', 'anthropic', 'openai', 'google', 'bedrock', 'openrouter',
                              'together', 'fireworks', 'okta', 'entra', 'cursor', 'copilot',
                              'codex', 'azure', 'litellm', 'vercel', 'modal', 'elevenlabs',
                              'groq', 'mistral', 'xai', 'perplexity', 'cohere', 'replicate',
                              'portkey', 'helicone', 'aws'));
