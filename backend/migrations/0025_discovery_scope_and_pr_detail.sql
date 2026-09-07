-- 0025: sharper GitHub feature discovery.
--
-- (a) Persist each PR signal's product context — title, branch, URL — so the review
--     UI can show "#202 Notifications system" (with a link) instead of a bare
--     "owner/repo#202" ref. Nullable; older signals just have NULLs.
-- (b) Remember the per-tenant repository SCOPE (which org + repos the user chose)
--     so a re-run analyzes the same repos, not the whole organization.

ALTER TABLE feature_signal ADD COLUMN title  text;
ALTER TABLE feature_signal ADD COLUMN branch text;
ALTER TABLE feature_signal ADD COLUMN url    text;

CREATE TABLE discovery_scope (
    tenant_id  uuid PRIMARY KEY REFERENCES tenant(id) ON DELETE CASCADE,
    owner      text NOT NULL,
    repos      jsonb NOT NULL DEFAULT '[]'::jsonb,  -- selected "owner/name" full names
    updated_at timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE, DELETE ON discovery_scope TO meter_app;

ALTER TABLE discovery_scope ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON discovery_scope
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);
