/**
 * Turning "each month, split by something" into the series a stacked chart
 * draws — apart from any drawing, so it can be tested on its own.
 *
 * A split can have any number of parts (developers, tools, products), and a
 * palette has six colours that stay distinguishable side by side. So the
 * largest parts are named and the rest are pooled into "Other", which is
 * counted, never dropped: a month's series always add up to its bar.
 */

export interface StackSeries {
  key: string;
  label: string;
  /** A CSS colour, normally one of the --chart-N tokens. */
  color: string;
  /** One value per period, in the order of the chart's periods. */
  values: number[];
  /** A residual rather than a real thing (Unattributed, Other): drawn grey,
   *  and listed quieter in the hover card. */
  muted?: boolean;
}

/** Parts named on their own before the rest are pooled. */
export const NAMED_PARTS = 6;
export const OTHER_KEY = "__other__";

export const PALETTE = [1, 2, 3, 4, 5, 6].map((n) => `var(--chart-${n})`);
export const OTHER_COLOR = "var(--chart-none)";

export interface Part {
  key: string;
  label: string;
  amount: number;
}

/**
 * Series from a per-period split, largest first across the whole range.
 *
 * `hidden` matters only for the pool: a part someone switched off elsewhere
 * (in the list beside the chart, say) is left out of "Other" too, so hiding it
 * hides it everywhere rather than only where it happens to be named.
 */
export function seriesFromSplits(
  splits: Part[][],
  options: { limit?: number; hidden?: ReadonlySet<string>; residualKeys?: string[] } = {},
): StackSeries[] {
  const limit = options.limit ?? NAMED_PARTS;
  const hidden = options.hidden ?? new Set<string>();
  const residual = new Set(options.residualKeys ?? []);
  const totals = new Map<string, { label: string; total: number }>();
  for (const parts of splits) {
    for (const part of parts) {
      const held = totals.get(part.key);
      totals.set(part.key, {
        label: held?.label ?? part.label,
        total: (held?.total ?? 0) + part.amount,
      });
    }
  }
  const ranked = [...totals.entries()]
    .filter(([, t]) => t.total > 0)
    .sort((a, b) => b[1].total - a[1].total)
    .map(([key]) => key);
  // One fewer named when there is a remainder, so the legend never holds more
  // than `limit` entries with "Other" counted among them.
  const named = ranked.length > limit ? ranked.slice(0, limit - 1) : ranked;
  const pooled = ranked.filter((key) => !named.includes(key));

  let colour = 0;
  const series: StackSeries[] = named.map((key) => {
    const isResidual = residual.has(key);
    return {
      key,
      label: totals.get(key)!.label,
      color: isResidual ? OTHER_COLOR : PALETTE[colour++ % PALETTE.length],
      values: splits.map((parts) => parts.find((p) => p.key === key)?.amount ?? 0),
      muted: isResidual || undefined,
    };
  });
  if (pooled.length > 0) {
    const inPool = new Set(pooled.filter((key) => !hidden.has(key)));
    series.push({
      key: OTHER_KEY,
      label: `Other (${pooled.length})`,
      color: OTHER_COLOR,
      values: splits.map((parts) =>
        parts.filter((p) => inPool.has(p.key)).reduce((sum, p) => sum + p.amount, 0),
      ),
      muted: true,
    });
  }
  return series;
}

/** What one period adds up to with some series switched off. */
export function visibleSum(
  series: StackSeries[],
  index: number,
  hidden: ReadonlySet<string>,
): number {
  return series.reduce((sum, s) => (hidden.has(s.key) ? sum : sum + s.values[index]), 0);
}
