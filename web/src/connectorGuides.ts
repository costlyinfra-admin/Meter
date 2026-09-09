/**
 * Per-connector setup instructions, shown inside a connector's Connect panel.
 * Each guide tells the user exactly where to get the credential this connector
 * needs and what we do with it (always read-only). Keyed by connector `type`;
 * connectors without an entry fall back to the generic paste-token form.
 *
 * The credential each one expects mirrors what the backend client actually uses
 * (see backend/meter/providers.py) — admin/cost keys, not standard keys.
 */
export type ConnectorGuide = {
  /** One line on what we read and the read-only assurance. */
  blurb: string;
  /** Ordered, plain-language steps to obtain the credential. */
  steps: string[];
  /** Placeholder for the input (shows the expected shape, e.g. a key prefix). */
  placeholder: string;
  /** Render a textarea instead of a single-line input (for JSON blobs). */
  multiline?: boolean;
  /** Optional deep link to the provider's credential page. */
  docUrl?: string;
};

export const CONNECTOR_GUIDES: Record<string, ConnectorGuide> = {
  github: {
    blurb: "Read-only. We read merged pull requests to discover features.",
    steps: [
      "Sign in to GitHub and open Settings → Developer settings → Personal access tokens.",
      "Generate a token with read-only repo access (fine-grained: Contents + Pull requests, Read-only).",
      "A token is optional for public organizations and required for private repos.",
      "Paste the token below.",
    ],
    placeholder: "ghp_… (optional for public orgs)",
    docUrl: "https://github.com/settings/tokens",
  },
  anthropic: {
    blurb: "Read-only. We call the organization Cost & Usage report only.",
    steps: [
      "Sign in to the Anthropic Console (console.anthropic.com) as an organization admin.",
      "Open Settings → Admin keys and create an Admin API key (starts with sk-ant-admin…).",
      "An Admin key is required — standard API keys cannot read organization cost.",
      "Paste the key below.",
    ],
    placeholder: "sk-ant-admin-…",
    docUrl: "https://console.anthropic.com/settings/admin-keys",
  },
  openai: {
    blurb: "Read-only. We call the organization Costs API only.",
    steps: [
      "Sign in to the OpenAI platform (platform.openai.com) as an organization owner.",
      "Open Settings → Organization → Admin keys and create an Admin key (starts with sk-admin-…).",
      "The Costs API requires an Admin key, not a standard secret key.",
      "Paste the key below.",
    ],
    placeholder: "sk-admin-…",
    docUrl: "https://platform.openai.com/settings/organization/admin-keys",
  },
  google: {
    blurb: "Read-only. Gemini spend lives in Google Cloud Billing, grouped by project.",
    steps: [
      "Grant a service account (or your user) the “Billing Account Viewer” role.",
      "Generate an OAuth access token with the cloud-billing.readonly scope — e.g. run `gcloud auth print-access-token`.",
      "Tokens are short-lived; use a service account for ongoing sync.",
      "Paste the access token below.",
    ],
    placeholder: "OAuth access token (ya29.…)",
    docUrl: "https://cloud.google.com/billing/docs/how-to/get-cost-data",
  },
  openrouter: {
    blurb: "Read-only. We read your monthly activity and usage.",
    steps: [
      "Sign in at openrouter.ai and open Keys.",
      "Create an API key (starts with sk-or-…).",
      "Paste the key below.",
    ],
    placeholder: "sk-or-…",
    docUrl: "https://openrouter.ai/keys",
  },
  together: {
    blurb: "Read-only. We read your monthly usage.",
    steps: [
      "Sign in to the Together dashboard (api.together.ai) and open Settings → API Keys.",
      "Copy your API key.",
      "Paste the key below.",
    ],
    placeholder: "Together API key",
    docUrl: "https://api.together.ai/settings/api-keys",
  },
  fireworks: {
    blurb: "Read-only. We read your monthly usage.",
    steps: [
      "Sign in at fireworks.ai and open Account → API Keys.",
      "Create or copy an API key.",
      "Paste the key below.",
    ],
    placeholder: "Fireworks API key",
    docUrl: "https://fireworks.ai/account/api-keys",
  },
  bedrock: {
    blurb:
      "Read-only. We read AWS Cost Explorer, filter to Amazon Bedrock, and split spend by a cost-allocation tag.",
    steps: [
      "Create an IAM user or role with the ce:GetCostAndUsage permission and generate an access key.",
      "Tag your Bedrock usage with a cost-allocation tag (e.g. “feature”) per feature, and activate it under Billing → Cost allocation tags.",
      "Paste the credentials below as JSON (this connector takes JSON, not a plain token).",
    ],
    placeholder:
      '{"access_key_id":"AKIA…","secret_access_key":"…","region":"us-east-1","tag":"feature"}',
    multiline: true,
    docUrl: "https://docs.aws.amazon.com/cost-management/latest/userguide/ce-api.html",
  },
  aws: {
    blurb:
      "Read-only. We read your whole AWS bill through Cost Explorer and split it " +
      "into infrastructure, model serving and build cost — one category per line item.",
    steps: [
      "Create an IAM user or role whose only permission is ce:GetCostAndUsage, and generate an access key. That permission reads billing totals and nothing else — it cannot see your data or your resources.",
      "In Billing → Cost allocation tags, activate the tag you use to mark which feature a resource belongs to (e.g. “feature”). AWS only reports activated tags, and only from the day you activate them onward.",
      "Tag the resources you want attributed. Untagged spend is not guessed at — it lands in Unattributed.",
      "Paste the credentials below as JSON. `tag` names the tag used for feature attribution; `metric` (default UnblendedCost) and `granularity` (DAILY or MONTHLY) are optional.",
      "AWS billing data lags by up to 24–48 hours and is restated for a few days after, so today's figure will move. Each sync re-reads recent history rather than adding to it.",
      "Amazon Bedrock is already imported by the “Amazon Bedrock (AWS cost)” connector on the Inference tab, which stays its source of truth. Bedrock line items seen here are recorded but never added to infrastructure totals, so nothing is counted twice.",
    ],
    placeholder:
      '{"access_key_id":"AKIA…","secret_access_key":"…","region":"us-east-1","tag":"feature"}',
    multiline: true,
    docUrl: "https://docs.aws.amazon.com/cost-management/latest/userguide/ce-api.html",
  },
  vercel_cloud: {
    blurb:
      "Read-only. We read Vercel's billing charges in FOCUS — the open cost standard — " +
      "so every charge arrives with its service, its tags and a billed amount.",
    steps: [
      "In Vercel, open Account Settings → Tokens and create a token scoped to the team whose bill you want to read.",
      "Copy the team ID from Team Settings → General, and paste both below as JSON.",
      "The billing charges endpoint is available to Pro and Enterprise teams. On a Hobby account the token is valid but the endpoint refuses it, so you will see a permission error rather than an empty bill.",
      "Vercel reports FOCUS tags, so spend attributes to features by a real cost-allocation tag — set `tag` to the one you use (default: feature). Untagged charges land in Unattributed rather than being guessed at.",
      "Charges are daily and available for up to a year, so a first sync backfills real history — unlike a subscription-based source.",
      "Vercel bills build execution by the minute. Those charges are counted as build cost, not infrastructure — it is the one bill here that contains both sides of the model.",
      "If you also use the Vercel AI Gateway, its spend stays with that connector on the Inference tab. Gateway charges seen here are recorded but never added to infrastructure totals, so nothing is counted twice.",
    ],
    placeholder: '{"token":"…","team_id":"team_…","tag":"feature"}',
    multiline: true,
    docUrl: "https://vercel.com/docs/rest-api/reference/endpoints/billing",
  },
  digitalocean: {
    blurb:
      "Read-only. We read your DigitalOcean invoices — the real line items, product by product.",
    steps: [
      "In the DigitalOcean control panel, open API → Tokens and generate a personal access token with READ scope only. Read scope cannot create, resize or destroy anything.",
      "Paste it below as JSON.",
      "DigitalOcean has no arbitrary cost-allocation tags, so spend is attributed to features by PROJECT — the grouping you already organise resources into. Map a project name to a feature and its spend follows; anything unmapped lands in Unattributed.",
      "Invoices are issued monthly, so a sync reads whole invoices rather than daily usage. The current month appears once DigitalOcean issues it.",
    ],
    placeholder: '{"token":"dop_v1_…"}',
    multiline: true,
    docUrl: "https://cloud.digitalocean.com/account/api/tokens",
  },
  mongodb_atlas: {
    blurb:
      "Read-only. We read your Atlas organisation invoices — per-cluster line items, priced by Atlas.",
    steps: [
      "In Atlas, open Organization Access Manager → Applications → API Keys and create a programmatic API key.",
      "Give it only the Organization Billing Viewer role. That role can read invoices and nothing else — not your data, not your clusters' contents.",
      "If your organisation uses an API access list, add Meter's egress IP, or Atlas will reject the key.",
      "Copy the organization ID from the URL or Organization Settings, and paste everything below as JSON.",
      "Spend is attributed to features by Atlas PROJECT, which is how Atlas separates workloads. Unmapped projects land in Unattributed.",
    ],
    placeholder: '{"public_key":"…","private_key":"…","org_id":"…"}',
    multiline: true,
    docUrl: "https://www.mongodb.com/docs/atlas/configure-api-access/",
  },
  cloudflare: {
    blurb:
      "Read-only. Cloudflare does not publish per-resource cost, so we read what you subscribe to and what each subscription costs.",
    steps: [
      "In the Cloudflare dashboard, open My Profile → API Tokens → Create Token, and use a custom token with just one permission: Account → Billing → Read. It cannot read traffic, logs, or zone content.",
      "Copy your account ID from any zone's Overview page, and paste both below as JSON.",
      "Important: a Cloudflare subscription describes its CURRENT billing period. There is no historical series to backfill, so a first sync records this period only and history builds up from the nightly sync onward.",
      "Spend is attributed by zone, or by product for account-wide subscriptions like Workers. Map a zone to a feature and its plan cost follows.",
      "Workers AI is model inference rather than infrastructure. It is recorded as inference and kept out of infrastructure totals — but it is still counted, because no other connector reads it.",
    ],
    placeholder: '{"api_token":"…","account_id":"…"}',
    multiline: true,
    docUrl: "https://dash.cloudflare.com/profile/api-tokens",
  },
  snowflake: {
    blurb:
      "Read-only. Snowflake meters in credits, and credits are not dollars — we read the one view that reports actual currency.",
    steps: [
      "Create a Snowflake user for Meter and generate an RSA key pair, then register the public key on that user (ALTER USER … SET RSA_PUBLIC_KEY = '…'). Key-pair auth means no password is stored anywhere.",
      "Grant that user the ORGADMIN role. Snowflake only exposes spend in currency through SNOWFLAKE.ORGANIZATION_USAGE.USAGE_IN_CURRENCY_DAILY, and that view is ORGADMIN-only — that is Snowflake's design, not ours.",
      "Find your account identifier (ORGNAME-ACCOUNTNAME) under Admin → Accounts.",
      "Paste the account, the user, and the unencrypted private key PEM below as JSON. Optionally add a warehouse to run the query on.",
      "We read spend in currency rather than credits on purpose: a credit's dollar value depends on your edition, region and contract, so pricing credits ourselves would produce an estimate rather than your bill.",
      "Cortex (SERVICE_TYPE = AI_SERVICES) is model inference. It is recorded as inference and kept out of infrastructure totals, while warehouse compute and storage are infrastructure.",
    ],
    placeholder:
      '{"account":"ORGNAME-ACCOUNTNAME","user":"METER_SVC","private_key":"-----BEGIN PRIVATE KEY-----\n…"}',
    multiline: true,
    docUrl: "https://docs.snowflake.com/en/developer-guide/sql-api/authenticating",
  },
  azure_cloud: {
    blurb:
      "Read-only. We read your whole Azure subscription through Cost Management and " +
      "split it into infrastructure, model serving and build cost — one category per line item.",
    steps: [
      "In Microsoft Entra ID, register an application and create a client secret. This is the service principal we authenticate as.",
      "In the subscription's Access control (IAM), assign it the Cost Management Reader role. That role reads billing aggregates only — it cannot see your data, your resources, or anything outside cost.",
      "Under Cost Management → Configuration, make sure the tag you use to mark which feature a resource belongs to (e.g. “feature”) is applied to your resources. Azure reports tags on the resources that carry them; untagged spend is not guessed at and lands in Unattributed.",
      "Paste the credentials below as JSON. `tag` names the tag used for feature attribution; `metric` (ActualCost or AmortizedCost) and `granularity` (Daily or Monthly) are optional.",
      "Azure cost data lags by up to 24–48 hours and is restated for a few days after, so today's figure will move. Each sync re-reads recent history rather than adding to it.",
      "Azure OpenAI is already imported by the “Azure OpenAI (Azure cost)” connector on the Inference tab, which stays its source of truth. Azure OpenAI line items seen here are recorded but never added to infrastructure totals, so nothing is counted twice.",
    ],
    placeholder:
      '{"tenant_id":"…","client_id":"…","client_secret":"…","subscription_id":"…","tag":"feature"}',
    multiline: true,
    docUrl:
      "https://learn.microsoft.com/en-us/azure/cost-management-billing/costs/understand-work-scopes",
  },
  gcp: {
    blurb:
      "Read-only. Google publishes no cost API, so we read the BigQuery billing export — " +
      "the only place GCP reports what you actually spent.",
    steps: [
      "In the Cloud console, go to Billing → Billing export and enable Detailed usage cost export to BigQuery. Note the dataset, and the table name it creates (gcp_billing_export_v1_<BILLING_ACCOUNT_ID>).",
      "Important: the export is not backfilled. There is no data for any period before you switch it on, so historical months will be empty until it has been running.",
      "Create a service account and grant it exactly two roles: BigQuery Job User on the project, and BigQuery Data Viewer on the billing export dataset. Neither can write, and neither can read anything outside that dataset.",
      "Create a JSON key for that service account and download it.",
      "Paste the key file below as JSON, and add `dataset`, `table`, and `tag` (the resource label naming which feature a resource belongs to). Untagged spend is not guessed at — it lands in Unattributed.",
      "GCP billing data lags by several hours and is restated for a few days after, so recent figures will move. Each sync re-reads recent history rather than adding to it.",
      "Vertex AI is model inference, not infrastructure. Those line items are recorded but never added to infrastructure totals, so they can never be counted twice against a model-provider connector.",
    ],
    placeholder:
      '{"type":"service_account","client_email":"…","private_key":"…","project_id":"…",' +
      '"dataset":"billing_export","table":"gcp_billing_export_v1_…","tag":"feature"}',
    multiline: true,
    docUrl: "https://cloud.google.com/billing/docs/how-to/export-data-bigquery-setup",
  },
  azure: {
    blurb:
      "Read-only. Azure OpenAI spend lives in Azure Cost Management; we read it, filter to Cognitive Services, and split by a cost-allocation tag.",
    steps: [
      "In Microsoft Entra ID, register an app (service principal) and create a client secret.",
      "Grant it the “Cost Management Reader” role on the subscription.",
      "Tag your Azure OpenAI resources with a cost-allocation tag (e.g. “feature”) per feature.",
      "Paste the credentials below as JSON (this connector takes JSON, not a plain token).",
    ],
    placeholder:
      '{"tenant_id":"…","client_id":"…","client_secret":"…","subscription_id":"…","tag":"feature"}',
    multiline: true,
    docUrl:
      "https://learn.microsoft.com/azure/cost-management-billing/automate/automation-ingest-usage-details-overview",
  },
  litellm: {
    blurb:
      "Read-only. Your LiteLLM proxy already tracks per-key, per-model dollar spend; we read its spend report.",
    steps: [
      "Use your self-hosted LiteLLM proxy URL (e.g. https://litellm.acme.com).",
      "Use the LITELLM_MASTER_KEY you configured (starts with sk-) — it authorizes the admin spend report.",
      "Paste both below as JSON.",
    ],
    placeholder: '{"base_url":"https://litellm.acme.com","master_key":"sk-…"}',
    multiline: true,
    docUrl: "https://docs.litellm.ai/docs/proxy/cost_tracking",
  },
  vercel: {
    blurb: "Read-only. We read the AI Gateway Custom Reporting API for cost by model and project.",
    steps: [
      "In Vercel → Account Settings → Tokens, create an access token.",
      "Optionally include your team id to scope the report to a team.",
      'Paste below as JSON. (The reporting API is in beta — if your endpoint differs, add a "url" field to override.)',
    ],
    placeholder: '{"token":"…","team_id":"team_… (optional)"}',
    multiline: true,
    docUrl: "https://vercel.com/docs/ai-gateway/capabilities/observability",
  },
  modal: {
    blurb:
      "Read-only. Modal bills GPU/CPU compute time per app; we read your workspace's billing usage and attribute by app.",
    steps: [
      "In the Modal dashboard → Settings → API Tokens, create a token (id + secret).",
      "Programmatic billing export needs a Team or Enterprise workspace.",
      'Paste below as JSON. (Add a "url" field to override the billing endpoint if needed.)',
    ],
    placeholder: '{"token_id":"ak-…","token_secret":"as-…"}',
    multiline: true,
    docUrl: "https://modal.com/docs/guide/billing",
  },
  elevenlabs: {
    blurb:
      "Read-only. ElevenLabs bills by characters/credits; we read your monthly character usage and price it at a transparent rate.",
    steps: [
      "In the ElevenLabs dashboard, open your profile → API Keys.",
      "Create or copy an API key.",
      "Paste the key below. (Cost is estimated from character usage, since ElevenLabs has no dollar-cost API.)",
    ],
    placeholder: "ElevenLabs API key (xi-…)",
    docUrl: "https://elevenlabs.io/docs/api-reference/usage/get",
  },
  groq: {
    blurb: "Read-only. We read your monthly usage; cost is priced from tokens via our price book.",
    steps: [
      "Sign in at console.groq.com and open API Keys.",
      "Create or copy an API key (starts with gsk_…).",
      "Paste the key below. (For exact per-feature cost, the metering SDK is the precise path.)",
    ],
    placeholder: "gsk_…",
    docUrl: "https://console.groq.com/keys",
  },
  mistral: {
    blurb: "Read-only. We read your monthly usage; cost is priced from tokens via our price book.",
    steps: [
      "Sign in to the Mistral console (console.mistral.ai) and open API Keys.",
      "Create or copy an API key.",
      "Paste the key below.",
    ],
    placeholder: "Mistral API key",
    docUrl: "https://console.mistral.ai/api-keys",
  },
  xai: {
    blurb: "Read-only. We read your monthly usage; cost is priced from tokens via our price book.",
    steps: [
      "Sign in to the xAI console (console.x.ai) and open API Keys.",
      "Create or copy an API key (starts with xai-…).",
      "Paste the key below.",
    ],
    placeholder: "xai-…",
    docUrl: "https://console.x.ai",
  },
  perplexity: {
    blurb: "Read-only. We read your monthly usage; cost is priced from tokens via our price book.",
    steps: [
      "Sign in at perplexity.ai → Settings → API and generate a key.",
      "Create or copy an API key (starts with pplx-…).",
      "Paste the key below.",
    ],
    placeholder: "pplx-…",
    docUrl: "https://www.perplexity.ai/settings/api",
  },
  cohere: {
    blurb: "Read-only. We read your monthly usage; cost is priced from tokens via our price book.",
    steps: [
      "Sign in to the Cohere dashboard (dashboard.cohere.com) and open API Keys.",
      "Copy a production API key.",
      "Paste the key below.",
    ],
    placeholder: "Cohere API key",
    docUrl: "https://dashboard.cohere.com/api-keys",
  },
  replicate: {
    blurb: "Read-only. Replicate bills by usage; we read your account's reported spend.",
    steps: [
      "Sign in at replicate.com → Account → API tokens.",
      "Copy your API token (starts with r8_…).",
      "Paste the token below.",
    ],
    placeholder: "r8_…",
    docUrl: "https://replicate.com/account/api-tokens",
  },
  portkey: {
    blurb:
      "Read-only. Portkey's analytics API reports per-model dollar cost across all providers you route through it.",
    steps: [
      "In the Portkey dashboard, open API Keys and copy your key.",
      'Paste it below as JSON. (If your analytics endpoint differs, add a "url" field to override.)',
    ],
    placeholder: '{"api_key":"…"}',
    multiline: true,
    docUrl: "https://portkey.ai/docs/api-reference/analytics",
  },
  helicone: {
    blurb:
      "Read-only. Helicone tracks per-request cost across providers; we read its cost query API.",
    steps: [
      "In Helicone → Settings → API Keys, create a key (starts with sk-helicone-…).",
      'Paste it below as JSON. (Add a "url" field to override the endpoint if needed.)',
    ],
    placeholder: '{"api_key":"sk-helicone-…"}',
    multiline: true,
    docUrl: "https://docs.helicone.ai/rest/overview",
  },
};
