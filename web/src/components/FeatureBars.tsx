/**
 * The By Feature tab's chart: the features that cost most, ranked.
 *
 * One measure at a time, chosen in place — inference, build, cost per active
 * user or requests — so a feature's build and inference are never drawn as one
 * bar (invariant 2). It ranks whatever the tab's own product filter and search
 * leave in the table, so slicing the table slices the chart. Rows can be left
 * out like any SpendBars list.
 */
import { useState } from "react";
import type { DashboardRow } from "../api";
import { compact, money } from "../format";
import { SpendBars } from "./SpendBars";

/** Bars shown; the table below always has every feature. */
const TOP = 6;

type Measure = "inference_cost" | "build_cost" | "cost_per_user" | "requests";

const MEASURES: { key: Measure; label: string; format: (v: number) => string }[] = [
  { key: "inference_cost", label: "Inference", format: money },
  { key: "build_cost", label: "Build", format: money },
  { key: "cost_per_user", label: "Cost per user", format: money },
  { key: "requests", label: "Requests", format: (v) => compact(v) },
];

export function FeatureBars({
  features,
  unattributed,
}: {
  features: DashboardRow[];
  /** The Unattributed row's build and inference, or null when the table is
   *  filtered to a product (spend with no feature has no product either). */
  unattributed: { build_cost: number; inference_cost: number } | null;
}) {
  const [measure, setMeasure] = useState<Measure>("inference_cost");
  const chosen = MEASURES.find((m) => m.key === measure)!;

  const rows = features
    .map((f) => ({ key: f.feature_id, label: f.name, amount: Number(f[measure] ?? 0) }))
    .filter((r) => r.amount > 0);
  // Unattributed is a money residual: it has no users and no requests, and is
  // ranked with the features only where it is the same kind of number.
  if (unattributed && (measure === "inference_cost" || measure === "build_cost")) {
    const amount = unattributed[measure];
    if (amount > 0) rows.push({ key: "__unattributed", label: "Unattributed", amount });
  }
  rows.sort((a, b) => b.amount - a.amount);
  // A share of a per-user cost means nothing, so it is ranked without one.
  const additive = measure !== "cost_per_user";
  const total = rows.reduce((sum, r) => sum + r.amount, 0);
  const top = rows.slice(0, TOP);

  return (
    <section className="feature-bars" aria-label="Top features">
      <div className="feature-bars-head">
        <span className="chart-title">Top features by</span>
        <select
          className="feature-bars-pick"
          aria-label="Rank features by"
          value={measure}
          onChange={(e) => setMeasure(e.target.value as Measure)}
        >
          {MEASURES.map((m) => (
            <option key={m.key} value={m.key}>
              {m.label}
            </option>
          ))}
        </select>
      </div>
      {top.length === 0 ? (
        <p className="muted">No {chosen.label.toLowerCase()} recorded for these features.</p>
      ) : (
        <>
          <SpendBars
            key={measure}
            rows={top.map((r) => ({
              ...r,
              pct:
                additive && total > 0 ? (r.amount / total) * 100 : (r.amount / top[0].amount) * 100,
            }))}
            format={chosen.format}
            showShare={additive}
            // Feature names are the customer's own; never re-cased.
            verbatim
            dense
          />
          {rows.length > TOP && (
            <p className="muted feature-bars-more">{rows.length - TOP} more in the table below.</p>
          )}
        </>
      )}
    </section>
  );
}
