-- Meter — an audit trail for the MCP server, and the means to write one.
--
-- "What did that agent read, and when?" is the first question anyone asks about
-- giving a coding agent access to their cost data, and until now Meter had no
-- answer beyond a token's last_used_at. This records every tool call: which
-- token, which tool, what it asked for, whether it worked.
--
-- It deliberately records ARGUMENTS, not results. The arguments are ids,
-- periods and dimension names — the shape of the question — and they are what
-- makes the trail useful. Results would be a second copy of the customer's cost
-- data with a different retention story.
--
-- The write goes through a SECURITY DEFINER function for the same reason the
-- token lookup does (0060): the MCP server holds `meter_read`, which has no
-- INSERT privilege anywhere, and keeping it that way matters more than the
-- convenience of a direct INSERT. A role that can write its own audit trail is
-- a role that can write.

CREATE TABLE mcp_audit (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    -- Which credential. Kept when the token is revoked (revocation is a
    -- timestamp, not a delete), so the trail survives turning a token off.
    token_id    uuid REFERENCES mcp_token(id) ON DELETE SET NULL,
    -- 'stdio' for a locally spawned server, 'http' for the hosted endpoint.
    transport   text NOT NULL CHECK (transport IN ('stdio', 'http')),
    tool        text NOT NULL CHECK (length(tool) BETWEEN 1 AND 100),
    -- Truncated and stored as text, not jsonb: this is a log line, never
    -- queried by shape, and a caller-controlled object should not become a
    -- caller-controlled index entry.
    arguments   text CHECK (arguments IS NULL OR length(arguments) <= 500),
    outcome     text NOT NULL CHECK (outcome IN ('ok', 'error', 'rate_limited')),
    duration_ms integer CHECK (duration_ms IS NULL OR duration_ms >= 0),
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX mcp_audit_tenant_time_idx ON mcp_audit (tenant_id, created_at DESC);
CREATE INDEX mcp_audit_token_idx ON mcp_audit (token_id);

GRANT SELECT, INSERT, UPDATE, DELETE ON mcp_audit TO meter_app;

ALTER TABLE mcp_audit ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON mcp_audit
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);

-- The writer. SECURITY DEFINER so it runs as the tables' owner; the tenant and
-- token are taken from the caller's own resolved token, never from anything a
-- client sent, so a caller cannot write a line into someone else's trail.
CREATE FUNCTION mcp_audit_log(
    p_tenant_id   uuid,
    p_token_id    uuid,
    p_transport   text,
    p_tool        text,
    p_arguments   text,
    p_outcome     text,
    p_duration_ms integer
) RETURNS void
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    INSERT INTO mcp_audit
        (tenant_id, token_id, transport, tool, arguments, outcome, duration_ms)
    VALUES
        (p_tenant_id, p_token_id, p_transport, p_tool, left(p_arguments, 500),
         p_outcome, p_duration_ms);
$$;

REVOKE ALL ON FUNCTION mcp_audit_log(uuid, uuid, text, text, text, text, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mcp_audit_log(uuid, uuid, text, text, text, text, integer)
    TO meter_read, meter_app;

-- `mcp_resolve_token` returned only the tenant. The audit trail needs to name
-- the credential too, so it now returns both. Replaced rather than added
-- alongside: two resolvers would drift.
DROP FUNCTION mcp_resolve_token(text);

CREATE FUNCTION mcp_resolve_token(p_hash text)
RETURNS TABLE (tenant_id uuid, token_id uuid)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    UPDATE mcp_token
       SET last_used_at = now()
     WHERE token_hash = p_hash
       AND revoked_at IS NULL
    RETURNING mcp_token.tenant_id, mcp_token.id;
$$;

REVOKE ALL ON FUNCTION mcp_resolve_token(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mcp_resolve_token(text) TO meter_read, meter_app;
