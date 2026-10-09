/**
 * Metered AI cost and human cost, month by month, stacked.
 *
 * The By Customer tab used to draw metered spend alone with `TrendChart`. That
 * component takes one series and is shared by three other screens, so widening
 * it to two would complicate all of them for the benefit of one; this is the
 * the shared stacked trend with the bar split in two, either half of which can
 * be switched off from the legend.
 *
 * The two halves are never summed into a single labelled figure without saying
 * what went into it: the legend names both, and the hover gives each on its own
 * line before the total. A stack whose segments are unlabelled is exactly the
 * blended number this product exists to take apart.
 */
import type { StackSeries } from "../chartSeries";
import { StackedTrend } from "./StackedTrend";

type Series = { period: string; amount: number }[];

export function DeliveryCostTrend({
  ai,
  human,
  effortPresent,
}: {
  /** The metered-spend trend this chart has always drawn. */
  ai: Series;
  human: Series;
  effortPresent: boolean;
}) {
  const humanBy = new Map(human.map((h) => [h.period, h.amount]));
  const aiBy = new Map(ai.map((a) => [a.period, a.amount]));
  const periods = [...new Set([...aiBy.keys(), ...humanBy.keys()])].sort();
  const series: StackSeries[] = [
    // AI at the bottom, so it keeps the baseline it had before this view
    // gained a second series.
    {
      key: "ai",
      label: "Metered AI cost",
      color: "var(--chart-1)",
      values: periods.map((p) => aiBy.get(p) ?? 0),
    },
  ];
  // With no effort data at all there is no human series, rather than one of
  // zeros: "nobody logged hours" is not "nobody worked". Inside a period that
  // HAS effort data, a month with no rows is a real zero.
  if (effortPresent) {
    series.push({
      key: "human",
      label: "Human cost",
      color: "var(--chart-2)",
      values: periods.map((p) => humanBy.get(p) ?? 0),
    });
  }
  return (
    <StackedTrend
      periods={periods}
      series={series}
      ariaLabel="Delivery cost per month: metered AI and human cost"
      legendLabel="Delivery cost legend"
    />
  );
}
