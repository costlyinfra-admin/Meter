-- 0035: enough PR detail to report developer activity over a period.
--
-- feature_signal already records who authored a PR (actor) and how big it was in
-- commits/files. Two things were missing for the Overview's "By Developer" tab:
--
--   (a) line counts — GitHub returns `additions`/`deletions` from the same PR
--       detail endpoint the connector ALREADY calls for commits/changed_files,
--       so this costs no extra API request;
--   (b) merged_at — the PR's own merge date. `created_at` is when Meter
--       inserted the row, which is the date of the last discovery run, not of
--       the work. Without the real date, activity cannot honestly be scoped to
--       the review period the user selected.
--
-- All nullable: PRs discovered before this migration keep NULLs, and the read
-- side reports them as unknown rather than as zero.

ALTER TABLE feature_signal ADD COLUMN additions integer;
ALTER TABLE feature_signal ADD COLUMN deletions integer;
ALTER TABLE feature_signal ADD COLUMN merged_at date;

-- Activity is read per-tenant over a date window, grouped by author.
CREATE INDEX feature_signal_merged_idx
    ON feature_signal (tenant_id, merged_at) WHERE signal_type = 'pr';
