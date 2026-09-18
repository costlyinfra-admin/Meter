-- 0056: say whether a prefix's token count was measured or estimated.
--
-- The prompt-caching lever prices `uncached calls x prefix_tokens x rate x
-- (1 - cache discount)` and presented the result as measured, at high
-- confidence. But `prefix_tokens` was the SDK's own `len(static) // 4` — a
-- character count standing in for a tokenizer it does not have. Pricing an
-- estimate and labelling it a measurement is exactly what invariant 3 forbids.
--
-- The SDK now sends the provider's own figure where the provider reports one
-- (Anthropic's `cache_creation_input_tokens` is the size of the prefix it
-- actually cached) and says which of the two it sent. Rows written before this
-- carry the estimate, so `false` is the correct default for them.
ALTER TABLE usage_signal ADD COLUMN prefix_measured boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN usage_signal.prefix_measured IS
    'prefix kind only: true when prefix_tokens came from the provider''s own '
    'cache-creation token count, false when it is the SDK''s character estimate.';
