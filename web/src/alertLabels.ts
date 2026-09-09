/** Shared display labels + helpers for the Alerts feature. */
import { money } from "./format";
import type { AlertRule } from "./api";

export const METRIC_LABELS: Record<string, string> = {
  inference_cost: "Inference cost",
  build_cost: "Build cost",
  combined_cost: "Combined AI cost",
  cost_per_user: "Cost per active user",
  token_usage: "Token usage",
  unattributed_cost: "Unattributed spend",
  stale_agents: "Stale agent runs",
  agent_runtime: "Longest agent runtime (minutes)",
  agent_steps: "Steps in a single run",
  cost_per_run: "Cost per agent run",
  retry_loop: "Repeats of one step in a run",
  failed_run_cost: "Cost incurred by failed runs",
  cache_hit_rate: "Prompt cache hit rate (%)",
};

/**
 * What a threshold is counted in. Mirrors alerts.METRIC_UNITS on the server and
 * arrives on /api/alerts/meta as `metric_units`; this copy is the fallback so a
 * threshold is never silently labelled in dollars when it is in minutes.
 */
export const METRIC_UNITS: Record<string, string> = {
  inference_cost: "money",
  build_cost: "money",
  combined_cost: "money",
  cost_per_user: "money",
  token_usage: "tokens",
  unattributed_cost: "money",
  stale_agents: "runs",
  agent_runtime: "minutes",
  agent_steps: "steps",
  cost_per_run: "money",
  retry_loop: "repeats",
  failed_run_cost: "money",
  cache_hit_rate: "percent",
};

/** The word that goes on the threshold input's label. */
export const UNIT_LABELS: Record<string, string> = {
  money: "$",
  tokens: "tokens",
  runs: "runs",
  minutes: "minutes",
  steps: "steps",
  repeats: "repeats",
  percent: "%",
};

/** A threshold or observed value written in its metric's own units. */
export function quantity(metric: string, value: number): string {
  const unit = METRIC_UNITS[metric] ?? "money";
  if (unit === "money") return money(value);
  if (unit === "percent")
    return `${value.toLocaleString(undefined, { maximumFractionDigits: 1 })}%`;
  if (unit === "repeats") return `${value.toLocaleString()}x`;
  // "1 run", not "1 runs". Mirrors _suffix() in alerts_eval.py.
  const word = value === 1 && unit.endsWith("s") ? unit.slice(0, -1) : unit;
  return `${value.toLocaleString(undefined, { maximumFractionDigits: 1 })} ${word}`;
}

/**
 * An observed value or threshold, in the units the RULE reports it in.
 *
 * The condition wins over the metric: an increase_pct rule on cost per run
 * reports a percentage change, not dollars. Mirrors `_quantity` in
 * alerts_eval.py so the in-app feed and the Slack message agree.
 */
export function ruleQuantity(
  r: { metric: string; condition_type?: string | null },
  value: number,
): string {
  if (r.condition_type === "increase_pct" || r.condition_type === "budget_pct")
    return `${value.toLocaleString(undefined, { maximumFractionDigits: 1 })}%`;
  return quantity(r.metric, value);
}

export const SCOPE_LABELS: Record<string, string> = {
  organization: "Entire organization",
  provider: "Provider",
  model: "Model",
  feature: "Feature",
  application: "AI application",
};

export const CONDITION_LABELS: Record<string, string> = {
  exceeds: "Exceeds a fixed value",
  increase_pct: "Increases by more than %",
  budget_pct: "Exceeds % of monthly budget",
  falls_below: "Falls below a value",
};

export const WINDOW_LABELS: Record<string, string> = {
  hourly: "Hourly",
  daily: "Daily",
  weekly: "Weekly",
  monthly: "Monthly",
};

export const COOLDOWN_LABELS: Record<string, string> = {
  none: "No cooldown",
  hour: "Once per hour",
  day: "Once per day",
  week: "Once per week",
};

export const CHANNEL_LABELS: Record<string, string> = {
  in_app: "In-app",
  email: "Email",
  slack: "Slack webhook",
  webhook: "Generic webhook",
};

export const STATUS_LABELS: Record<string, string> = {
  healthy: "Healthy",
  triggered: "Triggered",
  insufficient_data: "Insufficient data",
  delivery_error: "Delivery error",
  disabled: "Disabled",
};

export const EVENT_LABELS: Record<string, string> = {
  triggered: "Triggered",
  resolved: "Resolved",
  delivery_error: "Delivery error",
  test: "Test notification",
};

export function statusClass(status: string): string {
  return `alert-status alert-status-${status}`;
}

/** "Notify me when daily inference cost exceeds $100." */
export function previewText(r: {
  metric: string;
  scope_type: string;
  scope_ref?: string | null;
  scope_label?: string | null;
  condition_type: string;
  threshold: number;
  window: string;
}): string {
  const metric = (METRIC_LABELS[r.metric] ?? r.metric).toLowerCase();
  const scopeRef = r.scope_label ?? r.scope_ref;
  const scopePart = r.scope_type !== "organization" && scopeRef ? ` for ${scopeRef}` : "";
  let cond: string;
  if (r.condition_type === "exceeds") cond = `exceeds ${quantity(r.metric, r.threshold)}`;
  else if (r.condition_type === "falls_below")
    cond = `falls below ${quantity(r.metric, r.threshold)}`;
  else if (r.condition_type === "increase_pct")
    cond = `increases by more than ${r.threshold}% vs the previous ${r.window} period`;
  else cond = `exceeds ${r.threshold}% of the monthly budget`;
  return `Notify me when ${r.window} ${metric}${scopePart} ${cond}.`;
}

/**
 * The most relevant in-app cost view for a rule's scope — feature detail for a
 * feature-scoped alert, Cost Sources for provider/model, otherwise the Overview.
 */
export function costLink(scopeType: string, scopeRef?: string | null): string {
  if (scopeType === "feature" && scopeRef) return `/features/${scopeRef}`;
  if (scopeType === "application" && scopeRef) return `/applications/${scopeRef}`;
  if (scopeType === "provider" || scopeType === "model") return "/cost-sources";
  return "/";
}

/** How a rule's condition reads in the table. */
export function conditionText(r: AlertRule): string {
  if (r.condition_type === "exceeds") return `exceeds ${quantity(r.metric, r.threshold)}`;
  if (r.condition_type === "falls_below") return `below ${quantity(r.metric, r.threshold)}`;
  if (r.condition_type === "increase_pct") return `+${r.threshold}% vs previous`;
  return `> ${r.threshold}% of budget`;
}
