-- Meter M5 — record the actor (developer) behind a signal.
--
-- Build-cost allocation splits a developer's coding-tool spend across features by
-- the PRs they authored (design §7.1). To do that we need to know who authored
-- each PR behind a feature, so feature_signal gains an `actor` column. For 'pr'
-- signals it holds the PR author (e.g. a GitHub login); NULL for other signals.

ALTER TABLE feature_signal ADD COLUMN actor text;

CREATE INDEX feature_signal_actor_idx ON feature_signal (actor);
