-- 0067: live experiments — a share of real traffic on the change (EX-3).
--
-- EX-2 tested a cheaper model on the customer's own cases. This tests it on
-- their traffic: the customer's own feature-flag tool sends a share of
-- requests to the cheaper model, the SDK tags each call with the test and the
-- group it was in, and Meter compares the two groups on what it already
-- measures for every call — cost (priced by Meter), errors and latency — plus a
-- quality score the customer's own system sends, where there is one. Numbers
-- only, like everything else on this path.
--
-- experiment gains the live mode, a running status, and the live rule's dials:
-- the minimum calls per group and days before any verdict (so a lucky first
-- hour cannot pass a test), the guardrails that stop it early, and how much
-- quality may fall. A customer may loosen any of them (decision 4); the result
-- says so.
--
-- ai_span gains the test and group a call was in. Only a running live test of
-- the same organization can be named; anything else is dropped at ingest.
--
-- experiment_score holds quality scores as daily totals per group — count,
-- sum, sum of squares — enough for a mean and its interval, and nothing that
-- could say which answer a score was about.
--
-- alert_rule gains the `experiment_guardrail` metric: each live test registers
-- one system-managed rule, so a guardrail breach notifies through the same
-- channels as every other alert.

ALTER TABLE experiment DROP CONSTRAINT experiment_mode_check;
ALTER TABLE experiment ADD CONSTRAINT experiment_mode_check
    CHECK (mode IN ('simulate', 'offline', 'live'));
ALTER TABLE experiment DROP CONSTRAINT experiment_status_check;
ALTER TABLE experiment ADD CONSTRAINT experiment_status_check
    CHECK (status IN ('waiting_for_data', 'waiting_for_results', 'running', 'completed',
                      'cancelled'));

-- The share of traffic the customer intends to send to the candidate. Meter
-- does not route traffic; this is what the result is read against.
ALTER TABLE experiment ADD COLUMN traffic_share smallint
    CHECK (traffic_share IS NULL OR traffic_share BETWEEN 1 AND 50);
-- No verdict before BOTH: this many calls in each group, and this many days.
ALTER TABLE experiment ADD COLUMN min_calls integer
    CHECK (min_calls IS NULL OR min_calls BETWEEN 100 AND 100000);
ALTER TABLE experiment ADD COLUMN min_days smallint
    CHECK (min_days IS NULL OR min_days BETWEEN 1 AND 30);
-- Guardrails. The candidate's error rate may exceed the control's by at most
-- this many percentage points (0.01 = one point); its slowest-5% latency by at
-- most this share (0.25 = a quarter slower).
ALTER TABLE experiment ADD COLUMN max_error_increase numeric(5, 4)
    CHECK (max_error_increase IS NULL OR max_error_increase BETWEEN 0 AND 0.2);
ALTER TABLE experiment ADD COLUMN max_latency_increase numeric(5, 3)
    CHECK (max_latency_increase IS NULL OR max_latency_increase BETWEEN 0 AND 2);
-- How far the candidate's average quality score may fall below the control's,
-- as a share of the control's (scores come on any scale).
ALTER TABLE experiment ADD COLUMN quality_margin numeric(4, 3)
    CHECK (quality_margin IS NULL OR quality_margin BETWEEN 0 AND 0.5);
-- The guardrail rule this test registered (alerts), and when it was last checked.
ALTER TABLE experiment ADD COLUMN guardrail_alert_id uuid
    REFERENCES alert_rule(id) ON DELETE SET NULL;
ALTER TABLE experiment ADD COLUMN last_evaluated_at timestamptz;

ALTER TABLE experiment DROP CONSTRAINT experiment_setting_check;
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
    OR (lever = 'model_rightsizing' AND mode = 'live' AND provider IS NOT NULL
         AND control_model IS NOT NULL AND candidate_model IS NOT NULL
         AND control_model <> candidate_model
         AND traffic_share IS NOT NULL AND min_calls IS NOT NULL AND min_days IS NOT NULL
         AND max_error_increase IS NOT NULL AND max_latency_increase IS NOT NULL
         AND quality_margin IS NOT NULL
         AND ttl_seconds IS NULL AND scoped_only IS NULL AND cache_ttl IS NULL)
);

-- One test in progress per recommendation, whatever kind.
DROP INDEX experiment_one_waiting;
CREATE UNIQUE INDEX experiment_one_waiting
    ON experiment (tenant_id, feature_id, lever)
    WHERE status IN ('waiting_for_data', 'waiting_for_results', 'running');

ALTER TABLE ai_span ADD COLUMN experiment_id uuid REFERENCES experiment(id) ON DELETE SET NULL;
ALTER TABLE ai_span ADD COLUMN experiment_group text
    CHECK (experiment_group IS NULL OR experiment_group IN ('control', 'candidate'));
ALTER TABLE ai_span ADD CONSTRAINT ai_span_experiment_tag_check
    CHECK ((experiment_id IS NULL) = (experiment_group IS NULL));
CREATE INDEX ai_span_experiment_idx
    ON ai_span (tenant_id, experiment_id, experiment_group) WHERE experiment_id IS NOT NULL;

CREATE TABLE experiment_score (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    experiment_id     uuid NOT NULL REFERENCES experiment(id) ON DELETE CASCADE,
    experiment_group  text NOT NULL CHECK (experiment_group IN ('control', 'candidate')),
    day               date NOT NULL,
    n                 bigint NOT NULL DEFAULT 0 CHECK (n >= 0),
    total             double precision NOT NULL DEFAULT 0,
    total_squares     double precision NOT NULL DEFAULT 0,
    updated_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (experiment_id, experiment_group, day)
);

GRANT SELECT, INSERT, UPDATE, DELETE ON experiment_score TO meter_app;

ALTER TABLE experiment_score ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON experiment_score
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);

GRANT SELECT ON experiment_score TO meter_read;

ALTER TABLE alert_rule DROP CONSTRAINT alert_rule_metric_check;
ALTER TABLE alert_rule ADD CONSTRAINT alert_rule_metric_check
    CHECK (metric IN ('inference_cost', 'build_cost', 'combined_cost',
                      'cost_per_user', 'token_usage', 'unattributed_cost',
                      'stale_agents', 'agent_runtime', 'agent_steps',
                      'cost_per_run', 'retry_loop', 'failed_run_cost',
                      'cache_hit_rate', 'experiment_guardrail'));
