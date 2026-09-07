-- 0043: rename the application role annapurna_app -> meter_app, and re-key the
-- one piece of stored data that carried the old product name.
--
-- WHY A MIGRATION AND NOT AN EDIT TO 0002:
--   Migration 0002 has already run on the production database, and the runner
--   tracks versions by filename, so editing it would change nothing there. It
--   HAS been edited — along with every later GRANT — so that a *fresh* database
--   creates `meter_app` directly and never knows the old name. This migration is
--   what brings an *existing* database to the same place.
--
--   Both paths therefore converge here:
--     fresh install  -> 0002 creates meter_app; this file finds it and no-ops
--     existing prod  -> 0002..0042 created annapurna_app; this file renames it
--
-- ON THE RENAME ITSELF:
--   ALTER ROLE ... RENAME carries every GRANT and every RLS policy reference
--   with it — policies reference roles by OID, not by name — so tenant isolation
--   is preserved exactly. The one thing a rename can drop is the password: an
--   MD5-hashed password is salted with the role name and is invalidated, while a
--   SCRAM-SHA-256 one survives. deploy/release.sh sets the password immediately
--   after migrations run on every deploy, so either way the app comes up with a
--   working credential. Nothing else needs to know.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'meter_app') THEN
        -- Fresh database: 0002 already created it under the new name.
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'annapurna_app') THEN
            -- Both exist. Only possible if someone created one by hand; say so
            -- rather than guessing which one the grants are on.
            RAISE EXCEPTION
                'Both meter_app and annapurna_app exist. Resolve by hand before deploying.';
        END IF;
    ELSIF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'annapurna_app') THEN
        ALTER ROLE annapurna_app RENAME TO meter_app;
    ELSE
        -- Neither: a database migrated past 0002 with the role dropped. Recreate
        -- it; the grants below in later migrations have already been applied to
        -- a role of this name, so re-granting the base set is enough to boot.
        CREATE ROLE meter_app LOGIN;
    END IF;
END
$$;

-- Belt and braces: the role must be able to read the schema. Idempotent, and a
-- no-op on the two paths above that already carry their grants.
GRANT USAGE ON SCHEMA public TO meter_app;

-- ---------------------------------------------------------------------------
-- Stored data that carried the product name.
--
-- recon_match.classification is free text (no CHECK constraint), and the
-- reconciliation engine wrote the literal 'annapurna_usage_absent_from_statement'
-- for "we metered this usage, the provider never billed it". The engine now
-- writes 'meter_usage_absent_from_statement'; without this update, historical
-- rows would render in the UI as a raw slug instead of "Tracked, not billed".
-- ---------------------------------------------------------------------------
UPDATE recon_match
   SET classification = 'meter_usage_absent_from_statement'
 WHERE classification = 'annapurna_usage_absent_from_statement';
