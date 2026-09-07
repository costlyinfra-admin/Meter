-- Meter M7 — per-tenant ingest token for the metering hook.
--
-- The metering SDK runs in the customer's production app (server-to-server), so
-- it can't use a browser session cookie. Instead each tenant has an ingest token;
-- only its SHA-256 hash is stored. The hook-ingest endpoint resolves the token to
-- a tenant (admin connection, like login), then writes events under that tenant.

CREATE TABLE hook_token (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    token_hash  text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX hook_token_hash_key ON hook_token (token_hash);
CREATE INDEX hook_token_tenant_idx ON hook_token (tenant_id);

GRANT SELECT, INSERT, UPDATE, DELETE ON hook_token TO meter_app;

ALTER TABLE hook_token ENABLE ROW LEVEL SECURITY;
ALTER TABLE hook_token FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON hook_token
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);
