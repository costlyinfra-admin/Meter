/**
 * Words for "Test this" (EX-1, docs/experiments-spec.md), shared by the
 * recommendation card, the setting page and the result page.
 */
import type { Experiment, OpportunityTest } from "./api";

/** Recommendation titles by lever, for pages that have only the lever. */
export const LEVER_TITLES: Record<string, string> = {
  duplicate_calls: "Repeated request candidates",
  prompt_caching: "Prompt caching",
  model_rightsizing: "Model right-sizing",
};

export const FRESHNESS_CHOICES: { seconds: number; label: string; hint: string }[] = [
  {
    seconds: 60,
    label: "1 minute",
    hint: "Answers go stale quickly: prices, live status, anything time-sensitive.",
  },
  { seconds: 600, label: "10 minutes", hint: "What Meter assumes unless you say otherwise." },
  { seconds: 3600, label: "1 hour", hint: "Answers hold for the length of a working session." },
  {
    seconds: 86400,
    label: "24 hours",
    hint: "Answers rarely change: documentation, classifications of fixed text.",
  },
];

export const CACHE_CHOICES: { ttl: "5m" | "1h"; label: string; hint: string }[] = [
  {
    ttl: "5m",
    label: "5 minutes",
    hint: "The provider's default. Cheaper to write, but it lapses between sparse calls.",
  },
  {
    ttl: "1h",
    label: "1 hour",
    hint: "Costs more each time it is written, but is rewritten far less often.",
  },
];

/** A test's state in a few words, and the badge class that goes with it. */
export function testStatus(
  t: Pick<OpportunityTest | Experiment, "status" | "outcome"> & { mode?: string },
): {
  label: string;
  className: string;
} {
  if (t.status === "waiting_for_data")
    return { label: "Waiting for data", className: "test-waiting" };
  if (t.status === "waiting_for_results")
    return { label: "Waiting for results", className: "test-waiting" };
  if (t.status === "cancelled") return { label: "Cancelled", className: "test-cancelled" };
  if (t.outcome === "passed")
    return { label: t.mode === "offline" ? "Tested" : "Simulated", className: "test-passed" };
  if (t.outcome === "failed") return { label: "Tested — did not hold", className: "test-failed" };
  return { label: "Inconclusive", className: "test-waiting" };
}

/** "May 21, 2026" from an ISO date or timestamp. */
export function testDate(iso: string): string {
  const d = new Date(iso.length === 10 ? `${iso}T00:00:00` : iso);
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}
