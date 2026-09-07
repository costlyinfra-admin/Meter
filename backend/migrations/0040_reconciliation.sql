-- 0040: provider invoice reconciliation (additive, opt-in, isolated).
--
-- Compares an official provider billing export against the spend Meter
-- already tracks, and explains the difference. Everything it needs lives in
-- these tables: the module READS inference_cost_daily and writes nothing back,
-- so no existing row, column, total or query changes because this exists.
--
-- The whole module is off unless a tenant has recon_settings.enabled = true,
-- which defaults to false. An installation that never touches Settings sees no
-- navigation, no routes and no jobs, and needs no backfill.
--
-- Money is numeric(18,6) rather than the numeric(14,4) used for tracked spend:
-- a provider statement carries its own precision (unit prices, tax lines), and
-- rounding it on the way in would manufacture the very discrepancies this
-- module exists to explain.

-- ---------------------------------------------------------------------------
-- Per-tenant switch and tolerances. One row per tenant, created on first use.
-- ---------------------------------------------------------------------------
CREATE TABLE recon_settings (
    tenant_id      uuid PRIMARY KEY REFERENCES tenant(id) ON DELETE CASCADE,
    enabled        boolean NOT NULL DEFAULT false,
    -- Conservative defaults: a dollar, or half a percent, whichever is larger
    -- at the amount in question. Both are applied, and both are copied onto
    -- every run so a historical result can always be read back in its own terms.
    tolerance_abs  numeric(18, 6) NOT NULL DEFAULT 1.000000,
    tolerance_pct  numeric(6, 3)  NOT NULL DEFAULT 0.500,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    updated_by     text
);

-- ---------------------------------------------------------------------------
-- One committed billing export.
-- ---------------------------------------------------------------------------
CREATE TABLE recon_import (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    provider          text NOT NULL,
    provider_account  text,                       -- workspace/org id on the statement
    filename          text NOT NULL,              -- sanitised; never used as a path
    checksum          text NOT NULL,              -- sha256 of the file bytes
    status            text NOT NULL DEFAULT 'committed'
        CHECK (status IN ('committed', 'superseded', 'removed')),
    source_type       text NOT NULL DEFAULT 'csv_upload',  -- room for api/object-store later
    currency          text NOT NULL,
    period_start      date,
    period_end        date,
    imported_by       text,
    imported_at       timestamptz NOT NULL DEFAULT now(),
    removed_at        timestamptz,                -- recoverable deletion: row stays
    row_count         integer NOT NULL DEFAULT 0,
    rejected_count    integer NOT NULL DEFAULT 0,
    metadata          jsonb NOT NULL DEFAULT '{}'::jsonb,
    validation_errors jsonb NOT NULL DEFAULT '[]'::jsonb
);

-- The same file cannot be committed twice for a provider while it is live.
-- Superseded and removed imports are excluded so a corrected re-import works.
CREATE UNIQUE INDEX recon_import_dedupe_idx
    ON recon_import (tenant_id, provider, checksum) WHERE status = 'committed';
CREATE INDEX recon_import_tenant_idx ON recon_import (tenant_id, provider, period_start);

-- ---------------------------------------------------------------------------
-- The statement's rows, normalised. `raw` keeps what the file actually said.
-- ---------------------------------------------------------------------------
CREATE TABLE recon_line_item (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    import_id         uuid NOT NULL REFERENCES recon_import(id) ON DELETE CASCADE,
    row_number        integer NOT NULL,           -- 1-based line in the source file
    service_date      date,
    period_start      date,
    period_end        date,
    provider_account  text,
    api_key_ref       text,
    model             text,
    usage_category    text,
    quantity          numeric(20, 4),
    -- Financial categories stay apart, always. The usage comparison uses
    -- usage_subtotal alone; tax, credits and fees are never treated as usage.
    usage_subtotal    numeric(18, 6) NOT NULL DEFAULT 0,
    credit            numeric(18, 6) NOT NULL DEFAULT 0,
    tax               numeric(18, 6) NOT NULL DEFAULT 0,
    fee               numeric(18, 6) NOT NULL DEFAULT 0,
    adjustment        numeric(18, 6) NOT NULL DEFAULT 0,
    billed_amount     numeric(18, 6) NOT NULL DEFAULT 0,
    currency          text NOT NULL,
    statement_id      text,
    line_item_id      text,
    raw               jsonb NOT NULL DEFAULT '{}'::jsonb,
    mapping_status    text NOT NULL DEFAULT 'ok' CHECK (mapping_status IN ('ok', 'rejected')),
    mapping_errors    jsonb NOT NULL DEFAULT '[]'::jsonb
);

CREATE INDEX recon_line_item_import_idx ON recon_line_item (tenant_id, import_id, row_number);

-- ---------------------------------------------------------------------------
-- One calculation. Immutable: recalculating writes a new row and leaves this one.
-- ---------------------------------------------------------------------------
CREATE TABLE recon_run (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    import_id         uuid REFERENCES recon_import(id) ON DELETE SET NULL,
    provider          text NOT NULL,
    provider_account  text,
    period_start      date NOT NULL,
    period_end        date NOT NULL,
    currency          text NOT NULL,
    status            text NOT NULL
        CHECK (status IN ('pending', 'matched', 'within_tolerance', 'discrepancy',
                          'incomplete_data', 'failed')),
    -- The tolerances in force when this ran, not the ones in force now.
    tolerance_abs     numeric(18, 6) NOT NULL,
    tolerance_pct     numeric(6, 3) NOT NULL,
    provider_usage    numeric(18, 6) NOT NULL DEFAULT 0,
    provider_credits  numeric(18, 6) NOT NULL DEFAULT 0,
    provider_tax      numeric(18, 6) NOT NULL DEFAULT 0,
    provider_fees     numeric(18, 6) NOT NULL DEFAULT 0,
    provider_total    numeric(18, 6) NOT NULL DEFAULT 0,
    tracked_usage     numeric(18, 6) NOT NULL DEFAULT 0,
    usage_difference  numeric(18, 6) NOT NULL DEFAULT 0,
    usage_difference_pct numeric(10, 4),
    unmatched_provider_count integer NOT NULL DEFAULT 0,
    unmatched_tracked_count  integer NOT NULL DEFAULT 0,
    created_by        text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    completed_at      timestamptz,
    failure_reason    text
);

CREATE INDEX recon_run_tenant_idx
    ON recon_run (tenant_id, provider, period_start DESC, created_at DESC);

-- ---------------------------------------------------------------------------
-- Every comparison the run made — matched or not — with its evidence.
--
-- The reference to tracked spend is deliberately NOT a foreign key: this module
-- must never constrain, lock or cascade into the existing cost tables. It is a
-- descriptive key (day/model/account) recorded for explanation only.
-- ---------------------------------------------------------------------------
CREATE TABLE recon_match (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    run_id           uuid NOT NULL REFERENCES recon_run(id) ON DELETE CASCADE,
    line_item_id     uuid REFERENCES recon_line_item(id) ON DELETE SET NULL,
    strategy         text NOT NULL
        CHECK (strategy IN ('line_item_id', 'account_key_date_model', 'account_date_model',
                            'aggregate', 'unmatched_provider', 'unmatched_tracked')),
    dimensions       jsonb NOT NULL DEFAULT '{}'::jsonb,   -- what was compared on
    provider_amount  numeric(18, 6) NOT NULL DEFAULT 0,
    tracked_amount   numeric(18, 6) NOT NULL DEFAULT 0,
    difference       numeric(18, 6) NOT NULL DEFAULT 0,
    difference_pct   numeric(10, 4),
    classification   text NOT NULL,
    explanation      text NOT NULL DEFAULT '',
    confidence       text NOT NULL CHECK (confidence IN ('confirmed', 'possible', 'unknown')),
    evidence         jsonb NOT NULL DEFAULT '[]'::jsonb
);

CREATE INDEX recon_match_run_idx ON recon_match (tenant_id, run_id, classification);

-- ---------------------------------------------------------------------------
-- Who did what. Append-only.
-- ---------------------------------------------------------------------------
CREATE TABLE recon_audit (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    event      text NOT NULL,
    actor      text,
    import_id  uuid,
    run_id     uuid,
    detail     jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX recon_audit_tenant_idx ON recon_audit (tenant_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- Grants + per-tenant isolation, identical to every other tenant table here.
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE ON
    recon_settings, recon_import, recon_line_item, recon_run, recon_match, recon_audit
    TO meter_app;

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['recon_settings', 'recon_import', 'recon_line_item',
                             'recon_run', 'recon_match', 'recon_audit']
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
