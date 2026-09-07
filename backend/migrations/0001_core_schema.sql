-- Meter M1 — core schema.
--
-- The six core entities from design doc §6, plus a `tenant` table that anchors
-- tenant_id (the seed needs "one fake tenant" and M2 creates tenants on signup).
--
-- Conventions:
--   * Primary keys are uuid (gen_random_uuid(), built into Postgres 13+).
--   * Money is numeric(14,4) in `currency` (default USD) — never floats.
--   * `period` is a DATE pinned to the first day of the month it represents
--     (monthly buckets; build cost is concentrated, inference cost recurs).
--   * `feature_id` is nullable on the cost tables: a NULL feature_id means the
--     spend is in the **Unattributed bucket** (never silently dropped — invariant 4).
--   * confidence is high/med/low on every cost row (invariant 3).

-- ---------------------------------------------------------------------------
-- tenant — the isolation anchor. Every other table's tenant_id references it.
-- ---------------------------------------------------------------------------
CREATE TABLE tenant (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- feature — THE SPINE. Auto-proposed from PR history, then confirmed by a human.
-- ---------------------------------------------------------------------------
CREATE TABLE feature (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    name                  text NOT NULL,
    description           text,
    status                text NOT NULL DEFAULT 'proposed'
                              CHECK (status IN ('proposed', 'confirmed', 'archived')),
    shipped_at            timestamptz,
    -- how sure auto-discovery was when proposing this feature (distinct from
    -- cost-attribution confidence). NULL until a discovery run sets it.
    discovery_confidence  text CHECK (discovery_confidence IN ('high', 'med', 'low')),
    created_at            timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- feature_signal — the evidence trail. Why a number is what it is (invariant 3).
-- ---------------------------------------------------------------------------
CREATE TABLE feature_signal (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    feature_id   uuid NOT NULL REFERENCES feature(id) ON DELETE CASCADE,
    signal_type  text NOT NULL
                     CHECK (signal_type IN ('pr', 'repo', 'branch', 'service',
                                            'api_key', 'usage_tag', 'hook_tag')),
    external_ref text,
    confidence   text CHECK (confidence IN ('high', 'med', 'low')),
    source       text,
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- build_cost — AI coding-tool spend, attributed per developer and PR.
-- feature_id NULL => Unattributed bucket.
-- ---------------------------------------------------------------------------
CREATE TABLE build_cost (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    feature_id    uuid REFERENCES feature(id) ON DELETE SET NULL,
    developer_id  text,
    tool          text NOT NULL
                      CHECK (tool IN ('claude_code', 'cursor', 'copilot', 'codex')),
    pr_ref        text,
    amount        numeric(14, 4) NOT NULL,
    currency      text NOT NULL DEFAULT 'USD',
    period        date NOT NULL,
    confidence    text NOT NULL CHECK (confidence IN ('high', 'med', 'low')),
    source        text,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- inference_cost — prod LLM spend. `source` makes the model hook-ready:
-- cost_api rows are authoritative totals by key; hook rows are metered per call.
-- feature_id NULL => Unattributed bucket.
-- ---------------------------------------------------------------------------
CREATE TABLE inference_cost (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    feature_id     uuid REFERENCES feature(id) ON DELETE SET NULL,
    provider       text NOT NULL CHECK (provider IN ('anthropic', 'openai')),
    model          text,
    api_key_ref    text,
    amount         numeric(14, 4) NOT NULL,
    currency       text NOT NULL DEFAULT 'USD',
    period         date NOT NULL,
    tokens_in      bigint,
    tokens_out     bigint,
    request_count  bigint,
    source         text NOT NULL CHECK (source IN ('cost_api', 'hook')),
    confidence     text NOT NULL CHECK (confidence IN ('high', 'med', 'low')),
    created_at     timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- bill_reconciliation — keeps hook numbers honest against the real provider bill.
-- delta is derived; any non-zero delta flows to the Unattributed bucket (M7).
-- ---------------------------------------------------------------------------
CREATE TABLE bill_reconciliation (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    provider          text NOT NULL CHECK (provider IN ('anthropic', 'openai')),
    period            date NOT NULL,
    billed_total      numeric(14, 4) NOT NULL,
    attributed_total  numeric(14, 4) NOT NULL,
    delta             numeric(14, 4) GENERATED ALWAYS AS (billed_total - attributed_total) STORED,
    status            text NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending', 'balanced', 'delta')),
    created_at        timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- feature_usage — powers the "worth it?" view (cost per active user).
-- ---------------------------------------------------------------------------
CREATE TABLE feature_usage (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    feature_id    uuid NOT NULL REFERENCES feature(id) ON DELETE CASCADE,
    period        date NOT NULL,
    active_users  integer,
    events        bigint,
    source        text,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Indexes — every tenant-scoped lookup filters by tenant_id; joins use feature_id.
-- ---------------------------------------------------------------------------
CREATE INDEX feature_tenant_idx              ON feature (tenant_id);
CREATE INDEX feature_signal_tenant_idx       ON feature_signal (tenant_id);
CREATE INDEX feature_signal_feature_idx      ON feature_signal (feature_id);
CREATE INDEX build_cost_tenant_idx           ON build_cost (tenant_id);
CREATE INDEX build_cost_feature_idx          ON build_cost (feature_id);
CREATE INDEX build_cost_period_idx           ON build_cost (tenant_id, period);
CREATE INDEX inference_cost_tenant_idx       ON inference_cost (tenant_id);
CREATE INDEX inference_cost_feature_idx      ON inference_cost (feature_id);
CREATE INDEX inference_cost_period_idx       ON inference_cost (tenant_id, period);
CREATE INDEX bill_reconciliation_tenant_idx  ON bill_reconciliation (tenant_id);
CREATE INDEX feature_usage_tenant_idx        ON feature_usage (tenant_id);
CREATE INDEX feature_usage_feature_idx       ON feature_usage (feature_id);
