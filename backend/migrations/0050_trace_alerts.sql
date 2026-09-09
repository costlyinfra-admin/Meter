-- 0050: alert vocabulary for request-level metrics.
--
-- 0029 wrote the alert vocabulary into three CHECK constraints. That was right
-- -- the database refusing a metric the evaluator cannot compute is the
-- cheapest possible guard against a rule that silently never fires -- but it
-- means adding a metric is a migration, and this is that migration.
--
-- What is new is a whole class of alert. Everything in 0029 answers "how much
-- did we spend this month". These answer "what is happening to our runs right
-- now": a run has gone quiet, a workflow is looping, a release made every run
-- dearer, cache efficiency has collapsed. They read ai_trace/ai_span (0049)
-- rather than the monthly cost tables.
--
-- `application` joins the scope list because a trace belongs to an application
-- and a feature. It deliberately does NOT gain provider or model scope: one run
-- can call several providers and belongs wholly to none of them.
--
-- `falls_below` joins the conditions because cache efficiency is the one metric
-- whose alarm is a fall. Everything else here is a rise.

ALTER TABLE alert_rule DROP CONSTRAINT alert_rule_metric_check;
ALTER TABLE alert_rule ADD CONSTRAINT alert_rule_metric_check
    CHECK (metric IN ('inference_cost', 'build_cost', 'combined_cost',
                      'cost_per_user', 'token_usage', 'unattributed_cost',
                      'stale_agents', 'agent_runtime', 'agent_steps',
                      'cost_per_run', 'retry_loop', 'failed_run_cost',
                      'cache_hit_rate'));

ALTER TABLE alert_rule DROP CONSTRAINT alert_rule_scope_type_check;
ALTER TABLE alert_rule ADD CONSTRAINT alert_rule_scope_type_check
    CHECK (scope_type IN ('organization', 'provider', 'model', 'feature', 'application'));

ALTER TABLE alert_rule DROP CONSTRAINT alert_rule_condition_type_check;
ALTER TABLE alert_rule ADD CONSTRAINT alert_rule_condition_type_check
    CHECK (condition_type IN ('exceeds', 'increase_pct', 'budget_pct', 'falls_below'));
