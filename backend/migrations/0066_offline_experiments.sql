-- 0066: offline tests, run by the customer, results back as numbers (EX-2).
--
-- EX-1 tested by simulation from counters the SDK already sends. A cheaper
-- model cannot be tested that way: whether it holds quality is a question about
-- answers, and Meter does not hold answers. So the test runs where the answers
-- are — on the customer's machine, on their test cases, with their keys and
-- their choice of judge — and only numbers come back: tokens, latency, whether
-- a call failed or broke a check, and which answer the judge preferred.
--
-- experiment gains the offline mode, model right-sizing, and the customer's
-- setting for it: which model is being replaced by which, and the decision
-- rule's two dials. A customer may loosen the rule (decision 4, 2026-10-01);
-- the rule actually applied is stored with the result, and a loosened one is
-- shown as such wherever the result appears.
--
-- experiment_token is the one credential an offline run holds: it can fetch
-- that experiment's instructions and post its results, once, and nothing else.
-- Stored as a hash, like the ingest token.
--
-- experiment_case is one compared test case: numbers and a verdict. The case is
-- identified by a hash the runner salts per run, so not even the customer's own
-- case ids leave their machine.

ALTER TABLE experiment DROP CONSTRAINT experiment_lever_check;
ALTER TABLE experiment ADD CONSTRAINT experiment_lever_check
    CHECK (lever IN ('duplicate_calls', 'prompt_caching', 'model_rightsizing'));
ALTER TABLE experiment DROP CONSTRAINT experiment_mode_check;
ALTER TABLE experiment ADD CONSTRAINT experiment_mode_check
    CHECK (mode IN ('simulate', 'offline'));
ALTER TABLE experiment DROP CONSTRAINT experiment_status_check;
ALTER TABLE experiment ADD CONSTRAINT experiment_status_check
    CHECK (status IN ('waiting_for_data', 'waiting_for_results', 'completed', 'cancelled'));

-- Which model is being replaced, by which, on which provider.
ALTER TABLE experiment ADD COLUMN provider text
    CHECK (provider IS NULL OR provider IN ('anthropic', 'openai'));
ALTER TABLE experiment ADD COLUMN control_model text
    CHECK (control_model IS NULL OR length(control_model) BETWEEN 1 AND 120);
ALTER TABLE experiment ADD COLUMN candidate_model text
    CHECK (candidate_model IS NULL OR length(candidate_model) BETWEEN 1 AND 120);
-- The decision rule's dials: how many cases may go worse beyond the ones that
-- go better, as a share of all of them, and how many cases are needed at all.
ALTER TABLE experiment ADD COLUMN loss_margin numeric(4, 3)
    CHECK (loss_margin IS NULL OR (loss_margin >= 0 AND loss_margin <= 0.5));
ALTER TABLE experiment ADD COLUMN min_cases integer
    CHECK (min_cases IS NULL OR min_cases BETWEEN 10 AND 500);
-- Where the results came from: Meter's runner, or a tool the customer already
-- uses, imported on their machine.
ALTER TABLE experiment ADD COLUMN results_source text
    CHECK (results_source IS NULL OR results_source IN ('runner', 'promptfoo', 'inspect'));
-- The judge the customer chose, as they named it. A model name, not prose.
ALTER TABLE experiment ADD COLUMN judge_model text
    CHECK (judge_model IS NULL OR judge_model ~ '^[A-Za-z0-9._:/@-]{1,120}$');
-- What running the test cost, priced by Meter from the tokens it reported.
-- Already part of the provider's bill; recorded so it can be shown as testing.
ALTER TABLE experiment ADD COLUMN test_cost numeric(14, 6)
    CHECK (test_cost IS NULL OR test_cost >= 0);

ALTER TABLE experiment DROP CONSTRAINT experiment_check;
ALTER TABLE experiment ADD CONSTRAINT experiment_setting_check CHECK (
    (lever = 'duplicate_calls' AND mode = 'simulate' AND ttl_seconds IS NOT NULL
         AND scoped_only IS NOT NULL AND cache_ttl IS NULL AND control_model IS NULL)
    OR (lever = 'prompt_caching' AND mode = 'simulate' AND cache_ttl IS NOT NULL
         AND ttl_seconds IS NULL AND scoped_only IS NULL AND control_model IS NULL)
    OR (lever = 'model_rightsizing' AND mode = 'offline' AND provider IS NOT NULL
         AND control_model IS NOT NULL AND candidate_model IS NOT NULL
         AND control_model <> candidate_model
         AND loss_margin IS NOT NULL AND min_cases IS NOT NULL
         AND ttl_seconds IS NULL AND scoped_only IS NULL AND cache_ttl IS NULL)
);

-- One test waiting per recommendation, whichever kind of waiting.
DROP INDEX experiment_one_waiting;
CREATE UNIQUE INDEX experiment_one_waiting
    ON experiment (tenant_id, feature_id, lever)
    WHERE status IN ('waiting_for_data', 'waiting_for_results');

CREATE TABLE experiment_token (
    experiment_id  uuid PRIMARY KEY REFERENCES experiment(id) ON DELETE CASCADE,
    tenant_id      uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    token_hash     text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    expires_at     timestamptz NOT NULL,
    -- Set when results arrive. A used token answers nothing more.
    used_at        timestamptz
);

CREATE UNIQUE INDEX experiment_token_hash_key ON experiment_token (token_hash);

GRANT SELECT, INSERT, UPDATE, DELETE ON experiment_token TO meter_app;

ALTER TABLE experiment_token ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON experiment_token
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);

CREATE TABLE experiment_case (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    experiment_id       uuid NOT NULL REFERENCES experiment(id) ON DELETE CASCADE,
    case_hash           text NOT NULL CHECK (case_hash ~ '^[0-9a-f]{64}$'),
    control_tokens_in   integer NOT NULL CHECK (control_tokens_in >= 0),
    control_tokens_out  integer NOT NULL CHECK (control_tokens_out >= 0),
    control_latency_ms  integer NOT NULL CHECK (control_latency_ms >= 0),
    control_error       boolean NOT NULL,
    candidate_tokens_in   integer NOT NULL CHECK (candidate_tokens_in >= 0),
    candidate_tokens_out  integer NOT NULL CHECK (candidate_tokens_out >= 0),
    candidate_latency_ms  integer NOT NULL CHECK (candidate_latency_ms >= 0),
    candidate_error       boolean NOT NULL,
    -- Deterministic checks the candidate's answer failed where the control's
    -- passed: empty, not JSON when the control's was, refused, much longer.
    check_failures      integer NOT NULL CHECK (check_failures BETWEEN 0 AND 10),
    verdict             text NOT NULL CHECK (verdict IN ('better', 'same', 'worse', 'unjudged')),
    UNIQUE (experiment_id, case_hash)
);

GRANT SELECT, INSERT, UPDATE, DELETE ON experiment_case TO meter_app;

ALTER TABLE experiment_case ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON experiment_case
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);

-- Numbers; the read-only MCP role reads them like the experiment itself.
GRANT SELECT ON experiment_case TO meter_read;
