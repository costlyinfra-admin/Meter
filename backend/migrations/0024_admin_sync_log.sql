-- 0024: admin_sync_log — connector test/sync telemetry for the internal admin
-- portal. A row is written whenever an admin triggers "Test Connection" or
-- "Sync Now" from the portal, giving Sync History and Errors real data.
--
-- Admin-only: accessed exclusively via the RLS-exempt owner connection
-- (admin_dsn). The app role (meter_app) gets NO grant, so a tenant can never
-- read it. Not part of the customer product; no RLS policy needed.

CREATE TABLE admin_sync_log (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    connector_type   text NOT NULL,
    action           text NOT NULL,          -- 'test' | 'sync'
    started_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz,
    records_imported integer,
    status           text NOT NULL,          -- 'success' | 'error'
    error_message    text
);

CREATE INDEX admin_sync_log_recent_idx ON admin_sync_log (started_at DESC);
CREATE INDEX admin_sync_log_tenant_idx ON admin_sync_log (tenant_id, started_at DESC);
