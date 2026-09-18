-- 0057: human_effort — what it COSTS IN PEOPLE to serve a customer.
--
-- Meter has always answered "what did the machines cost?". For a company selling
-- an AI product, the model bill is only half of delivery: the other half is the
-- engineer who built the customer's workflow change, the person who sat on the
-- deployment call, and the rework after it went wrong. That labour is invisible
-- here, which makes a cheap-to-run customer look profitable when it is not.
--
-- A THIRD COST CATEGORY, not a third opinion about an existing one.
--
--   * It is NOT build cost. Build cost is what the team spent MAKING a feature,
--     allocated from coding-tool spend by PR overlap, and it has no customer —
--     invariant 2 keeps it separate from inference and it stays separate here.
--     Nothing in this table is ever added to build_cost or attributed back to it.
--   * It is NOT inference. It never touches inference_cost, the reconciliation
--     against the provider bill, or the Unattributed bucket.
--   * It is declared, not derived. Every other cost row in this schema carries a
--     `confidence` grading an INFERRED attribution; these rows are a person
--     stating what they worked on and at what loaded rate, so confidence would
--     be grading the customer's own bookkeeping. Invariant 3's real requirement
--     — that no number is a black box — is met by `source`, `import_batch_id`
--     and the stored `loaded_hourly_rate`: every dollar traces to one uploaded
--     row and the rate that priced it, and the rate is stored per row so a later
--     rate change never silently reprices history.
--
-- WHY customer_id IS TEXT WITH NO FOREIGN KEY. There is no customer table, by
-- design: `customer_id` is whatever identifier the tenant already passes on
-- metered calls (customer_cost.customer_id is the same shape). Inventing a
-- master-data table to hold a foreign key would be a bigger product decision
-- than this feature, and would reject effort logged against a customer whose
-- calls are not instrumented yet — which is exactly the customer this is most
-- useful for.
--
-- WHY feature_id IS NULLABLE. Effort attributes to a CUSTOMER. Some of it maps
-- to a feature and some genuinely does not (a deployment call, a support
-- escalation). Forcing a feature would mean inventing one. ON DELETE SET NULL,
-- so deleting a feature loses the link and never the cost.

CREATE TABLE human_effort (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    -- The day the work happened. Rolled into monthly buckets at read time, the
    -- same way every other cost table is read, but stored at day precision
    -- because that is what a timesheet export carries.
    work_date          date NOT NULL,
    -- The tenant's own customer identifier; see above.
    customer_id        text NOT NULL CHECK (length(customer_id) BETWEEN 1 AND 200),
    -- Who did the work, as the tenant labels them. Deliberately a free label
    -- rather than a link to build_developer: that table is GitHub identities for
    -- code attribution, and support and review hours come from people who may
    -- never appear in it.
    person_label       text NOT NULL CHECK (length(person_label) BETWEEN 1 AND 200),
    feature_id         uuid REFERENCES feature(id) ON DELETE SET NULL,
    activity_type      text NOT NULL
        CHECK (activity_type IN ('development', 'support', 'review', 'rework', 'other')),
    hours              numeric(8, 2) NOT NULL CHECK (hours > 0),
    -- Fully loaded cost per hour, as supplied by the tenant. Stored per row, not
    -- per person: it is an input to this row's cost and must not move when a
    -- salary does.
    loaded_hourly_rate numeric(12, 4) NOT NULL CHECK (loaded_hourly_rate >= 0),
    source             text NOT NULL CHECK (source IN ('manual', 'csv')),
    note               text CHECK (note IS NULL OR length(note) <= 500),
    import_batch_id    uuid,
    created_at         timestamptz NOT NULL DEFAULT now()
);

-- The read path is always "this tenant, this month range", then grouped by
-- customer. Both indexes serve that; the second also serves a single customer.
CREATE INDEX human_effort_period_idx ON human_effort (tenant_id, work_date);
CREATE INDEX human_effort_customer_idx ON human_effort (tenant_id, customer_id, work_date);
CREATE INDEX human_effort_feature_idx ON human_effort (tenant_id, feature_id);
CREATE INDEX human_effort_batch_idx ON human_effort (tenant_id, import_batch_id);

GRANT SELECT, INSERT, UPDATE, DELETE ON human_effort TO meter_app;

ALTER TABLE human_effort ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON human_effort
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);

-- One import, recorded so the same file cannot be loaded twice.
--
-- The precedent is hook_batch (0038): a retry that silently doubles a number is
-- worse than a retry that fails, because nothing downstream can tell. Here the
-- risk is a person clicking Import twice, or pasting last month's file again —
-- both of which would double a customer's labour cost with no way to notice.
-- The checksum is of the file's content, so the second attempt is recognised
-- whatever it was named.
--
-- Unlike hook_batch these rows are kept: they are the evidence trail for the
-- effort rows that reference them, not a short-lived retry window.
CREATE TABLE human_effort_import (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    checksum    text NOT NULL CHECK (length(checksum) = 64),   -- sha256 hex
    row_count   integer NOT NULL DEFAULT 0,
    imported_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, checksum)
);

GRANT SELECT, INSERT, UPDATE, DELETE ON human_effort_import TO meter_app;

ALTER TABLE human_effort_import ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON human_effort_import
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);
