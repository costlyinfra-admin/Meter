-- 0044: when an inference_cost row last changed.
--
-- Hook events are aggregated, not stored one by one: ingest adds a batch's
-- tokens and cost onto the monthly row for its (feature, provider, model), so
-- there is no per-event record to point at. `created_at` answers "when did this
-- row first appear", which is not the same question — a second install in the
-- same month updates an existing row and leaves created_at where it was.
--
-- The Install SDK page needs to tell someone whether the SDK they just wired up
-- is reporting. Answering that with created_at would show them the timestamp of
-- their FIRST ever event and call it their latest, which is exactly the sort of
-- number this product is not supposed to print. This column moves whenever the
-- row does.
--
-- Backfilled from created_at: for rows written before this migration those are
-- the same moment, and it keeps the column NOT NULL without inventing a time.

ALTER TABLE inference_cost ADD COLUMN updated_at timestamptz;
UPDATE inference_cost SET updated_at = created_at WHERE updated_at IS NULL;
ALTER TABLE inference_cost ALTER COLUMN updated_at SET NOT NULL;
ALTER TABLE inference_cost ALTER COLUMN updated_at SET DEFAULT now();

-- The Install SDK verification panel's only query: this tenant's newest hook row.
CREATE INDEX inference_cost_hook_recent_idx
    ON inference_cost (tenant_id, updated_at DESC)
    WHERE source = 'hook';
