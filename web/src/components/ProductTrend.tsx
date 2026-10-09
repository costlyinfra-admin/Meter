/**
 * Spend per product, month by month — a stacked bar per month, one segment per
 * product, plus the two residuals.
 *
 * WHY A TOGGLE RATHER THAN ONE STACK OF BOTH KINDS. A segment per product whose
 * height was build + inference would put a blended per-product figure on screen,
 * which is the one thing this product never does (invariant 2). So the chart
 * draws one kind of money at a time and says which: every segment, every axis
 * label and every hover figure is build cost, or inference cost, never a sum of
 * the two.
 *
 * The segments include Unassigned and Unattributed, because a chart of products
 * alone would show a rising total as features get assigned and look like growth
 * that never happened. Both are grey: neither is a product.
 *
 * Any product, or either residual, can be switched off from the legend; the
 * drawing itself is the shared StackedTrend.
 */
import { useState } from "react";
import { type ProductTrendMonth } from "../api";
import { OTHER_COLOR, PALETTE, type StackSeries } from "../chartSeries";
import { StackedTrend } from "./StackedTrend";

type Kind = "inference_cost" | "build_cost";

/** One series per product, in the order the months list them, then the two
 *  residuals. Every value is one kind of money. */
function productSeries(trend: ProductTrendMonth[], kind: Kind): StackSeries[] {
  const products = new Map<string, string>();
  for (const month of trend) {
    for (const p of month.products)
      if (!products.has(p.product_id)) products.set(p.product_id, p.name);
  }
  return [
    ...[...products.entries()].map(([id, name], i) => ({
      key: id,
      label: name,
      // The ramp cycles: a customer's product list has no fixed length.
      color: PALETTE[i % PALETTE.length],
      values: trend.map((m) => m.products.find((p) => p.product_id === id)?.[kind] ?? 0),
    })),
    {
      key: "__unassigned",
      label: "Unassigned",
      color: OTHER_COLOR,
      values: trend.map((m) => m.unassigned[kind]),
      muted: true,
    },
    {
      key: "__unattributed",
      label: "Unattributed",
      color: "var(--muted)",
      values: trend.map((m) => m.unattributed[kind]),
      muted: true,
    },
  ];
}

export function ProductTrend({ trend }: { trend: ProductTrendMonth[] }) {
  const [kind, setKind] = useState<Kind>("inference_cost");
  if (trend.length === 0) return <p className="muted">No data yet.</p>;
  const word = kind === "build_cost" ? "build" : "inference";

  return (
    <div>
      <div className="trend-toggle" role="group" aria-label="Which cost">
        <button
          type="button"
          className={kind === "inference_cost" ? "active" : ""}
          aria-pressed={kind === "inference_cost"}
          onClick={() => setKind("inference_cost")}
        >
          Inference
        </button>
        <button
          type="button"
          className={kind === "build_cost" ? "active" : ""}
          aria-pressed={kind === "build_cost"}
          onClick={() => setKind("build_cost")}
        >
          Build
        </button>
      </div>
      <StackedTrend
        periods={trend.map((m) => m.period)}
        series={productSeries(trend, kind)}
        ariaLabel={`${kind === "build_cost" ? "Build" : "Inference"} cost per product, per month`}
        legendLabel="Product legend"
        emptyText={`No ${word} cost in this period.`}
      />
    </div>
  );
}
