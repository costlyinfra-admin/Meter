-- 0038: make hook ingest safe to retry.
--
-- ingest_events ADDS to whatever is already stored (_upsert_hook_row does
-- `amount = amount + ...`), and nothing identifies a batch. So replaying one —
-- which is exactly what a retry is — double-counts a feature's cost.
--
-- That asymmetry matters here: reconciliation compares the hook total against
-- the provider's authoritative bill and routes a POSITIVE gap (bill > hook) to
-- Unattributed, so under-counting is caught by design. Over-counting is not: it
-- silently inflates the per-feature numbers the whole product is for. Retrying
-- without this table would trade a caught failure for an uncaught one.
--
-- The SDK sends a batch_id that stays the same across retries of the same batch.
-- First delivery inserts; a replay hits the primary key, is recognised, and
-- returns the ORIGINAL result rather than applying anything again.
--
-- Rows are only useful for as long as a client might still retry (seconds), so
-- ingest prunes anything older than an hour. The table stays small on its own —
-- no cron, no cleanup job.

CREATE TABLE hook_batch (
    tenant_id  uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    batch_id   text NOT NULL,
    accepted   integer NOT NULL DEFAULT 0,   -- the original response, replayed verbatim
    cost       numeric(14, 6) NOT NULL DEFAULT 0,
    seen_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, batch_id)
);

GRANT SELECT, INSERT, UPDATE, DELETE ON hook_batch TO meter_app;

ALTER TABLE hook_batch ENABLE ROW LEVEL SECURITY;
ALTER TABLE hook_batch FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON hook_batch
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);

-- Supports the age-based prune.
CREATE INDEX hook_batch_seen_idx ON hook_batch (tenant_id, seen_at);
