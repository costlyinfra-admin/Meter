-- 0047: DigitalOcean, MongoDB Atlas, Cloudflare and Snowflake.
--
-- Four managed platforms on the infrastructure tab. As with 0046, only the
-- credential CHECK needs widening: infra_cost is provider-generic and `provider`
-- is free text there, so a new source costs one line here and none in the cost
-- table itself.
--
-- The bar for being here is that the vendor reports DOLLARS. Each of these four
-- publishes a real billed figure — DigitalOcean and Atlas as invoice line items,
-- Cloudflare as subscription prices, Snowflake as
-- ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY. Platforms that publish only usage
-- (compute-hours, storage-GB, request counts) are deliberately absent: pricing
-- that usage ourselves would produce a modelled number presented as a bill,
-- which is the black-box behaviour this product exists to replace.
--
-- Two of these bill model inference that no other connector ingests —
-- Cloudflare Workers AI and Snowflake Cortex (SERVICE_TYPE = AI_SERVICES). Those
-- are classified `inference` and stay out of infrastructure totals, but unlike
-- Bedrock they are COUNTED, because nothing else is counting them.

ALTER TABLE connector_credential DROP CONSTRAINT connector_credential_connector_type_check;
ALTER TABLE connector_credential ADD CONSTRAINT connector_credential_connector_type_check
    CHECK (connector_type IN ('github', 'anthropic', 'openai', 'google', 'bedrock', 'openrouter',
                              'together', 'fireworks', 'okta', 'entra', 'cursor', 'copilot',
                              'codex', 'azure', 'litellm', 'vercel', 'modal', 'elevenlabs',
                              'groq', 'mistral', 'xai', 'perplexity', 'cohere', 'replicate',
                              'portkey', 'helicone', 'aws', 'azure_cloud', 'gcp',
                              'digitalocean', 'mongodb_atlas', 'cloudflare', 'snowflake'));
