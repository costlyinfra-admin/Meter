-- 0042: a record of what discovery actually did, and an optional schedule.
--
-- Until now nothing recorded that discovery had run. The only way to guess at
-- its coverage was to look at the merge dates of the pull requests it happened
-- to find — which cannot tell "we looked at March and there was nothing" from
-- "we never looked at March". A customer staring at an empty activity table had
-- no way to know which.
--
--   discovery_run      — one row per run: the window of merge dates it asked
--                        GitHub for, what it found, whether a person or the
--                        schedule started it, and how it ended. Customer-facing
--                        and RLS-isolated (unlike admin_sync_log, which is the
--                        internal portal's and which tenants cannot read).
--
--   discovery_scope.*  — the schedule lives on the scope row because a scheduled
--                        run needs an owner and a repo list to run against.
--                        A tenant that has never run discovery has no scope row,
--                        and so cannot schedule one, which is the right answer.
--                        Auto is OFF by default: discovery spends the customer's
--                        GitHub rate limit and, with BYOK configured, their LLM
--                        budget, and it can raise new feature proposals for
--                        someone to review. That is not something to start doing
--                        on a schedule nobody asked for.

CREATE TABLE discovery_run (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    owner          text,
    repos          jsonb NOT NULL DEFAULT '[]'::jsonb,
    -- The merge-date window the run requested. `covered_to` is the run's own
    -- day: GitHub is asked for everything merged on or after `covered_from`.
    covered_from   date NOT NULL,
    covered_to     date NOT NULL,
    prs            integer NOT NULL DEFAULT 0,
    proposals      integer NOT NULL DEFAULT 0,
    trigger        text NOT NULL DEFAULT 'manual'
                       CHECK (trigger IN ('manual', 'scheduled')),
    status         text NOT NULL CHECK (status IN ('success', 'error')),
    -- Safe message only: never a token, never a raw provider response body.
    error_message  text,
    started_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz,
    started_by     text
);

GRANT SELECT, INSERT, UPDATE, DELETE ON discovery_run TO annapurna_app;

ALTER TABLE discovery_run ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON discovery_run
    USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid);

CREATE INDEX discovery_run_recent_idx ON discovery_run (tenant_id, started_at DESC);
-- The scheduler's own lookup: due runs across every tenant.
CREATE INDEX discovery_run_success_idx ON discovery_run (tenant_id, covered_to DESC)
    WHERE status = 'success';

-- The optional schedule, on the scope it would run against.
ALTER TABLE discovery_scope ADD COLUMN auto_enabled       boolean NOT NULL DEFAULT false;
-- How far back an automatic run looks. Small on purpose: an incremental run only
-- needs to reach past the last one, with a few days of overlap for PRs that were
-- merged while the previous run was in flight.
ALTER TABLE discovery_scope ADD COLUMN auto_lookback_days integer NOT NULL DEFAULT 14
    CHECK (auto_lookback_days BETWEEN 1 AND 365);
ALTER TABLE discovery_scope ADD COLUMN next_run_at        timestamptz;

COMMENT ON COLUMN discovery_scope.auto_enabled IS
    'Off by default. Discovery costs GitHub rate limit and, with BYOK, LLM spend, '
    'and can raise feature proposals a person then has to review.';
