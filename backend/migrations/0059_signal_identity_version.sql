-- 0059: record WHICH request identity a signal was computed from, and whether
-- anybody said the two calls could be reused for each other.
--
-- The duplicate-call finding is only as good as the fingerprint behind it, and
-- v1's fingerprint was provider + model + messages. Nothing else. A call
-- differing only in temperature, system, tools, max_tokens, stop,
-- response_format or seed hashed identically and was counted as an avoidable
-- repeat — all of which change the output. Rows already in this table were
-- computed that way and cannot be upgraded: the fields that would decide it
-- were never sent. They are marked 'v1' and the detector leaves them out of the
-- finding rather than letting an incomplete match be presented as an exact one.
--
-- They are NOT deleted. They are real history of what the SDK reported, they
-- back existing applied actions, and rewriting them would be rewriting the
-- past. The finding's job is to stop trusting them, not to make them disappear.
--
-- scope_kind records whether the caller named a boundary — a customer or an
-- explicit cache scope — inside which a response could actually be reused. An
-- unscoped repeat is still a repeat and is still counted; what it is not is
-- evidence that the second call could have served the first, because nobody
-- said the two belong to the same user, tenant or cache. Absence is stored as
-- absence rather than being read as permission.
ALTER TABLE usage_signal ADD COLUMN fingerprint_version text NOT NULL DEFAULT 'v1'
    CHECK (fingerprint_version IN ('v1', 'v2'));
ALTER TABLE usage_signal ADD COLUMN scope_kind text
    CHECK (scope_kind IS NULL OR scope_kind IN ('explicit', 'unscoped'));

-- The unique key gains the version: a v1 and a v2 row for the same fingerprint
-- are different facts about different comparisons and must not merge into one.
--
-- 0019 declared it as a table-level UNIQUE(...), so Postgres named the
-- constraint itself and truncated that name to 63 characters. Looked up rather
-- than guessed: a DROP of a name that does not exist is a silent no-op, which
-- would leave the old constraint in place and still block the v1/v2 pair this
-- migration exists to allow.
DO $$
DECLARE
    name text;
BEGIN
    SELECT c.conname INTO name
    FROM pg_constraint c
    JOIN pg_class t ON t.oid = c.conrelid
    WHERE t.relname = 'usage_signal' AND c.contype = 'u'
      AND pg_get_constraintdef(c.oid) LIKE '%fingerprint)';
    IF name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE usage_signal DROP CONSTRAINT %I', name);
    END IF;
END $$;

CREATE UNIQUE INDEX usage_signal_key ON usage_signal
    (tenant_id, feature_id, provider, model, period, signal_kind, fingerprint,
     fingerprint_version);

COMMENT ON COLUMN usage_signal.fingerprint_version IS
    'Which canonical request identity produced this fingerprint. v1 = provider, '
    'model and messages only (incomplete; excluded from the duplicate finding). '
    'v2 = the whole request minus transport and credentials, scoped.';
COMMENT ON COLUMN usage_signal.scope_kind IS
    'explicit = the caller named a customer or cache scope the response could be '
    'reused in. unscoped = nobody did, so the repeat is counted but never '
    'presented as safe reuse. NULL on v1 rows, where the question was not asked.';
