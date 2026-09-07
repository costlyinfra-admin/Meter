-- 0027: organization + privacy settings, stored on the tenant (org) itself.
--
-- The Settings page is now purely administrative. Organization name already
-- exists as `tenant.name`; this adds the remaining org-level preferences and a
-- few privacy controls. All tenant-scoped (every user in the tenant sees the
-- same values), additive, and nullable-safe via NOT NULL DEFAULTs so existing
-- tenants keep working with sensible, privacy-conscious defaults.
--
--   timezone           — IANA zone for display/reporting preferences (default UTC)
--   currency           — reporting currency; USD-only today, column is future-proof
--   customer_id_storage— how customer identifiers will be stored once customer
--                        attribution ships (default 'hashed' — the most private)
--   store_prompts      — whether to store raw prompt content (default OFF; today
--                        Meter stores no prompt text at all, so this is a
--                        forward-looking guarantee, not a toggle over existing data)
--   data_retention     — retention window; enforcement is deferred, so the safe
--                        default is 'indefinite' (never auto-delete existing data)

ALTER TABLE tenant ADD COLUMN timezone            text NOT NULL DEFAULT 'UTC';
ALTER TABLE tenant ADD COLUMN currency            text NOT NULL DEFAULT 'USD';
ALTER TABLE tenant ADD COLUMN customer_id_storage text NOT NULL DEFAULT 'hashed'
    CHECK (customer_id_storage IN ('names', 'aliases', 'hashed'));
ALTER TABLE tenant ADD COLUMN store_prompts       boolean NOT NULL DEFAULT false;
ALTER TABLE tenant ADD COLUMN data_retention      text NOT NULL DEFAULT 'indefinite'
    CHECK (data_retention IN ('30d', '90d', '1y', 'indefinite'));
