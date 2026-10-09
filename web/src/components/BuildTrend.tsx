/**
 * Build cost per month, stacked by coding tool or by developer.
 *
 * Sits beside the list of the same tools or developers and shares what is
 * hidden with it: leave Cursor out of the list and it leaves the trend too.
 * An older server sends the monthly total alone, and then this is the plain
 * single-series chart it always was.
 */
import type { ProviderSpend } from "../api";
import { seriesFromSplits } from "../chartSeries";
import { prettyTool } from "../format";
import { StackedTrend } from "./StackedTrend";
import { TrendChart } from "./TrendChart";

export function BuildTrend({
  trend,
  by,
  hidden,
  onHiddenChange,
}: {
  trend: ProviderSpend["build_trend"];
  by: "tool" | "developer";
  hidden: ReadonlySet<string>;
  onHiddenChange: (hidden: Set<string>) => void;
}) {
  const split = trend.some((t) => (by === "tool" ? t.by_tool : t.by_developer) !== undefined);
  if (!split) return <TrendChart trend={trend} />;

  const series = seriesFromSplits(
    trend.map((t) =>
      by === "tool"
        ? (t.by_tool ?? []).map((p) => ({
            key: p.tool,
            label: prettyTool(p.tool),
            amount: p.amount,
          }))
        : (t.by_developer ?? []).map((p) => ({
            key: p.developer_id,
            label: p.label,
            amount: p.amount,
          })),
    ),
    // Build cost with no developer is not a developer: grey, like every residual.
    { hidden, residualKeys: ["Unattributed"] },
  );

  return (
    <StackedTrend
      periods={trend.map((t) => t.period)}
      series={series}
      ariaLabel={`Build cost per month, by ${by}`}
      legendLabel={`Build cost by ${by}`}
      hidden={hidden}
      onHiddenChange={onHiddenChange}
    />
  );
}
