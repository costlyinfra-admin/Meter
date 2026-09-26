-- 0063: value a prefix at its AVERAGE measured size, not its largest.
--
-- prefix_tokens was folded together with GREATEST, and the result multiplied by
-- every uncached call in the group. Two things were wrong with that.
--
-- The smaller one: a provider's cache-creation count varies between calls that
-- share a static block, because what it actually caches depends on where the
-- breakpoint falls and how much conversation sits in front of it. Taking the
-- largest figure seen all month and applying it to every call values the whole
-- group at its most expensive member. The bias only ever points one way.
--
-- The larger one: GREATEST could not tell a measurement from an estimate. One
-- process that never saw a cache creation sends its own character count; another
-- sends the provider's real number. If the estimate is bigger it wins, and
-- prefix_measured -- which latches on -- then labels the row as measured. The
-- row ends up holding a figure the provider never produced while claiming the
-- provider produced it, which is the exact failure 0056 was written to prevent,
-- reintroduced one layer up in the aggregation.
--
-- So measurements are now summed and counted instead of maximised, and they are
-- kept apart from the estimate rather than competing with it for one column:
--
--   prefix_tokens         -- the SDK's character estimate. Constant within a
--                            fingerprint group, because the fingerprint IS the
--                            static block, so folding it with GREATEST is a
--                            no-op and stays one.
--   prefix_tokens_sum/_n  -- the provider's own counts, summed and counted, so
--                            the mean survives being aggregated across batches,
--                            processes and replicas. A max does not: max(max)
--                            is a max, but max(mean) is not a mean.
--
-- Rows written before this have no sum to divide, and their prefix_tokens may
-- be either kind of number with no way left to tell which. They are not
-- deleted -- they are real history and they back existing applied actions --
-- but the detector reads them as estimates, which is the weaker and safer of
-- the two readings. prefix_measured stays for exactly that: telling those rows
-- apart from new ones, not for deciding confidence any more.
ALTER TABLE usage_signal ADD COLUMN prefix_tokens_sum bigint NOT NULL DEFAULT 0;
ALTER TABLE usage_signal ADD COLUMN prefix_tokens_n   bigint NOT NULL DEFAULT 0;

COMMENT ON COLUMN usage_signal.prefix_tokens_sum IS
    'prefix kind only: sum of the provider''s own cache-creation token counts.';
COMMENT ON COLUMN usage_signal.prefix_tokens_n IS
    'prefix kind only: how many measurements prefix_tokens_sum is over. The '
    'detector uses sum/n as the prefix size; 0 means it has only the estimate.';
COMMENT ON COLUMN usage_signal.prefix_tokens IS
    'prefix kind only: the SDK''s character estimate of the prefix size. On '
    'rows written before 0063 this may instead hold a provider measurement, '
    'which is why those rows are read as estimates.';
