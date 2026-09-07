-- 0039: bring-your-own LLM key for feature discovery.
--
-- Discovery clusters PR metadata with an LLM. Today that is always Meter's
-- own server-side endpoint (METER_DISCOVERY_*). A tenant may prefer to use
-- their own account — for data-handling reasons, for a model they trust, or to
-- keep the spend on their own bill. This table holds that optional override.
--
-- One row per tenant (the primary key), and entirely optional: no row means
-- discovery behaves exactly as it does today. `enabled` lets a tenant switch
-- back to Meter's endpoint without discarding the configuration.
--
-- The key is encrypted with crypto.encrypt before it reaches this table, the
-- same as connector_credential, and is never read back out to any API or UI —
-- only to the outbound request that uses it.

CREATE TABLE discovery_llm (
    tenant_id   uuid PRIMARY KEY REFERENCES tenant(id) ON DELETE CASCADE,
    provider    text NOT NULL,        -- a known OpenAI-compatible host, or 'custom'
    base_url    text NOT NULL,        -- resolved endpoint, e.g. https://api.groq.com/openai/v1
    model       text NOT NULL,
    ciphertext  bytea NOT NULL,       -- the API key, encrypted at rest
    enabled     boolean NOT NULL DEFAULT true,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text                  -- user email that last changed it
);

GRANT SELECT, INSERT, UPDATE, DELETE ON discovery_llm TO meter_app;

ALTER TABLE discovery_llm ENABLE ROW LEVEL SECURITY;
ALTER TABLE discovery_llm FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON discovery_llm
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);
