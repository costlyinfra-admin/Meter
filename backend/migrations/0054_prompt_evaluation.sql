-- 0054: proving a rewrite is not worse, for Prompt Optimization (PO-4).
--
-- A candidate is a suggestion until real inputs have been run through both the
-- old prompt and the new one and compared. These tables hold the key that makes
-- that possible, the run itself, and what each replayed case actually produced.
--
-- WHY A SEPARATE KEY. The provider credentials Meter already stores are
-- read-only cost-API keys: they can read a bill and nothing else. Replaying a
-- prompt means making real model calls on the customer's account, which is a
-- different power and a different consent, so it gets its own key, its own
-- table, and its own decision to add.
--
-- WHAT IS CONTENT HERE. The replayed outputs are the model's answers to the
-- customer's real inputs, so they are ciphertext under the same per-tenant data
-- key as everything else in this feature, and they cascade from the candidate —
-- which cascades from its template. Withdrawal, switching a feature off and the
-- 30-day purge all reach them without a second delete path.
--
-- WHAT IS DELIBERATELY PLAINTEXT. Tokens, cost, latency, the verdict and the
-- deterministic checks. They are the evidence for the recommendation, and a
-- reader of the database learns nothing about the prompt from them.

-- ---------------------------------------------------------------------------
-- The key that can replay a prompt. One per provider.
-- ---------------------------------------------------------------------------
CREATE TABLE prompt_eval_key (
    tenant_id   uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    provider    text NOT NULL CHECK (provider IN ('anthropic', 'openai')),
    ciphertext  bytea NOT NULL,
    added_by    text NOT NULL CHECK (length(added_by) BETWEEN 1 AND 320),
    added_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, provider)
);

-- ---------------------------------------------------------------------------
-- One evaluation run.
-- ---------------------------------------------------------------------------
CREATE TABLE prompt_evaluation (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    candidate_id    uuid NOT NULL REFERENCES prompt_candidate(id) ON DELETE CASCADE,
    template_id     uuid NOT NULL REFERENCES prompt_template(id) ON DELETE CASCADE,

    status          text NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running', 'completed', 'failed')),
    -- The answer this whole milestone exists to give. NULL until it is known,
    -- and never 'recommended' unless the decision rule in prompt_eval.py said so.
    decision        text CHECK (decision IS NULL
                                OR decision IN ('recommended', 'not_recommended')),
    decision_reason text NOT NULL DEFAULT '',

    cases_planned   integer NOT NULL CHECK (cases_planned >= 0),
    cases_done      integer NOT NULL DEFAULT 0 CHECK (cases_done >= 0),
    better          integer NOT NULL DEFAULT 0,
    same            integer NOT NULL DEFAULT 0,
    worse           integer NOT NULL DEFAULT 0,
    check_failures  integer NOT NULL DEFAULT 0,

    -- Measured on the replay, priced from the price book. Per call, not totals:
    -- what a customer decides with is "what does one call cost now, and after".
    cost_before     numeric(14, 6) NOT NULL DEFAULT 0 CHECK (cost_before >= 0),
    cost_after      numeric(14, 6) NOT NULL DEFAULT 0 CHECK (cost_after >= 0),
    tokens_in_before  bigint NOT NULL DEFAULT 0,
    tokens_in_after   bigint NOT NULL DEFAULT 0,
    tokens_out_before bigint NOT NULL DEFAULT 0,
    tokens_out_after  bigint NOT NULL DEFAULT 0,
    latency_before_ms bigint NOT NULL DEFAULT 0,
    latency_after_ms  bigint NOT NULL DEFAULT 0,
    -- What the run itself cost the customer, to hold against the monthly cap.
    spend           numeric(14, 6) NOT NULL DEFAULT 0 CHECK (spend >= 0),

    provider        text NOT NULL CHECK (length(provider) BETWEEN 1 AND 60),
    model           text NOT NULL CHECK (length(model) BETWEEN 1 AND 120),
    judge_model     text NOT NULL DEFAULT '',
    error           text NOT NULL DEFAULT '',
    started_by      text NOT NULL CHECK (length(started_by) BETWEEN 1 AND 320),
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz
);

CREATE INDEX prompt_evaluation_candidate_idx
    ON prompt_evaluation (tenant_id, candidate_id, started_at DESC);
CREATE INDEX prompt_evaluation_spend_idx ON prompt_evaluation (tenant_id, started_at);

-- ---------------------------------------------------------------------------
-- One replayed input, both ways.
-- ---------------------------------------------------------------------------
CREATE TABLE prompt_evaluation_case (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    evaluation_id   uuid NOT NULL REFERENCES prompt_evaluation(id) ON DELETE CASCADE,
    sample_id       uuid,

    -- The two answers, encrypted like every other piece of content here.
    before_cipher   bytea NOT NULL,
    after_cipher    bytea NOT NULL,

    tokens_in_before  integer NOT NULL DEFAULT 0,
    tokens_in_after   integer NOT NULL DEFAULT 0,
    tokens_out_before integer NOT NULL DEFAULT 0,
    tokens_out_after  integer NOT NULL DEFAULT 0,
    latency_before_ms integer NOT NULL DEFAULT 0,
    latency_after_ms  integer NOT NULL DEFAULT 0,
    cost_before     numeric(14, 6) NOT NULL DEFAULT 0,
    cost_after      numeric(14, 6) NOT NULL DEFAULT 0,

    verdict         text NOT NULL CHECK (verdict IN ('better', 'same', 'worse', 'unjudged')),
    -- One line from the judge, about the two answers. Not content itself, but it
    -- can quote them, so it is encrypted with them.
    verdict_cipher  bytea NOT NULL,
    -- Deterministic checks: what broke, if anything. Names only, never values.
    failed_checks   jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX prompt_evaluation_case_run_idx ON prompt_evaluation_case (tenant_id, evaluation_id);

-- ---------------------------------------------------------------------------
-- Grants and per-tenant isolation.
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE ON
    prompt_eval_key, prompt_evaluation, prompt_evaluation_case TO meter_app;

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['prompt_eval_key', 'prompt_evaluation', 'prompt_evaluation_case']
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

-- A candidate can now be recommended, or not, by an evaluation.
ALTER TABLE prompt_candidate DROP CONSTRAINT prompt_candidate_status_check;
ALTER TABLE prompt_candidate ADD CONSTRAINT prompt_candidate_status_check CHECK (
    status IN ('not_evaluated', 'evaluating', 'recommended', 'not_recommended', 'discarded'));

-- The audit log learns who spent the customer's tokens, and on what.
ALTER TABLE prompt_audit DROP CONSTRAINT prompt_audit_event_check;
ALTER TABLE prompt_audit ADD CONSTRAINT prompt_audit_event_check CHECK (event IN (
    'consent_granted', 'consent_withdrawn', 'data_key_destroyed',
    'feature_enabled', 'feature_disabled',
    'sample_content_viewed', 'samples_purged',
    'candidate_generated', 'candidate_viewed', 'candidate_discarded',
    'eval_key_added', 'eval_key_removed',
    'evaluation_started', 'evaluation_finished', 'evaluation_viewed'));
