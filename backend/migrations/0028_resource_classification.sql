-- 0028: user-controlled resource classification, decoupled from monthly cost.
--
-- Classification (production / development / internal / ignore / unclassified) is a
-- MANUAL decision that must persist independently of the monthly cost rows it
-- annotates. This table is the stable config, keyed by
--   (tenant_id, provider, resource_type, resource_id)
-- e.g. (tenant, anthropic, api_key, apikey_123) or (tenant, openai, project, proj_1).
--
-- Ingestion registers discovered resources here (defaulting to 'unclassified' and
-- NEVER overwriting an existing classification), then resolves each cost row's
-- `environment` snapshot from the saved classification. No naming convention ever
-- sets a classification — the user does.

CREATE TABLE resource_classification (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    provider           text NOT NULL,
    resource_type      text NOT NULL,   -- 'api_key' | 'workspace' | 'project' | ...
    resource_id        text NOT NULL,   -- provider's external id for the resource
    resource_name      text,            -- human name (display only; never drives class)
    parent_resource_id text,            -- e.g. api_key's workspace_id (hierarchy)
    classification     text NOT NULL DEFAULT 'unclassified'
        CHECK (classification IN
               ('production', 'development', 'internal', 'ignore', 'unclassified')),
    first_seen         timestamptz NOT NULL DEFAULT now(),
    last_seen          timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    updated_by         text,            -- user email when a person set it (else NULL)
    UNIQUE (tenant_id, provider, resource_type, resource_id)
);

GRANT SELECT, INSERT, UPDATE, DELETE ON resource_classification TO meter_app;

ALTER TABLE resource_classification ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON resource_classification
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);

-- The resolved-classification snapshot on cost rows gains the new 'ignore' value.
ALTER TABLE inference_cost DROP CONSTRAINT IF EXISTS inference_cost_environment_check;
ALTER TABLE inference_cost ADD CONSTRAINT inference_cost_environment_check
    CHECK (environment IN
           ('production', 'development', 'internal', 'ignore', 'unclassified'));
