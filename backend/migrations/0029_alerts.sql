-- 0029: customer cost alerting.
--
-- Five org-scoped tables (RLS-isolated like the rest of the app):
--   alert_rule          — the rule + its evaluation state (status, next_eval_at…)
--   alert_destination   — a rule's notification channels; webhook/Slack secrets
--                         are encrypted at rest (ciphertext only, never returned)
--   alert_incident      — one OPEN incident per rule at a time (dedupes triggers);
--                         resolves when the condition returns to normal
--   alert_event         — the activity feed: triggered / resolved / delivery_error
--                         / test, with a deterministic unique event_key and an
--                         in-app read flag
--   alert_notification  — per-channel delivery attempts (status + safe metadata)

-- ---------------------------------------------------------------------------
CREATE TABLE alert_rule (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    name             text NOT NULL,
    description      text,
    metric           text NOT NULL
        CHECK (metric IN ('inference_cost', 'build_cost', 'combined_cost',
                          'cost_per_user', 'token_usage', 'unattributed_cost')),
    scope_type       text NOT NULL DEFAULT 'organization'
        CHECK (scope_type IN ('organization', 'provider', 'model', 'feature')),
    scope_ref        text,                   -- provider name / model id / feature id
    condition_type   text NOT NULL
        CHECK (condition_type IN ('exceeds', 'increase_pct', 'budget_pct')),
    threshold        numeric(16, 4) NOT NULL CHECK (threshold >= 0),
    -- Monthly budget in the org currency; used only by the 'budget_pct' condition
    -- (threshold is then a percentage of this). Nullable for the other conditions.
    budget_amount    numeric(16, 4) CHECK (budget_amount >= 0),
    "window"         text NOT NULL
        CHECK ("window" IN ('hourly', 'daily', 'weekly', 'monthly')),
    cooldown         text NOT NULL DEFAULT 'day'
        CHECK (cooldown IN ('none', 'hour', 'day', 'week')),
    recovery_notify  boolean NOT NULL DEFAULT true,
    enabled          boolean NOT NULL DEFAULT true,
    -- Evaluation state.
    status           text NOT NULL DEFAULT 'insufficient_data'
        CHECK (status IN ('healthy', 'triggered', 'insufficient_data',
                          'delivery_error', 'disabled')),
    last_observed    numeric(16, 4),
    last_evaluated_at timestamptz,
    last_triggered_at timestamptz,
    last_notified_at  timestamptz,           -- drives cooldown
    next_eval_at      timestamptz NOT NULL DEFAULT now(),
    created_by        text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE alert_destination (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    alert_id    uuid NOT NULL REFERENCES alert_rule(id) ON DELETE CASCADE,
    channel     text NOT NULL CHECK (channel IN ('in_app', 'email', 'slack', 'webhook')),
    target      text,                        -- email address / webhook URL (non-secret)
    secret_ciphertext bytea,                 -- encrypted signing secret / token (nullable)
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE alert_incident (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    alert_id      uuid NOT NULL REFERENCES alert_rule(id) ON DELETE CASCADE,
    status        text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    opened_at     timestamptz NOT NULL DEFAULT now(),
    resolved_at   timestamptz,
    observed_value numeric(16, 4),
    threshold     numeric(16, 4)
);
-- At most ONE open incident per rule — makes "don't recreate while triggered"
-- a database guarantee even under concurrent evaluators.
CREATE UNIQUE INDEX alert_incident_one_open ON alert_incident (alert_id)
    WHERE status = 'open';

CREATE TABLE alert_event (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    alert_id      uuid NOT NULL REFERENCES alert_rule(id) ON DELETE CASCADE,
    incident_id   uuid REFERENCES alert_incident(id) ON DELETE SET NULL,
    event_type    text NOT NULL
        CHECK (event_type IN ('triggered', 'resolved', 'delivery_error', 'test')),
    -- Deterministic per (org, rule, window, transition): a second worker's INSERT
    -- hits this unique constraint instead of creating a duplicate event.
    event_key     text NOT NULL,
    observed_value numeric(16, 4),
    threshold     numeric(16, 4),
    "window"      text,
    window_start  timestamptz,
    message       text,
    read          boolean NOT NULL DEFAULT false,
    occurred_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, event_key)
);

CREATE TABLE alert_notification (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    alert_id      uuid NOT NULL REFERENCES alert_rule(id) ON DELETE CASCADE,
    event_id      uuid REFERENCES alert_event(id) ON DELETE CASCADE,
    channel       text NOT NULL,
    status        text NOT NULL
        CHECK (status IN ('sent', 'failed', 'unconfigured', 'skipped')),
    attempts      integer NOT NULL DEFAULT 1,
    error         text,                       -- safe message only (never secrets)
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- Grants + RLS (same pattern as every other tenant table).
GRANT SELECT, INSERT, UPDATE, DELETE ON alert_rule, alert_destination,
    alert_incident, alert_event, alert_notification TO meter_app;

ALTER TABLE alert_rule         ENABLE ROW LEVEL SECURITY;
ALTER TABLE alert_destination  ENABLE ROW LEVEL SECURITY;
ALTER TABLE alert_incident     ENABLE ROW LEVEL SECURITY;
ALTER TABLE alert_event        ENABLE ROW LEVEL SECURITY;
ALTER TABLE alert_notification ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON alert_rule
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);
CREATE POLICY tenant_isolation ON alert_destination
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);
CREATE POLICY tenant_isolation ON alert_incident
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);
CREATE POLICY tenant_isolation ON alert_event
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);
CREATE POLICY tenant_isolation ON alert_notification
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);

-- Indexes for the evaluator + UI.
CREATE INDEX alert_rule_due_idx      ON alert_rule (next_eval_at) WHERE enabled;
CREATE INDEX alert_rule_tenant_idx   ON alert_rule (tenant_id, status);
CREATE INDEX alert_dest_alert_idx    ON alert_destination (alert_id);
CREATE INDEX alert_incident_rule_idx ON alert_incident (alert_id, status);
CREATE INDEX alert_event_feed_idx    ON alert_event (tenant_id, occurred_at DESC);
CREATE INDEX alert_event_unread_idx  ON alert_event (tenant_id) WHERE NOT read;
CREATE INDEX alert_notif_event_idx   ON alert_notification (event_id);
