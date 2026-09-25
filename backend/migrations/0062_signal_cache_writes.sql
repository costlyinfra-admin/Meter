-- 0062: record what caching would COST, not only what it would save.
--
-- The prompt-caching lever priced `uncached calls x prefix tokens x rate x
-- (1 - cache read discount)` and stopped there, as if the only effect of
-- turning caching on were a discount. It is not. Writing a prefix into a
-- provider's cache costs MORE than sending it uncached — Anthropic bills a
-- 5-minute write at 1.25x the input rate, a 1-hour write at 2x — and only the
-- reads that follow pay that premium back. pricing.CACHE_WRITE_MULT has carried
-- those multipliers since 0033; nothing was multiplying by them.
--
-- Two counts are needed and neither could be derived from what was stored:
--
-- write_calls is how many of these calls the provider reported as cache
-- CREATIONS. It answers a question the detector had no way to ask: is this
-- prefix already cached? A creation returns cache_creation_input_tokens and no
-- cache_read, so every one of them was being counted as an uncached call and
-- offered back as an opportunity to enable caching. On steady traffic with a
-- 5-minute TTL that is thousands of calls a month, and because a creation is
-- also the only source of a provider-measured prefix size, they were the calls
-- that pushed the finding to HIGH confidence. The strongest version of this
-- recommendation was reachable only by customers who had already taken it.
--
-- cache_windows is how many times a cache would have had to be written if one
-- were in use: once per gap longer than the provider's TTL. Sparse traffic --
-- a call every few hours -- would write on every single call and never read,
-- so caching it costs 25% MORE than not caching it. That was being reported as
-- a saving. Only the SDK can count this: it needs the call timestamps, and by
-- the time a month is aggregated into a period bucket they are gone.
--
-- Both are NULL/0 for rows written before this. NULL cache_windows means the
-- SDK that sent the row could not count windows, which is not the same as
-- counting zero of them -- the detector treats those rows as a ceiling rather
-- than assuming writes are free.
ALTER TABLE usage_signal ADD COLUMN write_calls bigint NOT NULL DEFAULT 0;
ALTER TABLE usage_signal ADD COLUMN cache_windows bigint;

COMMENT ON COLUMN usage_signal.write_calls IS
    'prefix kind only: calls the provider reported as cache creations. These '
    'are already cached; they are not an opportunity to enable caching.';
COMMENT ON COLUMN usage_signal.cache_windows IS
    'prefix kind only: distinct provider-TTL windows this prefix was called in '
    '— the number of cache writes enabling caching would incur. NULL when the '
    'reporting SDK predates the count, which is not the same as zero.';
