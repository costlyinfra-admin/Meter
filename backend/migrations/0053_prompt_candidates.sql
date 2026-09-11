-- 0053: proposed prompts, for Prompt Optimization (PO-3).
--
-- A candidate is a rewrite of one captured template, with a reason for every
-- change. It is content — it quotes the customer's prompt — so it lives under
-- the same rules as the samples it came from: encrypted with the tenant's data
-- key, readable only through an audited request, and gone when consent is.
--
-- CASCADING IS THE RETENTION STORY. A candidate hangs off its template, so it
-- disappears with it: when a feature is switched off, when consent is withdrawn
-- (which also destroys the key), and when the nightly purge removes a template
-- nothing has used for 30 days. There is no second delete path to forget.
--
-- WHAT A CANDIDATE IS NOT, YET. It carries no savings figure and no
-- recommendation. Both need the replay evaluation (PO-4): until then a
-- candidate's status is exactly 'not_evaluated', and the UI says so.

CREATE TABLE prompt_candidate (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    template_id     uuid NOT NULL REFERENCES prompt_template(id) ON DELETE CASCADE,
    feature_id      uuid NOT NULL REFERENCES feature(id) ON DELETE CASCADE,
    prompt_id       text NOT NULL CHECK (length(prompt_id) BETWEEN 1 AND 200),
    prompt_version  text NOT NULL CHECK (length(prompt_version) BETWEEN 1 AND 60),

    -- The proposed template, and the list of changes with their reasons. Both
    -- quote the prompt, so both are ciphertext under the tenant data key.
    ciphertext      bytea NOT NULL,
    changes_cipher  bytea NOT NULL,
    -- Identities and counts, safe to read without decrypting.
    change_count    integer NOT NULL CHECK (change_count >= 0),
    original_chars  integer NOT NULL CHECK (original_chars >= 0),
    candidate_chars integer NOT NULL CHECK (candidate_chars >= 0),
    -- Which model wrote it: the same disclosure the organization consented to.
    provider        text NOT NULL CHECK (length(provider) BETWEEN 1 AND 80),
    model           text NOT NULL CHECK (length(model) BETWEEN 1 AND 200),

    status          text NOT NULL DEFAULT 'not_evaluated'
                        CHECK (status IN ('not_evaluated', 'discarded')),
    created_by      text NOT NULL CHECK (length(created_by) BETWEEN 1 AND 320),
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX prompt_candidate_template_idx
    ON prompt_candidate (tenant_id, template_id, created_at DESC);

GRANT SELECT, INSERT, UPDATE, DELETE ON prompt_candidate TO meter_app;
ALTER TABLE prompt_candidate ENABLE ROW LEVEL SECURITY;
ALTER TABLE prompt_candidate FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON prompt_candidate
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);

-- The audit log learns three more things worth answering for: who asked a model
-- to rewrite a prompt, who read the result, and who threw it away.
ALTER TABLE prompt_audit DROP CONSTRAINT prompt_audit_event_check;
ALTER TABLE prompt_audit ADD CONSTRAINT prompt_audit_event_check CHECK (event IN (
    'consent_granted', 'consent_withdrawn', 'data_key_destroyed',
    'feature_enabled', 'feature_disabled',
    'sample_content_viewed', 'samples_purged',
    'candidate_generated', 'candidate_viewed', 'candidate_discarded'));
