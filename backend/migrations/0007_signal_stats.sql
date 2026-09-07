-- Meter — per-PR commit and file-change counts on the evidence trail.
--
-- The GitHub PR *list* endpoint doesn't return commit or changed-file counts;
-- they come from the single-PR detail endpoint. We fetch them (best-effort) during
-- discovery and store them on the 'pr' feature_signal rows, so the feature
-- drill-down can show commits + files changed per developer. NULL when unknown.

ALTER TABLE feature_signal ADD COLUMN commits integer;
ALTER TABLE feature_signal ADD COLUMN files_changed integer;
