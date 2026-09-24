-- Meter — a credential and a database role for the read-only MCP server.
--
-- The MCP server (backend/meter/mcp) lets a coding agent read this
-- organization's cost and optimization data. Until now it authenticated with a
-- person's email and password and connected as `meter_app`, which can write
-- every table. Both are more than it needs.
--
-- This migration gives it exactly what it needs and nothing else:
--
--   1. `mcp_token`    — a revocable, labelled, per-tenant credential. Only the
--                       SHA-256 hash is stored, like `hook_token` (0005).
--   2. `meter_read`   — a database role with SELECT and no INSERT, UPDATE or
--                       DELETE anywhere. Read-only stops being a property of
--                       the Python and becomes a property of the connection.
--   3. `mcp_resolve_token()` — how a process holding only `meter_read` turns a
--                       token into a tenant, without being able to read the
--                       token table itself.
--
-- Tenant isolation is unchanged: `meter_read` is a non-owner, so every existing
-- RLS policy governs it exactly as it governs `meter_app`.

-- ---------------------------------------------------------------------------
-- 1. The credential
-- ---------------------------------------------------------------------------
CREATE TABLE mcp_token (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    -- Which machine or person this token is for. A tenant will have several —
    -- one per laptop — and revoking the right one means being able to tell
    -- them apart.
    label         text NOT NULL CHECK (length(label) BETWEEN 1 AND 100),
    token_hash    text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    -- Revoked rather than deleted: "this token was used until Tuesday, then
    -- turned off" is the answer to a security question, and DELETE destroys it.
    revoked_at    timestamptz,
    -- Answers "is anyone still using this?" before revoking, and "when did the
    -- agent last read our data?" afterwards. Written on every resolve.
    last_used_at  timestamptz
);

CREATE UNIQUE INDEX mcp_token_hash_key ON mcp_token (token_hash);
CREATE INDEX mcp_token_tenant_idx ON mcp_token (tenant_id);

GRANT SELECT, INSERT, UPDATE, DELETE ON mcp_token TO meter_app;

-- ENABLE but not FORCE, matching 0006: the owner is exempt, which is what lets
-- the resolver below look a token up before any tenant context exists.
ALTER TABLE mcp_token ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON mcp_token
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);

-- ---------------------------------------------------------------------------
-- 2. The read-only role
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'meter_read') THEN
        CREATE ROLE meter_read LOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO meter_read;

-- An explicit list, not GRANT ON ALL TABLES. A blanket grant would hand this
-- role `app_user.password_hash`, `hook_token`, the provider credentials and the
-- prompt-optimization tables — none of which any read tool touches, and all of
-- which are worse to leak than the numbers this role exists to serve.
--
-- The list is the tables the three MCP tools reach through dashboard.py,
-- optimize_measured.py, optimize_billing.py and ai_reads.py. It is not
-- maintained by hand alone: test_mcp_read_role.py runs every tool as this role,
-- so a tool that needs a new table fails loudly rather than in production.
GRANT SELECT ON
    tenant,
    feature, feature_signal, feature_usage,
    build_cost, inference_cost, inference_cost_daily,
    customer_cost, human_effort,
    usage_signal, optimization_action,
    product, product_repo,
    ai_application, ai_trace, ai_span,
    discovery_run, discovery_scope,
    alert_rule
TO meter_read;

-- The Overview asks whether a GitHub connector exists — `EXISTS (SELECT 1 ...)`
-- and nothing more. A column grant gives it that without giving it the
-- encrypted credential sitting in the same row.
GRANT SELECT (id, tenant_id, connector_type, label, created_at, updated_at)
    ON connector_credential TO meter_read;

-- ---------------------------------------------------------------------------
-- 3. Resolving a token without being able to read the token table
-- ---------------------------------------------------------------------------
-- SECURITY DEFINER, so it runs as this migration's role (the tables' owner) and
-- is exempt from RLS — a token has to be resolved BEFORE there is a tenant
-- context to resolve it under. `meter_read` gets EXECUTE on the function and no
-- privilege at all on the table, so a process holding that role can turn its own
-- token into its own tenant id and cannot enumerate anyone else's.
--
-- search_path is pinned: an unpinned SECURITY DEFINER function can be pointed at
-- a caller-controlled schema.
CREATE FUNCTION mcp_resolve_token(p_hash text)
RETURNS uuid
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    UPDATE mcp_token
       SET last_used_at = now()
     WHERE token_hash = p_hash
       AND revoked_at IS NULL
    RETURNING tenant_id;
$$;

REVOKE ALL ON FUNCTION mcp_resolve_token(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mcp_resolve_token(text) TO meter_read, meter_app;
