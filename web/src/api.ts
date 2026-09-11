/**
 * Thin API client for the Meter backend.
 *
 * All calls send the session cookie (`credentials: "include"`). In dev, Vite
 * proxies `/api` to the FastAPI backend (see vite.config.ts).
 */

export interface User {
  id: string;
  tenant_id: string;
  email: string;
  org_name?: string;
  is_admin?: boolean;
  impersonating?: { tenant_id: string; company: string } | null;
}

// Organization-level settings (shared by every user in the tenant).
export interface OrgSettings {
  org_name: string;
  timezone: string;
  currency: string;
  customer_id_storage: "names" | "aliases" | "hashed";
  store_prompts: boolean;
  data_retention: "30d" | "90d" | "1y" | "indefinite";
  /** How long request-level traces are kept. Financial aggregates outlive them. */
  trace_retention_days: 7 | 30 | 90;
  /** How long a running agent may be quiet before the UI calls it stale. */
  agent_stale_after_minutes: number;
  /** Always "disabled" today. The reserved values exist so a future consented
   *  capture feature needs no migration; nothing may select them yet. */
  content_capture: "disabled" | "redacted" | "full";
}

/** The organization's AI budget. Null everywhere until someone sets one — there
 *  is no default budget, and none is invented on read. */
export interface Budget {
  amount: number;
  cadence: "monthly" | "annual";
  currency: string;
  /** ISO date. Days before it are not budgeted. */
  effective_from: string;
  updated_at: string | null;
  updated_by: string | null;
}

export interface BudgetInput {
  amount: number;
  cadence: "monthly" | "annual";
  effective_from: string;
  currency?: string;
}

/** How a budget was prorated onto the window being shown, so the UI can explain
 *  a figure that is rarely the round number the customer typed in. */
export interface BudgetProration {
  amount: number;
  method: "monthly" | "annual";
  covered_days: number;
  window_days: number;
  covered_start: string | null;
  covered_end: string | null;
  fully_covered: boolean;
}

/**
 * Where the reporting window lands against the budget. Computed entirely on the
 * server: read `status` first.
 *
 *   `closed`       the window is over. `forecast` is the final spend.
 *   `open`         the window includes the current month and could be projected.
 *   `insufficient` open, but with no observed daily spend to project from.
 */
export interface BudgetForecast {
  status: "open" | "closed" | "insufficient";
  /** The date the server treated as today, in the org's timezone. */
  as_of: string;
  /** True only for a demo tenant with a pinned as-of date. */
  as_of_is_fixed: boolean;
  window_start: string;
  window_end: string;
  actual: number;
  actual_build: number;
  actual_inference: number;
  /** Null when the organization has no budget. */
  budget: number | null;
  budget_detail: BudgetProration | null;
  budget_cadence: "monthly" | "annual" | null;
  currency: string | null;
  /** Null when there is nothing to project from. */
  forecast: number | null;
  forecast_optimized: number | null;
  identified_savings: number | null;
  variance: number | null;
  variance_pct: number | null;
  method: "closed" | "recent_weighted" | "month_to_date_average" | "none";
  confidence: "final" | "high" | "medium" | "low" | "none";
  observed_days: number;
}

/** BYOK: the tenant's own LLM for feature discovery. The key is write-only —
 *  no field here ever carries it back. */
export interface DiscoveryLlm {
  configured: boolean;
  enabled: boolean;
  /** The whole truth about the stored secret: never a prefix, suffix or length. */
  has_key: boolean;
  provider?: string;
  base_url?: string;
  model?: string;
  updated_at?: string | null;
  updated_by?: string | null;
}

export interface DiscoveryLlmProviders {
  providers: { value: string; base_url: string }[];
  default_model: string;
}

// ---- Internal admin portal (allow-listed admins only) ----
export interface AdminOverview {
  total_customers: number;
  connected_customers: number;
  pending_connections: number;
  total_ai_spend: number;
  total_opportunities: number;
  total_verified_savings: number;
}

export interface AdminCustomer {
  tenant_id: string;
  company: string;
  created_at: string | null;
  status: "connected" | "pending";
  connected_providers: string[];
  last_sync: string | null;
  monthly_spend: number;
  opportunities: number;
  verified_savings: number;
}

export interface AdminSyncRow {
  tenant_id: string;
  company: string;
  connector_type: string;
  action: string;
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  records_imported: number | null;
  status: string;
  error_message: string | null;
}

export interface AdminCustomerDetail {
  tenant_id: string;
  company: string;
  created_at: string | null;
  users: string[];
  connectors: ConnectorStatus[];
  repositories: string[];
  optimization_runs: {
    lever: string;
    applied_on: string;
    projected_monthly: number;
    created_at: string;
  }[];
  recent_syncs: AdminSyncRow[];
  recent_errors: AdminSyncRow[];
}

export interface ConnectorActionResult {
  status: string;
  records_imported: number | null;
  error_message: string | null;
  started_at: string;
  finished_at: string;
}

/** One infrastructure (cloud) provider on the Cost sources tab. */
export interface InfraProvider {
  type: string;
  name: string;
  short: string;
  /** Every listed cloud is connectable. Narrowed to one value on purpose: if a
   *  future provider is ever listed but not connectable, widening this union is
   *  what forces the UI to grow a way to show that, rather than silently
   *  offering a Connect button that cannot work. */
  status: "available";
  /** How this provider's numbers arrive: read from its API, or a file you upload. */
  ingest: "api" | "csv";
  note: string;
  connected: boolean;
  last_sync: InfraSyncRun | null;
  /** Non-secret settings only. Access keys are never sent to the browser. */
  config: InfraConfig | null;
}

export interface InfraSyncRun {
  status: "success" | "error";
  started_at: string | null;
  finished_at: string | null;
  items: number;
  amount: number;
  error_message: string | null;
}

export interface InfraConfig {
  /** The cost-allocation tag/label that drives feature attribution. */
  tag: string;
  /** What the dollars measure, in the provider's own vocabulary. */
  metric: string;
  granularity: string;
  /** What the numbers cover: an AWS region, an Azure subscription, a GCP dataset. */
  scope: string;
  /** What `scope` is called for this provider ("region", "subscription id", …). */
  scope_label: string;
  group_by: string[];
}

/** What a CSV import found. A preview returns this without writing anything. */
export interface InfraImportReport {
  provider: string;
  dry_run: boolean;
  rows_read: number;
  rows_imported: number;
  rows_skipped: number;
  /** meaning -> the column header it was taken from. */
  mapping: Record<string, string>;
  unmapped_columns: string[];
  warnings: string[];
  total: number;
  currency: string;
  from: string | null;
  to: string | null;
  /** Present only after a real import. */
  items?: number;
  infrastructure?: number;
  attributed?: number;
  unattributed?: number;
}

export interface InfraSummary {
  provider: string;
  month: string;
  total: number;
  attributed: number;
  unattributed: number;
  /** Recorded but owned by another connector (Bedrock). Never in `total`. */
  excluded: number;
  rows: number;
  by_category: { category: string; amount: number }[];
  services: { service: string; amount: number; attributed: number }[];
}

/** One step inside an agent run. Never carries prompt or response content —
 *  there is no column for it, by design. */
export interface AiSpan {
  external_span_id: string;
  parent_span_id: string | null;
  /** True when the parent event never arrived; the UI renders it as a root. */
  parent_missing: boolean;
  span_kind: "workflow" | "llm" | "embedding" | "retrieval" | "tool" | "guardrail" | "evaluation";
  operation_name: string;
  provider: string | null;
  model: string | null;
  tokens_in: number | null;
  tokens_out: number | null;
  cache_read_tokens: number | null;
  cache_write_tokens: number | null;
  reasoning_tokens: number | null;
  amount: number;
  latency_ms: number | null;
  status: "running" | "success" | "error" | "cancelled";
  prompt_id: string | null;
  prompt_version: string | null;
  started_at: string;
  ended_at: string | null;
}

export interface AiTrace {
  id: string;
  /** The id the customer's own SDK generated. */
  trace_id: string;
  operation_name: string;
  /** What is stored. `stale` is never one of these. */
  status: "running" | "success" | "error" | "cancelled";
  /** What to show: `running` plus a derived `stale`, computed per request
   *  against the tenant's threshold. A stale run has not failed and may finish. */
  live_status: "running" | "stale" | "success" | "error" | "cancelled";
  started_at: string;
  ended_at: string | null;
  duration_ms: number | null;
  last_activity_at: string;
  last_heartbeat_at: string | null;
  current_span_id: string | null;
  total_cost: number;
  total_tokens: number;
  span_count: number;
  llm_calls: number;
  environment: string;
  release_version: string | null;
  customer_ref: string | null;
  application: { id: string; name: string; slug: string };
  feature: { id: string; name: string } | null;
  spans?: AiSpan[];
}

export interface AiTracePage {
  traces: AiTrace[];
  total: number;
  limit: number;
  offset: number;
  stale_after_minutes: number;
}

export interface AiApplication {
  id: string;
  name: string;
  slug: string;
  description: string | null;
  owner: string | null;
  runs: number;
  spend: number;
  tokens: number;
  features: number;
  active: number;
  stale: number;
  /** null when the window had no runs — "free" and "nothing happened" differ. */
  cost_per_run: number | null;
  error_rate: number | null;
  prior_spend: number;
  prior_runs: number;
  spend_change: number | null;
  by_feature?: { feature: string; spend: number; runs: number }[];
  by_model?: { provider: string; model: string; spend: number; calls: number }[];
  releases?: { release: string; runs: number; spend: number }[];
}

/** One stored account under a connector. Identity and dates only — the secret
 *  is never returned by any route. */
/** One hand-entered build-cost line. */
export interface ManualBuildEntry {
  id: string;
  developer: string;
  handle: string | null;
  tool: string;
  amount: number;
  created_at: string | null;
}

export interface ConnectorCredential {
  id: string;
  label: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface ConnectorStatus {
  type: string;
  name: string;
  category: string;
  connected: boolean;
  /** When the stored credential was last written. The credential itself is
   *  never returned by any route — this is all the server says about it. */
  credential_set_at?: string | null;
  /** How many accounts are connected under this one source. */
  credential_count?: number;
  /** Whether a second credential would actually be read by this connector's
   *  sync. False means saving one replaces what is stored. */
  supports_multiple?: boolean;
}

export interface FeatureSignal {
  id: string;
  signal_type: string;
  external_ref: string;
  confidence: string | null;
  title?: string | null; // PR title (source evidence for the review UI)
  branch?: string | null; // PR head branch
  url?: string | null; // GitHub PR URL
}

export interface Feature {
  id: string;
  name: string;
  description: string;
  status: string;
  discovery_confidence: string | null;
  /** Product surface (chat/api/ui/...), or null when nobody has tagged it. */
  category: string | null;
  /** 'user' | 'discovery' — the basis for `category`. */
  category_source: string | null;
  signals: FeatureSignal[];
}

export interface DiscoverySummary {
  owner: string;
  prs: number;
  repos: string[]; // repos actually analyzed (the selected scope)
  repos_with_prs: string[];
  prs_by_repo: Record<string, number>;
  repos_scanned: number;
  proposals: number;
}

export interface RepoList {
  owner: string;
  repos: string[];
}

/** The newest thing the SDK has reported, for the Install SDK verification
 *  panel. Null until one arrives. Deliberately thin — enough to confirm an
 *  install and nothing from the call itself. */
/** What the OpenTelemetry setup guide shows once an exporter has reported. */
export interface OtelTrace {
  application: string;
  operation_name: string;
  span_count: number;
  received_at: string;
}

export interface HookEvent {
  /** Null when the event carried no feature: the Unattributed bucket. */
  feature_id: string | null;
  feature_name: string | null;
  provider: string;
  model: string | null;
  received_at: string;
  requests: number | null;
}

export interface DiscoveryScope {
  owner: string | null;
  repos: string[];
}

/** One discovery run: the window of merge dates it asked GitHub for, and how it
 *  ended. Failed runs are recorded too — an attempt that failed is a fact about
 *  coverage, not an absence of one. */
export interface DiscoveryRun {
  id: string;
  owner: string | null;
  repos: string[];
  covered_from: string;
  covered_to: string;
  prs: number;
  proposals: number;
  trigger: "manual" | "scheduled";
  status: "success" | "error";
  error_message: string | null;
  started_at: string;
  finished_at: string | null;
  started_by: string | null;
}

/** What discovery has covered, from the runs themselves rather than inferred
 *  from whichever pull requests happened to turn up. */
export interface DiscoveryCoverage {
  runs: number;
  covered_from: string | null;
  covered_to: string | null;
  last_run_at: string | null;
  last_run_status: "success" | "error" | null;
  last_run_trigger: "manual" | "scheduled" | null;
}

export interface DiscoverySchedule {
  /** False until discovery has run once: a scheduled run needs an owner and a
   *  repo selection, and there is nowhere to put a schedule without them. */
  configurable: boolean;
  enabled: boolean;
  lookback_days: number;
  next_run_at: string | null;
  owner: string | null;
  repos: string[];
}

export interface SplitGroup {
  name: string;
  signal_ids: string[];
}

// ---- Alerts ----
export interface AlertChannel {
  id?: string;
  channel: string; // in_app | email | slack | webhook
  target?: string | null;
  label?: string;
  secret?: string; // write-only (never returned)
  configured?: boolean;
}

export interface AlertRule {
  id: string;
  name: string;
  description: string | null;
  metric: string;
  metric_label: string;
  scope_type: string;
  scope_ref: string | null;
  scope_label: string | null;
  condition_type: string;
  threshold: number;
  budget_amount: number | null;
  window: string;
  cooldown: string;
  recovery_notify: boolean;
  enabled: boolean;
  status: string; // healthy | triggered | insufficient_data | delivery_error | disabled
  last_observed: number | null;
  last_evaluated_at: string | null;
  last_triggered_at: string | null;
  next_eval_at: string | null;
  created_by: string | null;
  created_at: string | null;
  updated_at: string | null;
  channels: AlertChannel[];
}

export interface AlertSummary {
  triggered: number;
  healthy: number;
  delivery_errors: number;
  disabled: number;
  unread: number;
}

export interface AlertActivityEvent {
  id: string;
  alert_id: string;
  alert_name: string;
  metric: string;
  metric_label: string;
  /** The rule's condition, so the feed can format the observed value. */
  condition_type: string;
  scope_type: string;
  scope_ref: string | null;
  scope_label: string | null;
  event_type: string; // triggered | resolved | delivery_error | test
  observed_value: number | null;
  threshold: number | null;
  window: string | null;
  message: string | null;
  read: boolean;
  occurred_at: string | null;
  deliveries: { channel: string; status: string }[];
}

export interface AlertMeta {
  metrics: { value: string; label: string }[];
  scopes: string[];
  conditions: string[];
  windows: string[];
  cooldowns: string[];
  channels: string[];
  valid_conditions: Record<string, string[]>;
  valid_scopes: Record<string, string[]>;
  /** What each metric's threshold is counted in — "money", "minutes", "steps". */
  metric_units: Record<string, string>;
  templates: { id: string; label: string; requires_budget?: boolean; rule: Partial<AlertRule> }[];
  /** Whether the organization has a budget for budget_pct rules to measure
   *  against. False means such a rule cannot be saved, and the form says so. */
  has_budget: boolean;
  budget_required_message: string;
  /** Conditions that need a budget — the server's list, not the form's guess. */
  budget_conditions: string[];
}

export type AlertInput = {
  name: string;
  description?: string | null;
  metric: string;
  scope_type: string;
  scope_ref?: string | null;
  condition_type: string;
  threshold: number;
  budget_amount?: number | null;
  window: string;
  cooldown: string;
  recovery_notify: boolean;
  enabled: boolean;
  channels: { channel: string; target?: string | null; secret?: string }[];
};

// ---- Shared cost-source resource classification ----
export type Classification = "production" | "development" | "internal" | "ignore" | "unclassified";

export const CLASSIFICATION_OPTIONS: { value: Classification; label: string }[] = [
  { value: "production", label: "Production" },
  { value: "development", label: "Development / Test" },
  { value: "internal", label: "Internal" },
  { value: "ignore", label: "Ignore" },
  { value: "unclassified", label: "Unclassified" },
];

export interface SourceResourceRow {
  resource_type: string;
  resource_id: string | null;
  name: string | null;
  group: string | null;
  classification: Classification;
  cost: number;
}

export interface SourceDetail {
  provider: string;
  period?: string | null;
  // When true, rows span all synced history and `cost` is the total across it.
  all_time?: boolean;
  classifiable: boolean;
  columns?: { group: string; name: string };
  rows: SourceResourceRow[];
  message?: string;
}

export interface DashboardRow {
  feature_id: string;
  name: string;
  category: string | null;
  category_source: string | null;
  build_cost: number;
  inference_cost: number;
  active_users: number | null;
  cost_per_user: number | null;
  requests: number | null;
  worth_it: string;
  confidence: string | null;
}

export interface DashboardHighlights {
  most_expensive: DashboardRow | null;
  optimization: DashboardRow | null;
  highest_cost_per_user: DashboardRow | null;
}

export interface Insight {
  kind: string;
  /** The finding, on its own. */
  text: string;
  /** The qualifier that would otherwise trail it in the same sentence. */
  detail: string;
}

/** Something a person could go and fix, with where to fix it. */
export interface OpenAction {
  kind: string;
  title: string;
  detail: string;
  href: string;
  tone: "warn" | "info";
}

/** One month of the Overview's trend. Build and inference stay apart, and the
 *  token counts ride along because they answer a different question: the same
 *  dollars can buy very different amounts of work. */
export interface TrendMonth {
  period: string;
  build_cost: number;
  inference_cost: number;
  tokens_in: number;
  /** A subset of tokens_in, not an addition to it. */
  cached_tokens_in: number;
  tokens_out: number;
  /** cached_tokens_in as a percentage of tokens_in; 0 when there is no input. */
  cache_rate: number;
}

/** Total spend per vendor over the period, with each one's own split.
 *  Named apart from ProviderSpend below, which is the By-provider tab's much
 *  larger payload. */
export interface ProviderTotal {
  provider: string;
  build_cost: number;
  inference_cost: number;
  amount: number;
  share: number;
}

export interface Dashboard {
  period: string;
  start: string;
  end: string;
  months: number;
  features: DashboardRow[];
  unattributed: { build_cost: number; inference_cost: number };
  highlights: DashboardHighlights;
  insights: Insight[];
  actions: OpenAction[];
  trend: TrendMonth[];
  providers: ProviderTotal[];
  // When cost data was last ingested (NOT when the page loaded). Null before any
  // sync/import has run.
  data_updated_at: string | null;
  inference_updated_at: string | null;
  build_updated_at: string | null;
  totals: {
    build_cost: number;
    inference_cost: number;
    // Portion of inference_cost that is estimated (not yet billed), for labelling.
    estimated_inference: number;
    prev_build_cost: number;
    prev_inference_cost: number;
    tokens_in: number;
    tokens_out: number;
  };
}

export interface FeatureDetail {
  feature_id: string;
  name: string;
  description: string;
  status: string;
  discovery_confidence: string | null;
  period: string;
  start: string;
  end: string;
  category: string | null;
  category_source: string | null;
  headline: {
    build_cost: number;
    inference_cost: number;
    active_users: number | null;
    avg_latency_ms: number | null;
  };
  build_total: number;
  build_contributors: number;
  build_by_developer: {
    developer_id: string;
    tool: string;
    amount: number;
    confidence: string;
    prs: number | null;
    commits: number | null;
    files_changed: number | null;
  }[];
  evidence: {
    signal_type: string;
    external_ref: string;
    confidence: string | null;
    actor: string | null;
    source: string | null;
  }[];
  inference_sources: string[];
  optimization: HeuristicOptimization;
}

/** The heuristic (estimated) optimization tier — directional rules of thumb. */
export interface HeuristicOptimization {
  opportunities: {
    opportunity: string;
    savings: number;
    confidence: string;
    rationale: string;
  }[];
  monthly_savings: number;
  annual_savings: number;
}

/** A unified optimization opportunity (opt spec §18). `savings_type` is the
 * canonical taxonomy; the three totals are computed separately and never combined. */
export interface Opportunity {
  lever: string;
  title: string;
  source: "connector" | "sdk" | "heuristic";
  savings_type: "measured" | "modeled_ceiling" | "directional";
  confidence: string;
  confidence_reason: string;
  projected_monthly_savings: number;
  projected_annual_savings: number;
  engineering_effort: "very_low" | "low" | "medium" | "high";
  priority_score: number;
  evidence: string;
  fix: string | null;
  validation_guidance: string;
  verification: string;
  status: string;
  overlaps: string | null; // set when superseded by an overlapping lever (opt spec §22)
  trail: {
    fingerprint?: string;
    provider?: string;
    model: string;
    note?: string;
    call_count?: number;
    calls?: number;
    prefix_tokens?: number;
    cached?: number;
  }[];
}

/** An applied optimization, reconciled projected-vs-realized (opt spec §11). */
export interface OptimizationAction {
  lever: string;
  applied_on: string;
  projected_monthly: number;
  current_avoidable: number;
  realized_monthly: number | null; // null until a later period can reconcile it
  status: "pending" | "measured" | "verified";
}

export interface FeatureOpportunities {
  period: string;
  opportunities: Opportunity[];
  totals: { measured: number; modeled_ceiling: number; directional: number };
  cache_utilization: number | null;
  actions: OptimizationAction[];
}

/** Tenant-wide optimization Overview (opt spec §21). Measured, modeled and verified
 * savings are three distinct figures — never combined. */
/** A recommendation derivable from billing data alone (no SDK telemetry). */
export interface BillingOpportunity {
  id: string;
  type: string;
  title: string;
  description: string;
  evidence: {
    source: string;
    period_start: string;
    period_end: string;
    observed_cost: number | null;
    token_count: number | null;
    resource_id: string | null;
    calculation: string;
  };
  confidence: "high" | "medium";
  impact: { kind: "spend_to_review" | "risk_reduction" | "visibility"; amount: number | null };
  savings: {
    kind: "measured" | "deterministic" | "not_quantified";
    amount: number | null;
    explanation: string;
  };
  limitations: string[];
  action: { label: string; href: string };
}

export interface CopilotOverview {
  period: string;
  totals: { measured: number; modeled_ceiling: number; directional: number };
  verified_monthly_savings: number;
  verified_annual_savings: number;
  top_recommendations: (Opportunity & { feature_id: string; feature_name: string })[];
  by_feature: {
    feature_id: string;
    name: string;
    measured: number;
    modeled_ceiling: number;
    directional: number;
  }[];
  by_lever: {
    lever: string;
    title: string;
    savings_type: string;
    monthly: number;
    count: number;
  }[];
  applied: (OptimizationAction & { feature_id: string; feature_name: string })[];
  // Billing-only path: kept out of every total above.
  has_sdk_telemetry: boolean;
  has_billing_data: boolean;
  billing_opportunities: BillingOpportunity[];
}

export interface FeatureInference {
  start: string;
  end: string;
  total: number;
  by_model: { model: string; amount: number; pct: number; requests: number | null }[];
  trend: { period: string; amount: number }[];
}

/** A review period: a named month-range, or an explicit custom month span. */
export type RangeKind =
  | "this_month"
  | "last_month"
  | "last_3_months"
  | "last_6_months"
  | "last_12_months"
  | "custom";
export interface ReviewRange {
  kind: RangeKind;
  start?: string; // YYYY-MM (custom only)
  end?: string; // YYYY-MM (custom only)
}

export function rangeQuery(r?: ReviewRange): string {
  if (!r) return "";
  if (r.kind === "custom") {
    // Until both months are picked, fall back to the backend default.
    return r.start && r.end ? `?start=${r.start}&end=${r.end}` : "";
  }
  return `?range=${r.kind}`;
}

// One month of inference spend, split by classification. Buckets sum to `total`
// (Ignore is excluded upstream).
export interface ClassificationTrendPoint {
  period: string;
  total: number;
  production: number;
  development: number;
  internal: number;
  unclassified: number;
  // Where the period's spend came from (providers that expose workspace identity).
  workspaces?: { workspace: string; amount: number }[];
}

/** Overview "By Customer" tab — SDK-metered spend, a subset of the real bill. */
export interface CustomerSpend {
  start: string;
  end: string;
  months: number;
  /** Metered (customer-tagged) inference spend in the window. */
  total: number;
  customers: {
    customer_id: string;
    amount: number;
    pct: number;
    requests: number | null;
    cost_per_request: number | null;
    /** Spend in the equal-length window before. null = new this window. */
    prev_amount: number | null;
    delta_pct: number | null;
    months_active: number;
  }[];
  trend: { period: string; amount: number }[];
  /** The whole inference bill for the window, so `total` reads as a subset. */
  inference_total: number;
  coverage_pct: number;
}

export interface ProviderSpend {
  start: string;
  end: string;
  total: number;
  by_provider: {
    provider: string;
    amount: number;
    pct: number;
    requests: number | null;
    by_model: { model: string; amount: number; pct: number }[];
  }[];
  // Inference trend, segmented by classification per month (a stacked bar).
  trend: ClassificationTrendPoint[];
  // Same shape at DAY resolution (used for short ranges); empty for older data.
  daily_trend: ClassificationTrendPoint[];
  build_total: number;
  build_by_tool: { tool: string; amount: number; pct: number }[];
  /** Engineering activity per developer over the same window (PR evidence). */
  developer_activity: {
    handle: string;
    label: string;
    prs: number;
    features: number;
    commits: number | null;
    files_changed: number | null;
    additions: number | null;
    deletions: number | null;
    build_cost: number;
    cost_per_pr: number | null;
  }[];
  /** Why the activity table is empty, when it is — so the UI can name which of
   *  the several possible reasons applies instead of rendering nothing at all. */
  activity_coverage: {
    github_connected: boolean;
    /** PR evidence anywhere in the tenant, not only in this window. */
    dated_prs: number;
    /** Discovered before merge dates were recorded, so placeable in no window. */
    undated_prs: number;
    first_merged: string | null;
    last_merged: string | null;
    /** Recorded coverage: successful runs, and the window they reached. Zero
     *  runs means discovery has never completed for this tenant. */
    runs: number;
    covered_from: string | null;
    covered_to: string | null;
    last_run_at: string | null;
    last_run_status: "success" | "error" | null;
    last_run_trigger: "manual" | "scheduled" | null;
  };
  build_by_developer: {
    developer_id: string;
    // Display label: "Name (handle)", or whichever identity is available.
    label: string;
    amount: number;
    pct: number;
    by_tool: { tool: string; amount: number; pct: number }[];
  }[];
  build_trend: { period: string; amount: number }[];
  customer_total: number;
  by_customer: { customer_id: string; amount: number; pct: number; requests: number | null }[];
  // Billed dollars split across token types (input / cache write / cache read /
  // output), weighted by each type's real rate. Sums to token_total.
  token_total: number;
  by_token_type: {
    token_type: string;
    label: string;
    amount: number;
    pct: number;
    tokens: number;
  }[];
  workspace_total: number;
  by_workspace: {
    workspace: string;
    amount: number;
    pct: number;
    // Provider-reported token counts (input + output), exact.
    tokens: number;
    by_key: { api_key: string; amount: number; pct: number; tokens: number }[];
  }[];
}

export interface SeatSource {
  id: string;
  provider: string;
  app_id: string;
  app_label: string | null;
  tool: string;
  plan: string;
}

export interface ComputePool {
  id: string;
  name: string;
  provider_label: string;
  monthly_cost: number;
}

export interface PoolAllocation {
  pool: string;
  provider_label: string;
  allocated: number;
  unattributed: number;
}

/** Support assistant: one handbook excerpt sent as grounding for a question. */
export interface AssistantPassage {
  id: string;
  title: string;
  category: string;
  text: string;
}

export interface AssistantReply {
  answer: string;
  /** Ids of the excerpts the answer used — rendered as links into the handbook. */
  sources: string[];
  /** False when the handbook did not cover the question. */
  answered: boolean;
  /** False when the reply is a handbook excerpt rather than a written answer. */
  composed: boolean;
}

export interface AssistantMeta {
  composed: boolean;
  support_email: string;
}

/** Provider invoice reconciliation. The whole module is opt-in per organization;
 *  `available` is false when the operator has disabled it for the installation. */
export interface ReconSettings {
  available: boolean;
  enabled: boolean;
  tolerance_abs: number;
  tolerance_pct: number;
  updated_at?: string | null;
  updated_by?: string | null;
  providers?: string[];
}

export interface ReconPreviewRow {
  row_number: number;
  service_date: string | null;
  provider_account: string;
  api_key_ref: string;
  model: string;
  usage_category: string;
  quantity: number | null;
  usage_subtotal: number;
  credit: number;
  tax: number;
  fee: number;
  adjustment: number;
  billed_amount: number;
  currency: string;
  status: string;
  errors: string[];
}

export interface ReconPreview {
  headers: string[];
  suggested_mapping: Record<string, string | null>;
  mapping: Record<string, string | null>;
  missing_required: string[];
  field_help: Record<string, string>;
  row_count: number;
  accepted_count: number;
  rejected_count: number;
  currencies: string[];
  period_start: string | null;
  period_end: string | null;
  usage_subtotal: number;
  credits: number;
  tax: number;
  fees: number;
  billed_total: number;
  rows: ReconPreviewRow[];
  rejected_rows: ReconPreviewRow[];
  checksum: string;
}

export interface ReconImport {
  id: string;
  provider: string;
  provider_account: string | null;
  filename: string;
  checksum: string;
  status: "committed" | "superseded" | "removed";
  currency: string;
  period_start: string | null;
  period_end: string | null;
  imported_by: string | null;
  imported_at: string | null;
  row_count: number;
  rejected_count: number;
  validation_errors: { row: number; errors: string[] }[];
  removed_at: string | null;
  run_count: number;
  duplicate?: boolean;
}

export interface ReconMatch {
  strategy: string;
  dimensions: Record<string, unknown>;
  provider_amount: number;
  tracked_amount: number;
  difference: number;
  difference_pct: number | null;
  classification: string;
  explanation: string;
  confidence: "confirmed" | "possible" | "unknown";
  evidence: string[];
}

export interface ReconRun {
  id: string;
  import_id: string | null;
  provider: string;
  provider_account: string | null;
  period_start: string;
  period_end: string;
  currency: string;
  status: "pending" | "matched" | "within_tolerance" | "discrepancy" | "incomplete_data" | "failed";
  tolerance_abs: number;
  tolerance_pct: number;
  provider_usage: number;
  provider_credits: number;
  provider_tax: number;
  provider_fees: number;
  provider_total: number;
  tracked_usage: number;
  usage_difference: number;
  usage_difference_pct: number | null;
  unmatched_provider_count: number;
  unmatched_tracked_count: number;
  created_by: string | null;
  created_at: string | null;
  completed_at: string | null;
  failure_reason: string | null;
  matches?: ReconMatch[];
  breakdown?: Record<string, { key: string; usage: number; lines: number }[]>;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    ...options,
  });

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      if (body?.detail) detail = typeof body.detail === "string" ? body.detail : detail;
    } catch {
      // non-JSON error body; keep statusText
    }
    throw new ApiError(response.status, detail);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  signup: (email: string, password: string) =>
    request<User>("/auth/signup", { method: "POST", body: JSON.stringify({ email, password }) }),

  login: (email: string, password: string) =>
    request<User>("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) }),

  logout: () => request<void>("/auth/logout", { method: "POST" }),

  me: () => request<User>("/auth/me"),

  // ---- Provider invoice reconciliation (opt-in module) ----
  reconSettings: () => request<ReconSettings>("/reconciliation/settings"),

  saveReconSettings: (body: {
    enabled?: boolean;
    tolerance_abs?: number;
    tolerance_pct?: number;
  }) =>
    request<ReconSettings>("/reconciliation/settings", {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  reconPreview: (content: string, mapping?: Record<string, string | null>) =>
    request<ReconPreview>("/reconciliation/preview", {
      method: "POST",
      body: JSON.stringify({ content, mapping }),
    }),

  reconImport: (body: {
    provider: string;
    filename: string;
    content: string;
    mapping?: Record<string, string | null>;
    replace_import_id?: string;
  }) =>
    request<ReconImport>("/reconciliation/imports", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  reconImports: () => request<ReconImport[]>("/reconciliation/imports"),

  removeReconImport: (id: string) =>
    request<ReconImport>(`/reconciliation/imports/${id}`, { method: "DELETE" }),

  reconRuns: () => request<ReconRun[]>("/reconciliation/runs"),

  reconRun: (id: string) => request<ReconRun>(`/reconciliation/runs/${id}`),

  runReconciliation: (importId: string) =>
    request<ReconRun>("/reconciliation/runs", {
      method: "POST",
      body: JSON.stringify({ import_id: importId }),
    }),

  reconReportUrl: (id: string) => `/api/reconciliation/runs/${id}/report.csv`,

  assistantMeta: () => request<AssistantMeta>("/assistant/meta"),

  askAssistant: (body: {
    question: string;
    passages: AssistantPassage[];
    history: { role: "user" | "assistant"; content: string }[];
    page: string;
  }) => request<AssistantReply>("/assistant/chat", { method: "POST", body: JSON.stringify(body) }),

  discoveryLlm: () => request<DiscoveryLlm>("/settings/discovery-llm"),

  discoveryLlmProviders: () => request<DiscoveryLlmProviders>("/settings/discovery-llm/providers"),

  saveDiscoveryLlm: (body: {
    provider: string;
    base_url: string;
    model: string;
    api_key?: string;
    enabled?: boolean;
  }) =>
    request<DiscoveryLlm>("/settings/discovery-llm", { method: "PUT", body: JSON.stringify(body) }),

  testDiscoveryLlm: (body: {
    provider?: string;
    base_url?: string;
    model?: string;
    api_key?: string;
  }) =>
    request<{ ok: boolean; model?: string; error?: string }>("/settings/discovery-llm/test", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  setDiscoveryLlmEnabled: (enabled: boolean) =>
    request<DiscoveryLlm>("/settings/discovery-llm", {
      method: "PATCH",
      body: JSON.stringify({ enabled }),
    }),

  removeDiscoveryLlm: () => request<DiscoveryLlm>("/settings/discovery-llm", { method: "DELETE" }),

  getBudget: () => request<{ budget: Budget | null }>("/budget"),

  setBudget: (body: BudgetInput) =>
    request<{ budget: Budget }>("/budget", { method: "PUT", body: JSON.stringify(body) }),

  removeBudget: () => request<{ budget: null }>("/budget", { method: "DELETE" }),

  // Takes the same window the Overview is showing, so the card and the chart
  // beside it can never disagree about which months are in view.
  budgetForecast: (range?: ReviewRange) =>
    request<BudgetForecast>(`/budget/forecast${rangeQuery(range)}`),

  getSettings: () => request<OrgSettings>("/settings"),

  updateSettings: (patch: Partial<OrgSettings>) =>
    request<OrgSettings>("/settings", { method: "PATCH", body: JSON.stringify(patch) }),

  // ---- Alerts ----
  alertsMeta: () => request<AlertMeta>("/alerts/meta"),
  listAlerts: () => request<{ rules: AlertRule[]; summary: AlertSummary }>("/alerts"),
  alertsSummary: () => request<AlertSummary>("/alerts/summary"),
  getAlert: (id: string) => request<AlertRule & { history: unknown }>(`/alerts/${id}`),
  createAlert: (body: AlertInput) =>
    request<AlertRule>("/alerts", { method: "POST", body: JSON.stringify(body) }),
  updateAlert: (id: string, body: AlertInput) =>
    request<AlertRule>(`/alerts/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteAlert: (id: string) => request<void>(`/alerts/${id}`, { method: "DELETE" }),
  enableAlert: (id: string, enabled: boolean) =>
    request<AlertRule>(`/alerts/${id}/enable`, {
      method: "POST",
      body: JSON.stringify({ enabled }),
    }),
  duplicateAlert: (id: string) => request<AlertRule>(`/alerts/${id}/duplicate`, { method: "POST" }),
  testAlert: (id: string) =>
    request<{ ok: boolean; deliveries: { channel: string; status: string }[] }>(
      `/alerts/${id}/test`,
      { method: "POST" },
    ),
  alertsActivity: () => request<{ events: AlertActivityEvent[] }>("/alerts/activity"),
  markAlertsRead: (event_ids: string[]) =>
    request<{ marked: number }>("/alerts/activity/read", {
      method: "POST",
      body: JSON.stringify({ event_ids }),
    }),
  markAllAlertsRead: () =>
    request<{ marked: number }>("/alerts/activity/read-all", { method: "POST" }),

  connectors: () => request<ConnectorStatus[]>("/connectors"),

  /** Add an account under a connector, or replace one by passing its id. */
  saveCredential: (connectorType: string, secret: string, label?: string, credentialId?: string) =>
    request<void>(`/connectors/${connectorType}/credential`, {
      method: "POST",
      body: JSON.stringify({ secret, label, credential_id: credentialId }),
    }),

  /** The accounts stored for a connector. Never includes a secret. */
  connectorCredentials: (connectorType: string) =>
    request<{ credentials: ConnectorCredential[] }>(`/connectors/${connectorType}/credentials`),

  deleteCredential: (connectorType: string, credentialId: string) =>
    request<void>(`/connectors/${connectorType}/credentials/${credentialId}`, {
      method: "DELETE",
    }),

  /** Build cost someone entered by hand. Additive: it sits alongside synced
   *  rows rather than replacing the tool's month. */
  addManualBuildCost: (entry: {
    developer: string;
    github_handle?: string;
    tool: string;
    amount: number;
    period?: string;
    months?: number;
  }) =>
    request<{ total: number }>("/build/manual", { method: "POST", body: JSON.stringify(entry) }),

  manualBuildCost: (period?: string) =>
    request<{ entries: ManualBuildEntry[] }>(`/build/manual${period ? `?period=${period}` : ""}`),

  deleteManualBuildCost: (id: string) => request<void>(`/build/manual/${id}`, { method: "DELETE" }),

  // ---- Discovery + features (wizard step 2) ----
  discoveryRepos: (owner: string) =>
    request<RepoList>(`/discovery/repos?owner=${encodeURIComponent(owner)}`),

  discoveryScope: () => request<DiscoveryScope>("/discovery/scope"),

  runDiscovery: (owner: string, repos: string[] = [], days = 90, since?: string) =>
    request<DiscoverySummary>("/discovery/run", {
      method: "POST",
      // `since` names the earliest merge date to fetch and wins over `days`, so
      // "cover March and April" travels as the window itself.
      body: JSON.stringify({ owner, repos, days, since: since ?? null }),
    }),

  recentHookEvent: () => request<{ event: HookEvent | null }>("/hook/recent"),

  /** The newest trace that arrived over OTLP. SDK traces never count here. */
  recentOtelTrace: () => request<{ trace: OtelTrace | null }>("/otel/recent"),

  discoveryRuns: () =>
    request<{ runs: DiscoveryRun[]; coverage: DiscoveryCoverage }>("/discovery/runs"),

  discoverySchedule: () => request<DiscoverySchedule>("/discovery/schedule"),

  setDiscoverySchedule: (enabled: boolean, lookbackDays?: number) =>
    request<DiscoverySchedule>("/discovery/schedule", {
      method: "PUT",
      body: JSON.stringify({ enabled, lookback_days: lookbackDays ?? null }),
    }),

  listFeatures: (status?: string) =>
    request<Feature[]>(`/features${status ? `?status=${status}` : ""}`),

  addFeature: (name: string, description = "") =>
    request<Feature>("/features", { method: "POST", body: JSON.stringify({ name, description }) }),

  renameFeature: (id: string, fields: { name?: string; description?: string }) =>
    request<Feature>(`/features/${id}`, { method: "PATCH", body: JSON.stringify(fields) }),

  deleteFeature: (id: string) => request<void>(`/features/${id}`, { method: "DELETE" }),

  splitFeature: (id: string, groups: SplitGroup[]) =>
    request<Feature[]>(`/features/${id}/split`, {
      method: "POST",
      body: JSON.stringify({ groups }),
    }),

  mergeFeatures: (featureIds: string[], name?: string) =>
    request<Feature>("/features/merge", {
      method: "POST",
      body: JSON.stringify({ feature_ids: featureIds, name }),
    }),

  confirmOnboarding: (featureIds?: string[]) =>
    request<Feature[]>("/onboarding/confirm", {
      method: "POST",
      body: JSON.stringify({ feature_ids: featureIds ?? null }),
    }),

  // ---- The three screens (M6) ----
  dashboard: (range?: ReviewRange) => request<Dashboard>(`/dashboard${rangeQuery(range)}`),

  featureDetail: (id: string, range?: ReviewRange) =>
    request<FeatureDetail>(`/features/${id}/detail${rangeQuery(range)}`),

  featureInference: (id: string, range?: ReviewRange) =>
    request<FeatureInference>(`/features/${id}/inference${rangeQuery(range)}`),

  featureOpportunities: (id: string, range?: ReviewRange) =>
    request<FeatureOpportunities>(`/features/${id}/opportunities${rangeQuery(range)}`),

  applyOpportunity: (id: string, lever: string, projectedMonthly: number) =>
    request<{ lever: string; applied_on: string }>(`/features/${id}/opportunities/apply`, {
      method: "POST",
      body: JSON.stringify({ lever, projected_monthly: projectedMonthly }),
    }),

  unapplyOpportunity: (id: string, lever: string) =>
    request<void>(`/features/${id}/opportunities/apply?lever=${lever}`, { method: "DELETE" }),

  copilotOverview: (period?: string) =>
    request<CopilotOverview>(`/copilot/overview${period ? `?period=${period}` : ""}`),

  // ---- Internal admin portal ----
  adminOverview: () => request<AdminOverview>("/admin/overview"),
  adminCustomers: () => request<AdminCustomer[]>("/admin/customers"),
  adminCustomer: (tenantId: string) => request<AdminCustomerDetail>(`/admin/customers/${tenantId}`),
  adminSaveConnector: (tenantId: string, connectorType: string, secret: string, label?: string) =>
    request<{ ok: boolean }>(`/admin/customers/${tenantId}/connectors`, {
      method: "POST",
      body: JSON.stringify({ connector_type: connectorType, secret, label: label ?? null }),
    }),
  adminTestConnector: (tenantId: string, connectorType: string) =>
    request<ConnectorActionResult>(
      `/admin/customers/${tenantId}/connectors/${connectorType}/test`,
      { method: "POST" },
    ),
  adminSyncConnector: (tenantId: string, connectorType: string) =>
    request<ConnectorActionResult>(
      `/admin/customers/${tenantId}/connectors/${connectorType}/sync`,
      { method: "POST" },
    ),
  adminDisconnectConnector: (tenantId: string, connectorType: string) =>
    request<void>(`/admin/customers/${tenantId}/connectors/${connectorType}`, { method: "DELETE" }),
  adminSyncHistory: () => request<AdminSyncRow[]>("/admin/sync-history"),
  adminErrors: () => request<AdminSyncRow[]>("/admin/errors"),
  impersonate: (tenantId: string) =>
    request<{ tenant_id: string; company: string }>(`/admin/impersonate/${tenantId}`, {
      method: "POST",
    }),
  stopImpersonate: () => request<void>("/admin/impersonate", { method: "DELETE" }),

  providerSpend: (range?: ReviewRange) =>
    request<ProviderSpend>(`/dashboard/providers${rangeQuery(range)}`),

  customerSpend: (range?: ReviewRange) =>
    request<CustomerSpend>(`/dashboard/customers${rangeQuery(range)}`),

  /** Tag a feature with its product surface; null clears the tag. */
  setFeatureCategory: (id: string, category: string | null) =>
    request<Feature>(`/features/${id}/category`, {
      method: "PUT",
      body: JSON.stringify({ category }),
    }),

  featureCategories: () =>
    request<{ categories: { value: string; label: string }[] }>("/features/categories"),

  setUsage: (id: string, activeUsers: number, period?: string) =>
    request<Feature>(`/features/${id}/usage`, {
      method: "PUT",
      body: JSON.stringify({ active_users: activeUsers, period }),
    }),

  ingestInference: (provider: string, period?: string, months?: number) =>
    request<{
      total: number;
      estimated?: number;
      months?: number;
      by_month?: { period: string; total: number; rows: number }[];
      errors?: { period: string; error: string }[];
    }>("/inference/ingest", {
      method: "POST",
      body: JSON.stringify({ provider, period, months }),
    }),

  /** Pull the current month from every connected inference provider. */
  refreshInference: () =>
    request<{
      providers: number;
      synced: { provider: string; total: number }[];
      errors: { provider: string; error: string }[];
      total: number;
    }>("/inference/refresh", { method: "POST" }),

  sourceDetail: (provider: string, period?: string) =>
    request<SourceDetail>(`/cost-sources/${provider}/detail${period ? `?period=${period}` : ""}`),

  classifyResource: (
    provider: string,
    body: { resource_type: string; resource_id: string; classification: Classification },
  ) =>
    request<{ classification: Classification }>(`/cost-sources/${provider}/classify`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  importBuildCost: (csv: string, tool?: string, period?: string) =>
    request<{ total: number; months_imported?: number }>("/build/import", {
      method: "POST",
      body: JSON.stringify({ csv, tool, period }),
    }),

  // ---- SSO/SCIM seat sources (Okta) ----
  listSeatSources: () => request<SeatSource[]>("/build/seat-sources"),

  registerSeatSource: (
    provider: string,
    appId: string,
    appLabel: string,
    tool: string,
    plan: string,
  ) =>
    request<SeatSource>("/build/seat-sources", {
      method: "POST",
      body: JSON.stringify({ provider, app_id: appId, app_label: appLabel, tool, plan }),
    }),

  syncIdpSeats: (period?: string) =>
    request<{
      total: number;
      total_seats: number;
      sources: { app_label: string; seats: number }[];
    }>("/build/seats/sync", { method: "POST", body: JSON.stringify({ period }) }),

  syncClaudeCodeSpend: (period?: string) =>
    request<{ total: number; members: number; spending_members: number }>(
      "/build/claude-code/sync",
      { method: "POST", body: JSON.stringify({ period }) },
    ),

  syncCursorSpend: (period?: string) =>
    request<{ total: number; members: number; spending_members: number }>("/build/cursor/sync", {
      method: "POST",
      body: JSON.stringify({ period }),
    }),

  syncCopilotSeats: (owner: string, period?: string) =>
    request<{ total: number; seats: number; plan: string; seat_price: number }>(
      "/build/copilot/sync",
      { method: "POST", body: JSON.stringify({ owner, period }) },
    ),

  recordTrainingCost: (featureId: string, amount: number, label: string, period?: string) =>
    request<{ total: number }>("/build/training", {
      method: "POST",
      body: JSON.stringify({ feature_id: featureId, amount, label, period }),
    }),

  // ---- Self-hosted compute pools (open-source inference) ----
  listComputePools: () => request<ComputePool[]>("/compute/pools"),

  createComputePool: (name: string, providerLabel: string, monthlyCost: number) =>
    request<ComputePool>("/compute/pools", {
      method: "POST",
      body: JSON.stringify({ name, provider_label: providerLabel, monthly_cost: monthlyCost }),
    }),

  allocateCompute: (period?: string, poolId?: string) =>
    request<PoolAllocation[]>("/compute/allocate", {
      method: "POST",
      body: JSON.stringify({ period, pool_id: poolId }),
    }),

  // ---- Infrastructure cost (cloud bill) ----
  infraProviders: () => request<InfraProvider[]>("/infrastructure/providers"),

  /** Re-read the bill. Cloud costs are restated for days, so a sync re-reads
   *  history rather than appending to it; the backend is idempotent. */
  syncInfrastructure: (provider: string, months?: number) =>
    request<{
      provider: string;
      items: number;
      infrastructure: number;
      excluded: number;
      attributed: number;
      unattributed: number;
      by_category: Record<string, number>;
    }>("/infrastructure/ingest", {
      method: "POST",
      body: JSON.stringify({ provider, months }),
    }),

  /** Preview or commit a downloaded bill. `dryRun` writes nothing. */
  importInfrastructureCsv: (
    provider: string,
    csv: string,
    opts: { dryRun?: boolean; tag?: string; mapping?: Record<string, string> } = {},
  ) =>
    request<InfraImportReport>("/infrastructure/import", {
      method: "POST",
      body: JSON.stringify({
        provider,
        csv,
        dry_run: opts.dryRun ?? false,
        tag: opts.tag ?? "feature",
        mapping: opts.mapping ?? null,
      }),
    }),

  infraSummary: (provider = "aws", period?: string) =>
    request<InfraSummary>(
      `/infrastructure/summary?provider=${encodeURIComponent(provider)}` +
        (period ? `&period=${encodeURIComponent(period)}` : ""),
    ),

  // ---- Request-level AI economics ----
  aiApplications: (params: { days?: number } = {}) =>
    request<{ applications: AiApplication[]; from: string; to: string }>(
      `/ai/applications${params.days ? `?days=${params.days}` : ""}`,
    ),

  aiApplication: (id: string, days?: number) =>
    request<AiApplication>(`/ai/applications/${id}${days ? `?days=${days}` : ""}`),

  createAiApplication: (name: string, slug?: string) =>
    request<{ id: string; name: string; slug: string }>("/ai/applications", {
      method: "POST",
      body: JSON.stringify({ name, slug }),
    }),

  renameAiApplication: (id: string, changes: { name?: string; owner?: string }) =>
    request<AiApplication>(`/ai/applications/${id}`, {
      method: "PATCH",
      body: JSON.stringify(changes),
    }),

  /** A page of traces. Every filter is optional; the server bounds the page. */
  aiTraces: (
    query: Record<string, string | number | boolean | undefined> = {},
    signal?: AbortSignal,
  ) => {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined && value !== "" && value !== false) params.set(key, String(value));
    }
    const qs = params.toString();
    return request<AiTracePage>(`/ai/traces${qs ? `?${qs}` : ""}`, signal ? { signal } : {});
  },

  aiTrace: (id: string) => request<AiTrace>(`/ai/traces/${encodeURIComponent(id)}`),

  // ---- Metering hook (M7, optional precision tier) ----
  createHookToken: () => request<{ token: string }>("/hook/token", { method: "POST" }),
};
