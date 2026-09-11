-- 0052: consented prompt capture, for Prompt Optimization (PO-1).
--
-- Until this migration Meter held no prompt text anywhere, and the trace path
-- still does not: ai_trace and ai_span have no content column, the SDK has no
-- field for it, and ingest refuses payloads that carry it. None of that changes.
-- These tables are a separate store that only fills when a customer has agreed,
-- in writing, feature by feature. See docs/prompt-optimization-spec.md.
--
-- THE DOUBLE KEY. Content is stored only while an organization consent row
-- exists AND the feature has been switched on. Withdrawing either deletes what
-- that consent covered.
--
-- ENVELOPE ENCRYPTION. Every piece of content here is Fernet ciphertext under a
-- per-tenant data key, and that key is itself stored only encrypted by the app
-- key. Withdrawing consent deletes the data key, which leaves any ciphertext that
-- survives elsewhere (a backup, a replica) unreadable. Template digests are keyed
-- HMACs under the same data key, so they cannot be matched against guessed text.
--
-- WHAT IS DELIBERATELY PLAINTEXT. Identities and numbers only: which feature,
-- which prompt and version, provider, model, token counts, latency, sizes and
-- times. Enough to list and count without decrypting; nothing a reader could use
-- to reconstruct a prompt.
--
-- RETENTION. Samples are deleted 30 days after they arrive, and templates 30 days
-- after they were last seen, by the daily job (prompt_capture.purge_all_tenants).
--
-- AUDIT. prompt_audit is append-only by grant: the app role may insert and read
-- it, never update or delete it, so an audit row cannot be quietly rewritten. It
-- outlives withdrawal on purpose, and never holds content.

-- ---------------------------------------------------------------------------
-- Organization consent. One row while consent stands; deleted on withdrawal
-- (the history lives in prompt_audit).
-- ---------------------------------------------------------------------------
CREATE TABLE prompt_capture_consent (
    tenant_id          uuid PRIMARY KEY REFERENCES tenant(id) ON DELETE CASCADE,
    -- The consent text the person agreed to. A newer text means capture pauses
    -- until someone agrees to it.
    consent_version    text NOT NULL CHECK (length(consent_version) BETWEEN 1 AND 40),
    granted_by         text NOT NULL CHECK (length(granted_by) BETWEEN 1 AND 320),
    granted_at         timestamptz NOT NULL DEFAULT now(),
    -- Who was named as seeing prompts. If the organization later points its
    -- model elsewhere, capture pauses until the new disclosure is agreed to.
    disclosed_source   text NOT NULL CHECK (disclosed_source IN ('byok', 'meter')),
    disclosed_provider text NOT NULL CHECK (length(disclosed_provider) BETWEEN 1 AND 80),
    disclosed_model    text NOT NULL CHECK (length(disclosed_model) BETWEEN 1 AND 200)
);

-- ---------------------------------------------------------------------------
-- Feature consent. A feature with no row captures nothing.
-- ---------------------------------------------------------------------------
CREATE TABLE prompt_capture_feature (
    tenant_id   uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    feature_id  uuid NOT NULL REFERENCES feature(id) ON DELETE CASCADE,
    enabled_by  text NOT NULL CHECK (length(enabled_by) BETWEEN 1 AND 320),
    enabled_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, feature_id)
);

-- ---------------------------------------------------------------------------
-- The tenant's data key, wrapped by the app key. Deleting this row is the
-- crypto-shred.
-- ---------------------------------------------------------------------------
CREATE TABLE prompt_data_key (
    tenant_id   uuid PRIMARY KEY REFERENCES tenant(id) ON DELETE CASCADE,
    wrapped_key bytea NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Prompt templates: the instruction text, stored once per distinct text within
-- a prompt version.
-- ---------------------------------------------------------------------------
CREATE TABLE prompt_template (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    feature_id      uuid NOT NULL REFERENCES feature(id) ON DELETE CASCADE,
    prompt_id       text NOT NULL CHECK (length(prompt_id) BETWEEN 1 AND 200),
    prompt_version  text NOT NULL CHECK (length(prompt_version) BETWEEN 1 AND 60),
    template_digest text NOT NULL CHECK (length(template_digest) = 64),
    ciphertext      bytea NOT NULL,
    size_bytes      integer NOT NULL CHECK (size_bytes >= 0),
    first_seen_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, feature_id, prompt_id, prompt_version, template_digest)
);

CREATE INDEX prompt_template_seen_idx ON prompt_template (tenant_id, last_seen_at);

-- ---------------------------------------------------------------------------
-- Samples: one real call's variable input and output, for testing a candidate.
-- ---------------------------------------------------------------------------
CREATE TABLE prompt_sample (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    feature_id      uuid NOT NULL REFERENCES feature(id) ON DELETE CASCADE,
    template_id     uuid NOT NULL REFERENCES prompt_template(id) ON DELETE CASCADE,
    prompt_id       text NOT NULL CHECK (length(prompt_id) BETWEEN 1 AND 200),
    prompt_version  text NOT NULL CHECK (length(prompt_version) BETWEEN 1 AND 60),
    provider        text NOT NULL CHECK (length(provider) BETWEEN 1 AND 60),
    model           text NOT NULL CHECK (length(model) BETWEEN 1 AND 120),
    -- {input, output, parameters}, encrypted under the tenant data key.
    ciphertext      bytea NOT NULL,
    size_bytes      integer NOT NULL CHECK (size_bytes >= 0),
    tokens_in       integer CHECK (tokens_in IS NULL OR tokens_in >= 0),
    tokens_out      integer CHECK (tokens_out IS NULL OR tokens_out >= 0),
    latency_ms      integer CHECK (latency_ms IS NULL OR latency_ms >= 0),
    captured_at     timestamptz NOT NULL,
    received_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX prompt_sample_version_idx
    ON prompt_sample (tenant_id, feature_id, prompt_id, prompt_version, received_at DESC);
CREATE INDEX prompt_sample_received_idx ON prompt_sample (tenant_id, received_at);

-- ---------------------------------------------------------------------------
-- Who did what. Append-only. Never content.
-- ---------------------------------------------------------------------------
CREATE TABLE prompt_audit (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    event      text NOT NULL CHECK (event IN (
                   'consent_granted', 'consent_withdrawn', 'data_key_destroyed',
                   'feature_enabled', 'feature_disabled',
                   'sample_content_viewed', 'samples_purged')),
    actor      text,
    -- Not a foreign key: the record of what happened to a feature outlives it.
    feature_id uuid,
    detail     jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX prompt_audit_tenant_idx ON prompt_audit (tenant_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- Grants and per-tenant isolation.
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE ON
    prompt_capture_consent, prompt_capture_feature, prompt_template, prompt_sample
    TO meter_app;
GRANT SELECT, INSERT, DELETE ON prompt_data_key TO meter_app;
-- Append-only: no UPDATE, no DELETE.
GRANT SELECT, INSERT ON prompt_audit TO meter_app;

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['prompt_capture_consent', 'prompt_capture_feature',
                             'prompt_data_key', 'prompt_template', 'prompt_sample',
                             'prompt_audit']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I '
            'USING (tenant_id = NULLIF(current_setting(''app.current_tenant'', true), '''')::uuid) '
            'WITH CHECK (tenant_id = NULLIF(current_setting(''app.current_tenant'', true), '''')::uuid)',
            t);
    END LOOP;
END $$;
