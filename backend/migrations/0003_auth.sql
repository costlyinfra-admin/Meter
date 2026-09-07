-- Meter M2 — auth identities and encrypted connector credentials.
--
--   * `app_user` — a login identity belonging to a tenant. (Named app_user, not
--     "user", to avoid quoting the SQL reserved word everywhere.)
--   * `connector_credential` — a tenant's connector secret, stored ENCRYPTED
--     (ciphertext bytea). Plaintext credentials never touch the database.
--
-- Both are tenant-scoped and isolated by RLS, exactly like the M1 tables.
--
-- AUTH vs RLS: authentication must resolve an email -> user BEFORE any tenant
-- context exists, so signup/login run on the bootstrap/admin connection (which
-- bypasses RLS). Email is globally unique, enforced by a constraint that holds
-- regardless of RLS visibility. Once authenticated, tenant-scoped reads/writes
-- (including connector_credential) go through the app role with RLS in force.

-- ---------------------------------------------------------------------------
-- app_user
-- ---------------------------------------------------------------------------
CREATE TABLE app_user (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    email          text NOT NULL,
    password_hash  text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now()
);

-- Global, case-insensitive email uniqueness (emails are stored lower-cased).
CREATE UNIQUE INDEX app_user_email_key ON app_user (email);
CREATE INDEX app_user_tenant_idx ON app_user (tenant_id);

-- ---------------------------------------------------------------------------
-- connector_credential — encrypted at rest (Fernet ciphertext in `ciphertext`).
-- ---------------------------------------------------------------------------
CREATE TABLE connector_credential (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    connector_type  text NOT NULL
                        CHECK (connector_type IN ('github', 'anthropic', 'openai',
                                                  'cursor', 'copilot', 'codex')),
    label           text,
    ciphertext      bytea NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX connector_credential_tenant_idx ON connector_credential (tenant_id);

-- ---------------------------------------------------------------------------
-- Grants + RLS (same pattern as 0002).
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE ON app_user, connector_credential TO meter_app;

ALTER TABLE app_user ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_user FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app_user
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);

ALTER TABLE connector_credential ENABLE ROW LEVEL SECURITY;
ALTER TABLE connector_credential FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON connector_credential
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);
