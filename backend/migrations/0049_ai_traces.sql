-- 0049: request-level AI economics — applications, traces and spans.
--
-- Meter could say what a FEATURE cost. It could not say why one agent run cost
-- $1.42, which step of a workflow spent it, or whether an agent is still
-- running. The hook aggregated individual model calls straight into monthly
-- totals, and the workflow that produced them was gone by the time the row was
-- written. These three tables keep it:
--
--   ai_application  the product surface a workflow belongs to ("support-agent")
--   ai_trace        one agent or workflow run
--   ai_span         one step inside it — an LLM call, a retrieval, a tool
--
-- WHAT IS DELIBERATELY ABSENT. There is no column here for prompt text,
-- response text, messages, tool arguments, tool results, retrieved documents,
-- exception messages or stack traces. Not a nullable one, not an empty JSONB.
-- A column that exists is a column something eventually writes to, and the
-- privacy guarantee this milestone makes is that the content is not in the
-- database to leak. `prompt_hash` is a one-way identity for comparing prompt
-- VERSIONS — it is salted per tenant (0020's opt_salt) precisely so a short
-- prompt cannot be recovered from it by dictionary attack.
--
-- WHY `stale` IS NOT A STATUS. An agent that has gone quiet has not failed; it
-- may be waiting on a slow tool and may still finish successfully. Persisting
-- staleness would mean a background job writing a state that a late heartbeat
-- then has to undo, and a customer seeing "error" for something still working.
-- Status holds only what actually happened; staleness is derived at read time
-- from last_activity_at against the tenant's threshold.

-- ---------------------------------------------------------------------------
-- ai_application
-- ---------------------------------------------------------------------------
CREATE TABLE ai_application (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    -- The SDK identifies an application by `slug`, which is stable and safe to
    -- put in a URL. `name` is what a person sees and may be renamed freely --
    -- renaming must never re-point the SDK's events at a new application.
    name         text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
    slug         text NOT NULL CHECK (slug ~ '^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$'),
    description  text CHECK (description IS NULL OR length(description) <= 500),
    owner        text CHECK (owner IS NULL OR length(owner) <= 200),
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, slug)
);

GRANT SELECT, INSERT, UPDATE, DELETE ON ai_application TO meter_app;
ALTER TABLE ai_application ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON ai_application
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);

-- ---------------------------------------------------------------------------
-- ai_trace — one agent/workflow run
-- ---------------------------------------------------------------------------
CREATE TABLE ai_trace (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    application_id    uuid NOT NULL REFERENCES ai_application(id) ON DELETE CASCADE,
    -- Nullable and ON DELETE SET NULL: a trace whose feature was deleted is
    -- still real spend and still evidence. It becomes Unattributed, as
    -- everything else in this product does, rather than disappearing.
    feature_id        uuid REFERENCES feature(id) ON DELETE SET NULL,

    -- The client generates this so a trace can be referred to before the server
    -- has seen it — which is what makes out-of-order and resumed events work.
    external_trace_id text NOT NULL CHECK (length(external_trace_id) BETWEEN 1 AND 128),
    operation_name    text NOT NULL CHECK (length(operation_name) BETWEEN 1 AND 200),
    customer_ref      text CHECK (customer_ref IS NULL OR length(customer_ref) <= 200),
    environment       text NOT NULL DEFAULT 'production'
                          CHECK (length(environment) BETWEEN 1 AND 60),
    release_version   text CHECK (release_version IS NULL OR length(release_version) <= 120),

    status            text NOT NULL DEFAULT 'running'
                          CHECK (status IN ('running', 'success', 'error', 'cancelled')),

    started_at        timestamptz NOT NULL,
    ended_at          timestamptz,
    duration_ms       bigint CHECK (duration_ms IS NULL OR duration_ms >= 0),
    -- Any lifecycle event for this trace moves last_activity_at; only an
    -- explicit heartbeat moves last_heartbeat_at. Staleness reads the former,
    -- so a busy agent that never heartbeats is still correctly seen as alive.
    last_activity_at  timestamptz NOT NULL DEFAULT now(),
    last_heartbeat_at timestamptz,
    current_span_id   text CHECK (current_span_id IS NULL OR length(current_span_id) <= 128),

    total_cost        numeric(14, 4) NOT NULL DEFAULT 0 CHECK (total_cost >= 0),
    total_tokens      bigint NOT NULL DEFAULT 0 CHECK (total_tokens >= 0),
    span_count        integer NOT NULL DEFAULT 0 CHECK (span_count >= 0),

    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, external_trace_id)
);

GRANT SELECT, INSERT, UPDATE, DELETE ON ai_trace TO meter_app;
ALTER TABLE ai_trace ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON ai_trace
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);

-- ---------------------------------------------------------------------------
-- ai_span — one step inside a run
-- ---------------------------------------------------------------------------
CREATE TABLE ai_span (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    trace_id          uuid NOT NULL REFERENCES ai_trace(id) ON DELETE CASCADE,

    external_span_id  text NOT NULL CHECK (length(external_span_id) BETWEEN 1 AND 128),
    -- Deliberately NOT a foreign key to ai_span. A child can arrive before its
    -- parent, and a workflow whose parent event was lost must still show its
    -- children rather than fail ingestion. The UI renders an unresolved parent
    -- as a root; the relationship stays visible either way.
    parent_span_id    text CHECK (parent_span_id IS NULL OR length(parent_span_id) <= 128),

    span_kind         text NOT NULL
                          CHECK (span_kind IN ('workflow', 'llm', 'embedding',
                                               'retrieval', 'tool', 'guardrail',
                                               'evaluation')),
    operation_name    text NOT NULL CHECK (length(operation_name) BETWEEN 1 AND 200),
    provider          text CHECK (provider IS NULL OR length(provider) <= 60),
    model             text CHECK (model IS NULL OR length(model) <= 120),

    tokens_in           bigint CHECK (tokens_in IS NULL OR tokens_in >= 0),
    tokens_out          bigint CHECK (tokens_out IS NULL OR tokens_out >= 0),
    cache_read_tokens   bigint CHECK (cache_read_tokens IS NULL OR cache_read_tokens >= 0),
    cache_write_tokens  bigint CHECK (cache_write_tokens IS NULL OR cache_write_tokens >= 0),
    reasoning_tokens    bigint CHECK (reasoning_tokens IS NULL OR reasoning_tokens >= 0),

    amount            numeric(14, 4) NOT NULL DEFAULT 0 CHECK (amount >= 0),
    latency_ms        bigint CHECK (latency_ms IS NULL OR latency_ms >= 0),
    status            text NOT NULL DEFAULT 'running'
                          CHECK (status IN ('running', 'success', 'error', 'cancelled')),

    -- Prompt IDENTITY, never prompt content. `prompt_hash` is salted with the
    -- tenant's opt_salt before storage so it cannot be reversed by trying
    -- likely prompts against it.
    prompt_id         text CHECK (prompt_id IS NULL OR length(prompt_id) <= 200),
    prompt_version    text CHECK (prompt_version IS NULL OR length(prompt_version) <= 60),
    prompt_hash       text CHECK (prompt_hash IS NULL OR length(prompt_hash) <= 64),

    started_at        timestamptz NOT NULL,
    ended_at          timestamptz,
    -- The billing instant: which month/day this span's cost belongs to. Kept
    -- separate from started_at so a span that begins before midnight and ends
    -- after it lands in exactly one period, deterministically.
    occurred_at       timestamptz NOT NULL,

    -- Set once, when the span is first finalized with a price. It is what makes
    -- financial aggregation exactly-once: a replayed completion sees this and
    -- adds nothing.
    costed_at         timestamptz,

    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (trace_id, external_span_id)
);

GRANT SELECT, INSERT, UPDATE, DELETE ON ai_span TO meter_app;
ALTER TABLE ai_span ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON ai_span
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);

-- ---------------------------------------------------------------------------
-- Indexes. Each one answers a question the product actually asks.
-- ---------------------------------------------------------------------------
CREATE INDEX ai_application_tenant_idx  ON ai_application (tenant_id, name);

-- "newest first", the default listing.
CREATE INDEX ai_trace_recent_idx        ON ai_trace (tenant_id, started_at DESC);
CREATE INDEX ai_trace_app_idx           ON ai_trace (tenant_id, application_id, started_at DESC);
CREATE INDEX ai_trace_feature_idx       ON ai_trace (tenant_id, feature_id, started_at DESC);
CREATE INDEX ai_trace_status_idx        ON ai_trace (tenant_id, status, started_at DESC);
CREATE INDEX ai_trace_env_idx           ON ai_trace (tenant_id, environment, started_at DESC);
CREATE INDEX ai_trace_release_idx       ON ai_trace (tenant_id, release_version, started_at DESC);
CREATE INDEX ai_trace_customer_idx      ON ai_trace (tenant_id, customer_ref, started_at DESC);
-- "most expensive", "most tokens", "slowest", "most steps".
CREATE INDEX ai_trace_cost_idx          ON ai_trace (tenant_id, total_cost DESC);
CREATE INDEX ai_trace_tokens_idx        ON ai_trace (tenant_id, total_tokens DESC);
CREATE INDEX ai_trace_duration_idx      ON ai_trace (tenant_id, duration_ms DESC);
CREATE INDEX ai_trace_steps_idx         ON ai_trace (tenant_id, span_count DESC);
-- "which agents are running now", and the staleness sweep. Partial: running
-- traces are a tiny fraction of history and this is polled every few seconds.
CREATE INDEX ai_trace_running_idx       ON ai_trace (tenant_id, last_activity_at DESC)
    WHERE status = 'running';
-- The retention sweep, which walks oldest-first within a tenant.
CREATE INDEX ai_trace_retention_idx     ON ai_trace (tenant_id, started_at);

-- The trace-detail waterfall, in order.
CREATE INDEX ai_span_trace_idx          ON ai_span (trace_id, started_at);
CREATE INDEX ai_span_parent_idx         ON ai_span (trace_id, parent_span_id);
CREATE INDEX ai_span_model_idx          ON ai_span (tenant_id, provider, model, occurred_at DESC);
CREATE INDEX ai_span_prompt_idx         ON ai_span (tenant_id, prompt_id, prompt_version)
    WHERE prompt_id IS NOT NULL;
CREATE INDEX ai_span_kind_idx           ON ai_span (tenant_id, span_kind, occurred_at DESC);
CREATE INDEX ai_span_cost_idx           ON ai_span (tenant_id, amount DESC);

-- ---------------------------------------------------------------------------
-- Tenant settings for this milestone.
-- ---------------------------------------------------------------------------
-- How long a running trace may be quiet before the UI calls it stale. A read
-- time threshold, not a state: see the note at the top of this file.
ALTER TABLE tenant ADD COLUMN agent_stale_after_minutes integer NOT NULL DEFAULT 10
    CHECK (agent_stale_after_minutes BETWEEN 1 AND 1440);

-- How long request-level evidence is kept. Financial aggregates are NOT
-- governed by this — deleting traces must never change what a month cost.
ALTER TABLE tenant ADD COLUMN trace_retention_days integer NOT NULL DEFAULT 30
    CHECK (trace_retention_days IN (7, 30, 90));

-- The content-capture policy. 'redacted' and 'full' are reserved so the column
-- does not need changing when a consented capture feature is designed; this
-- milestone enforces 'disabled' everywhere and exposes no control to change it.
-- Deliberately independent of `store_prompts` (0027): that setting governs a
-- different, older thing, and content must stay absent regardless of it.
ALTER TABLE tenant ADD COLUMN content_capture text NOT NULL DEFAULT 'disabled'
    CHECK (content_capture IN ('disabled', 'redacted', 'full'));

COMMENT ON COLUMN ai_span.prompt_hash IS
    'Salted one-way prompt identity for comparing versions. Never reversible to content.';
COMMENT ON COLUMN ai_span.costed_at IS
    'Set once when the span is first priced. Makes financial aggregation exactly-once.';
COMMENT ON COLUMN tenant.content_capture IS
    'Enforced as disabled this milestone. Reserved values exist so a future '
    'consented capture feature needs no schema change.';
