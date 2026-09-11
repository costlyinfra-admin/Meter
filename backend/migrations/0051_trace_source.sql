-- 0051: where a trace came from.
--
-- Traces can now arrive two ways: Meter's SDK posting lifecycle events, or an
-- existing OpenTelemetry exporter posting OTLP spans that otel.py translates
-- into the same events. Both end in these tables, which is the point — one
-- pricing path, one attribution path, one set of guarantees.
--
-- But "both end in the same tables" is exactly why the origin has to be
-- written down. Install SDK asks "has your instrumentation reported yet?", and
-- an organization already running the SDK would see a green light the moment it
-- opened the OpenTelemetry guide, having wired up nothing. That is the kind of
-- number this product does not print.
--
-- Defaulted rather than backfilled by guesswork: every trace that exists before
-- this migration arrived through the SDK, because there was no other way in.

ALTER TABLE ai_trace ADD COLUMN source text NOT NULL DEFAULT 'sdk'
    CHECK (source IN ('sdk', 'otel'));

-- The Install SDK verification panel's query: this tenant's newest trace from
-- one particular origin. `updated_at`, not `started_at`, for the same reason
-- 0044 gave: a long run keeps reporting, and "latest report" means the last
-- time anything arrived, not when the run began.
CREATE INDEX ai_trace_source_recent_idx
    ON ai_trace (tenant_id, source, updated_at DESC);
