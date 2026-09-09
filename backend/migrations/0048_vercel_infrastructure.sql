-- 0048: the Vercel infrastructure connector.
--
-- Vercel's /v1/billing/charges returns charges in FOCUS 1.3 — the FinOps Open
-- Cost and Usage Specification — at daily granularity for up to a year. That is
-- the first source on this tab with real cost-allocation TAGS: FOCUS carries a
-- Tags map on every row, so Vercel spend attributes to features the same way an
-- AWS or Azure bill does, rather than falling back to a project name.
--
-- On the id: "vercel_cloud", not "vercel". "vercel" is the Vercel AI Gateway
-- connector on the Inference tab, which reads model spend from the same account
-- with a different scope. Charges this connector sees whose FOCUS ServiceName is
-- the AI Gateway are classified `inference` and recorded with counted = false
-- against that connector — the same treatment Bedrock gets, and for the same
-- reason: one dollar, one owner.
--
-- Vercel bills build execution by the minute, and those rows classify as `build`
-- rather than infrastructure. It is the one platform here whose invoice contains
-- both sides of this product's model.

ALTER TABLE connector_credential DROP CONSTRAINT connector_credential_connector_type_check;
ALTER TABLE connector_credential ADD CONSTRAINT connector_credential_connector_type_check
    CHECK (connector_type IN ('github', 'anthropic', 'openai', 'google', 'bedrock', 'openrouter',
                              'together', 'fireworks', 'okta', 'entra', 'cursor', 'copilot',
                              'codex', 'azure', 'litellm', 'vercel', 'modal', 'elevenlabs',
                              'groq', 'mistral', 'xai', 'perplexity', 'cohere', 'replicate',
                              'portkey', 'helicone', 'aws', 'azure_cloud', 'gcp',
                              'digitalocean', 'mongodb_atlas', 'cloudflare', 'snowflake',
                              'vercel_cloud'));
