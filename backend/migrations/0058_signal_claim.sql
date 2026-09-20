-- 0058: let a span's optimization signal be counted exactly once.
--
-- `_claim_cost` (0049) makes MONEY idempotent: `costed_at` is NULL until a span
-- is priced, the conditional UPDATE is atomic, and a re-delivered completion
-- finds the row already claimed and adds nothing. Signals had no such guard.
-- They are folded into `usage_signal` BEFORE that claim, so re-delivering one
-- span inflated the duplicate/prefix counts while the dollars stayed correct:
--
--     first delivery              -> 1 signal, $0.0045
--     replay, same batch_id       -> 1 signal, $0.0045   (hook_batch catches it)
--     replay, NEW batch_id        -> 2 signals, $0.0045  <-- inflated
--     replay, no batch_id at all  -> 3 signals, $0.0045  <-- inflated again
--
-- hook_batch (0038) only recognises a repeat of the *same* batch id. A client
-- that retries with a fresh id, or none, is outside it — and the duplicate-call
-- finding is built on exactly these counts, so a retry loop could grow a
-- customer's "savings" without a single new call being made.
--
-- WHY NOT REUSE costed_at. A prefix summary carries a signal and deliberately
-- carries no cost — it summarises calls that were each metered when they
-- happened. Gating signals on the financial claim would drop every prefix
-- summary on the floor. The two facts are claimed separately because they are
-- separately true.
ALTER TABLE ai_span ADD COLUMN signalled_at timestamptz;

-- Spans that predate this column were already counted once by the old path;
-- leaving them NULL would let a re-delivery of an old span claim and count it a
-- second time. Only completed spans can have carried a signal.
UPDATE ai_span SET signalled_at = COALESCE(ended_at, updated_at, created_at)
WHERE status <> 'running';

COMMENT ON COLUMN ai_span.signalled_at IS
    'When this span''s optimization signal was folded into usage_signal. NULL '
    'until claimed; the conditional UPDATE in traces._claim_signal is what makes '
    'a re-delivered span count once. Separate from costed_at because a prefix '
    'summary carries a signal and no cost.';
