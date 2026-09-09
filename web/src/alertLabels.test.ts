import { describe, expect, it } from "vitest";
import {
  conditionText,
  costLink,
  previewText,
  quantity,
  ruleQuantity,
  statusClass,
} from "./alertLabels";

describe("alertLabels", () => {
  it("builds a plain-language preview for a fixed-value rule", () => {
    expect(
      previewText({
        metric: "inference_cost",
        scope_type: "organization",
        condition_type: "exceeds",
        threshold: 100,
        window: "daily",
      }),
    ).toBe("Notify me when daily inference cost exceeds $100.");
  });

  it("names the scope reference when the rule is scoped", () => {
    expect(
      previewText({
        metric: "inference_cost",
        scope_type: "provider",
        scope_ref: "anthropic",
        condition_type: "increase_pct",
        threshold: 25,
        window: "weekly",
      }),
    ).toContain("for anthropic");
  });

  it("phrases the budget-percentage condition", () => {
    expect(
      previewText({
        metric: "combined_cost",
        scope_type: "organization",
        condition_type: "budget_pct",
        threshold: 90,
        window: "monthly",
      }),
    ).toBe("Notify me when monthly combined ai cost exceeds 90% of the monthly budget.");
  });

  it("summarizes the condition for the table", () => {
    expect(conditionText({ condition_type: "increase_pct", threshold: 40 } as never)).toBe(
      "+40% vs previous",
    );
  });

  it("maps status to a namespaced css class", () => {
    expect(statusClass("delivery_error")).toBe("alert-status alert-status-delivery_error");
  });

  it("links each scope to the most relevant cost view", () => {
    expect(costLink("feature", "abc-123")).toBe("/features/abc-123");
    expect(costLink("application", "app-9")).toBe("/applications/app-9");
    expect(costLink("provider", "anthropic")).toBe("/cost-sources");
    expect(costLink("model", "claude")).toBe("/cost-sources");
    expect(costLink("organization", null)).toBe("/");
  });
});

describe("alertLabels — request-level metrics", () => {
  it("writes a threshold in the units its metric is actually counted in", () => {
    // The bug this guards: every threshold rendered as dollars, so "notify me
    // when a run takes more than 30 minutes" read as "more than $30.00".
    expect(quantity("agent_runtime", 30)).toBe("30 minutes");
    expect(quantity("agent_steps", 12)).toBe("12 steps");
    expect(quantity("retry_loop", 5)).toBe("5x");
    expect(quantity("cache_hit_rate", 40)).toBe("40%");
    expect(quantity("cost_per_run", 1.42)).toBe("$1.42");
    // "1 run", not "1 runs" — the stale-agent alert's most common reading.
    expect(quantity("stale_agents", 1)).toBe("1 run");
    expect(quantity("stale_agents", 3)).toBe("3 runs");
  });

  it("phrases a falls-below rule as a fall, not a rise", () => {
    expect(
      previewText({
        metric: "cache_hit_rate",
        scope_type: "organization",
        condition_type: "falls_below",
        threshold: 40,
        window: "daily",
      }),
    ).toBe("Notify me when daily prompt cache hit rate (%) falls below 40%.");
  });

  it("names the application a run-level rule is scoped to", () => {
    expect(
      previewText({
        metric: "agent_steps",
        scope_type: "application",
        scope_ref: "a-1",
        scope_label: "Support agent",
        condition_type: "exceeds",
        threshold: 20,
        window: "hourly",
      }),
    ).toBe("Notify me when hourly steps in a single run for Support agent exceeds 20 steps.");
  });

  it("reports a percentage rule as a percentage, whatever its metric counts", () => {
    // The observed value of an increase_pct rule is a percentage change. Read
    // as dollars it says "$40.00" for "40% dearer than yesterday".
    expect(ruleQuantity({ metric: "cost_per_run", condition_type: "increase_pct" }, 40)).toBe(
      "40%",
    );
    expect(ruleQuantity({ metric: "cost_per_run", condition_type: "exceeds" }, 1.42)).toBe("$1.42");
    expect(ruleQuantity({ metric: "agent_steps", condition_type: "exceeds" }, 12)).toBe("12 steps");
  });

  it("summarizes a falls-below condition for the table", () => {
    expect(
      conditionText({
        metric: "cache_hit_rate",
        condition_type: "falls_below",
        threshold: 40,
      } as never),
    ).toBe("below 40%");
  });
});
