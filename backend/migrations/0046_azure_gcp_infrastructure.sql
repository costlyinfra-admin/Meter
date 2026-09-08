-- 0046: the Azure and GCP infrastructure connectors.
--
-- Both read the whole cloud bill, the way the AWS connector added in 0045 does.
-- Only the credential CHECK needs widening — infra_cost is provider-generic by
-- design, and `provider` is deliberately free text there rather than an enum,
-- so a new cloud costs one line here and none in the cost table.
--
-- On the ids: "azure_cloud", not "azure". "azure" already belongs to the Azure
-- OpenAI connector, which reads a NARROW slice of the same Azure bill with a
-- different credential and a different role. They are two connections to one
-- provider, and collapsing them would make connecting one look like connecting
-- the other. Azure OpenAI line items seen by the infrastructure connector are
-- classified `inference` and recorded with counted = false, exactly as Bedrock
-- is — so the same dollar cannot arrive down both paths.
--
-- GCP is a service account rather than a key pair because BigQuery has no other
-- non-interactive auth. Google publishes no cost API: the Cloud Billing REST
-- API returns the price catalog, not spend, so the BigQuery billing export is
-- the only programmatic path to what a customer actually paid.

ALTER TABLE connector_credential DROP CONSTRAINT connector_credential_connector_type_check;
ALTER TABLE connector_credential ADD CONSTRAINT connector_credential_connector_type_check
    CHECK (connector_type IN ('github', 'anthropic', 'openai', 'google', 'bedrock', 'openrouter',
                              'together', 'fireworks', 'okta', 'entra', 'cursor', 'copilot',
                              'codex', 'azure', 'litellm', 'vercel', 'modal', 'elevenlabs',
                              'groq', 'mistral', 'xai', 'perplexity', 'cohere', 'replicate',
                              'portkey', 'helicone', 'aws', 'azure_cloud', 'gcp'));
