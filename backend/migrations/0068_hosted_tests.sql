-- 0068: Meter-hosted offline tests, and runs that survive a restart (EX-4).
--
-- Two things, because the second needs the first.
--
-- 1. eval_job: a run Meter makes model calls for, written down before the first
--    call. It names the captured samples the run will replay, so a run that is
--    interrupted — a deploy, a crash, a free-tier service going to sleep — is
--    picked up where it stopped rather than lost: each finished case is stored
--    as it completes, and a resumed run skips the ones already stored. A run is
--    held by a lease that each finished case extends; a lease that runs out
--    means whoever held it is gone, and the next reader of the run (or the
--    scheduled job) takes it over. Both prompt-rewrite evaluations (PO-4) and
--    hosted model tests use it.
--
--    `estimate` is what the run was expected to cost when it was allowed to
--    start. While it runs, that is what it counts against the monthly cap, so
--    two runs started together cannot both slip under it.
--
-- 2. Hosted model tests. For a feature whose prompts the customer has agreed
--    Meter may capture, Meter can run the right-sizing test itself: replay the
--    captured calls to the current model on it and on the cheaper one, with the
--    customer's evaluation key, check and judge the answers, and decide by the
--    same rule as a test run on the customer's side. Results are the same
--    numbers-only experiment_case rows; the answers themselves are not kept.
--    experiment.runs_at gains 'meter' and results_source gains 'meter'.

ALTER TABLE experiment DROP CONSTRAINT experiment_runs_at_check;
ALTER TABLE experiment ADD CONSTRAINT experiment_runs_at_check
    CHECK (runs_at IN ('customer', 'meter'));
-- Only an offline model test can run inside Meter, and only it says so.
ALTER TABLE experiment ADD CONSTRAINT experiment_hosted_check
    CHECK (runs_at = 'customer' OR (mode = 'offline' AND lever = 'model_rightsizing'));

ALTER TABLE experiment DROP CONSTRAINT experiment_results_source_check;
ALTER TABLE experiment ADD CONSTRAINT experiment_results_source_check
    CHECK (results_source IS NULL OR results_source IN ('runner', 'promptfoo', 'inspect', 'meter'));

CREATE TABLE eval_job (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    kind           text NOT NULL CHECK (kind IN ('prompt_evaluation', 'hosted_test')),
    -- Exactly one subject, matching the kind. Deleting the subject (consent
    -- withdrawn, a template purged) deletes the job with it.
    evaluation_id  uuid UNIQUE REFERENCES prompt_evaluation(id) ON DELETE CASCADE,
    experiment_id  uuid UNIQUE REFERENCES experiment(id) ON DELETE CASCADE,
    -- The captured samples this run will replay, in order. Ids, not content.
    sample_ids     uuid[] NOT NULL CHECK (cardinality(sample_ids) BETWEEN 1 AND 500),
    estimate       numeric(14, 6) NOT NULL CHECK (estimate >= 0),
    status         text NOT NULL DEFAULT 'queued'
                       CHECK (status IN ('queued', 'running', 'done', 'failed')),
    -- How many times whoever held the run vanished without finishing it. A
    -- run that keeps dying is stopped rather than retried for ever.
    interruptions  integer NOT NULL DEFAULT 0 CHECK (interruptions >= 0),
    -- Who holds the run, and until when. Each finished case renews the lease;
    -- only the holder of `lease_token` may renew it or finish the run.
    lease_token    uuid,
    leased_until   timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz,
    CHECK (
        (kind = 'prompt_evaluation' AND evaluation_id IS NOT NULL AND experiment_id IS NULL)
        OR (kind = 'hosted_test' AND experiment_id IS NOT NULL AND evaluation_id IS NULL)
    )
);

CREATE INDEX eval_job_open_idx ON eval_job (status, leased_until)
    WHERE status IN ('queued', 'running');
CREATE INDEX eval_job_month_idx ON eval_job (tenant_id, created_at);

GRANT SELECT, INSERT, UPDATE, DELETE ON eval_job TO meter_app;

ALTER TABLE eval_job ENABLE ROW LEVEL SECURITY;
ALTER TABLE eval_job FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON eval_job
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);

-- A resumed run needs to know which samples it already replayed, and must not
-- store one twice. (A hosted test's case is keyed by its sample id, hashed, and
-- experiment_case already holds each case once.)
CREATE UNIQUE INDEX prompt_evaluation_case_sample_key
    ON prompt_evaluation_case (evaluation_id, sample_id);

-- The audit log learns who started a hosted test, and that it finished.
ALTER TABLE prompt_audit DROP CONSTRAINT prompt_audit_event_check;
ALTER TABLE prompt_audit ADD CONSTRAINT prompt_audit_event_check CHECK (event IN (
    'consent_granted', 'consent_withdrawn', 'data_key_destroyed',
    'feature_enabled', 'feature_disabled',
    'sample_content_viewed', 'samples_purged',
    'candidate_generated', 'candidate_viewed', 'candidate_discarded',
    'eval_key_added', 'eval_key_removed',
    'evaluation_started', 'evaluation_finished', 'evaluation_viewed',
    'hosted_test_started', 'hosted_test_finished'));
