-- 0014: SSO/SCIM seat sources (Phase 2 of automated build cost).
--
-- An identity provider (Okta first) is the enterprise system of record for which
-- users are assigned which SaaS app. `seat_source` maps an IdP application to one
-- of our priced coding tools+plan, so a sync pulls the roster (who's assigned),
-- prices it from seatpricing, and allocates per developer to features — no CSV.
-- The IdP credential (domain + token) is stored as one encrypted JSON blob in the
-- existing connector_credential.ciphertext, like the AWS/Bedrock connector.

CREATE TABLE seat_source (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    provider    text NOT NULL,            -- identity provider, e.g. 'okta'
    app_id      text NOT NULL,            -- the IdP application id
    app_label   text,                     -- human-friendly app name
    tool        text NOT NULL,            -- maps to seatpricing (copilot/cursor/...)
    plan        text NOT NULL,            -- seat plan (business/enterprise/pro/...)
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX seat_source_app_key ON seat_source (tenant_id, provider, app_id);
CREATE INDEX seat_source_tenant_idx ON seat_source (tenant_id);

GRANT SELECT, INSERT, UPDATE, DELETE ON seat_source TO meter_app;

ALTER TABLE seat_source ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON seat_source
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);

-- Allow the Okta credential type.
ALTER TABLE connector_credential DROP CONSTRAINT connector_credential_connector_type_check;
ALTER TABLE connector_credential ADD CONSTRAINT connector_credential_connector_type_check
    CHECK (connector_type IN ('github', 'anthropic', 'openai', 'google', 'bedrock', 'openrouter',
                              'together', 'fireworks', 'okta', 'cursor', 'copilot', 'codex'));

-- Coding tools are now an open, growing set (Cursor, Tabnine, Cody, Amazon Q,
-- Gemini Code Assist, …), validated in app code (seatpricing / VALID_TOOLS)
-- rather than a fixed DB enum. Drop the brittle tool CHECK, same rationale as the
-- inference provider CHECK in 0008.
ALTER TABLE build_cost DROP CONSTRAINT build_cost_tool_check;
