/**
 * The Overview spend trend's slicing, apart from its drawing.
 *
 * Three independent choices decide what the bars show:
 *   - the MEASURE: dollars or tokens;
 *   - the SPLIT: what each bar is stacked by — the kind of money (build vs
 *     inference; for tokens, uncached input / cached input / output) or the
 *     vendor it was paid to;
 *   - the FILTER: everything, or one vendor's share of it.
 * A legend click then hides or shows one series. All of it is arithmetic on
 * numbers the server already sent; nothing here estimates or blends.
 *
 * Build and inference stay separate series in every view. The vendor split adds
 * them within one vendor only to answer "how much did we pay them", exactly as
 * the provider list beside the chart does, and the hover card still carries
 * each vendor's own split.
 */
import type { TrendMonth, TrendProvider } from "./api";

export type TrendMeasure = "cost" | "tokens";
export type TrendSplit = "kind" | "provider";

export interface TrendSeries {
  key: string;
  label: string;
  /** A CSS colour, normally one of the --chart-N tokens. */
  color: string;
}

export interface TrendView {
  series: TrendSeries[];
  /** values[month][series], in the order of `series` (bottom of the stack first). */
  values: number[][];
}

/** Vendors named on their own before the rest are pooled as "Other". Six is
 *  as many colours as the palette can keep apart side by side. */
export const NAMED_PROVIDERS = 6;
export const OTHER_KEY = "__other__";

const PALETTE = [1, 2, 3, 4, 5, 6].map((n) => `var(--chart-${n})`);
const OTHER_COLOR = "var(--chart-none)";

/** "self_hosted" -> "Self hosted". */
export function providerLabel(name: string): string {
  const words = name.replace(/[_-]+/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** Whether the server sent the per-vendor split this view needs. */
export function hasProviderSplit(trend: TrendMonth[]): boolean {
  return trend.some((m) => m.by_provider !== undefined);
}

/** What one month holds once the filter is applied: the whole month, or the
 *  single vendor's row of it (zeros when the vendor had nothing that month). */
function slice(
  month: TrendMonth,
  provider: string | null,
): { totals: TrendProvider; rows: TrendProvider[] } {
  const rows = month.by_provider ?? [];
  if (provider === null) {
    return {
      totals: {
        provider: "",
        build_cost: month.build_cost,
        inference_cost: month.inference_cost,
        tokens_in: month.tokens_in,
        cached_tokens_in: month.cached_tokens_in,
        tokens_out: month.tokens_out,
      },
      rows,
    };
  }
  const row = rows.find((r) => r.provider === provider) ?? {
    provider,
    build_cost: 0,
    inference_cost: 0,
    tokens_in: 0,
    cached_tokens_in: 0,
    tokens_out: 0,
  };
  return { totals: row, rows: [row] };
}

function rowValue(row: TrendProvider, measure: TrendMeasure): number {
  return measure === "cost" ? row.build_cost + row.inference_cost : row.tokens_in + row.tokens_out;
}

export function buildTrendView(
  trend: TrendMonth[],
  measure: TrendMeasure,
  split: TrendSplit,
  provider: string | null,
): TrendView {
  const months = trend.map((m) => slice(m, provider));

  if (split === "kind") {
    if (measure === "cost") {
      return {
        series: [
          // Build at the bottom, inference on top: the order the chart has
          // always stacked them in, so the shape a reader knows does not move.
          { key: "build", label: "Build", color: "var(--chart-4)" },
          { key: "run", label: "Inference", color: "var(--chart-1)" },
        ],
        values: months.map(({ totals }) => [totals.build_cost, totals.inference_cost]),
      };
    }
    return {
      series: [
        // Cached input is a SUBSET of tokens_in, so it is taken out of the
        // input series before the two are stacked — otherwise every cached
        // token would be counted twice.
        { key: "input", label: "Uncached input", color: "var(--chart-1)" },
        { key: "cached", label: "Cached input", color: "var(--chart-2)" },
        { key: "output", label: "Output", color: "var(--chart-3)" },
      ],
      values: months.map(({ totals }) => [
        Math.max(0, totals.tokens_in - totals.cached_tokens_in),
        totals.cached_tokens_in,
        totals.tokens_out,
      ]),
    };
  }

  // Split by vendor. Rank across the whole range, and over every vendor even
  // when the chart is narrowed to one, so a vendor keeps its colour and its
  // place in the stack from one month — and one filter — to the next.
  const totals = new Map<string, number>();
  for (const month of trend) {
    for (const row of month.by_provider ?? []) {
      totals.set(row.provider, (totals.get(row.provider) ?? 0) + rowValue(row, measure));
    }
  }
  const ranked = [...totals.entries()]
    .filter(([, total]) => total > 0)
    .sort((a, b) => b[1] - a[1])
    .map(([name]) => name);
  // One named vendor fewer when there is a remainder, so the legend never
  // holds more than NAMED_PROVIDERS entries with "Other" counted among them.
  const named = ranked.length > NAMED_PROVIDERS ? ranked.slice(0, NAMED_PROVIDERS - 1) : ranked;
  const pooled = ranked.length > named.length;
  const colorOf = (name: string) => {
    const i = named.indexOf(name);
    return i >= 0 ? PALETTE[i % PALETTE.length] : OTHER_COLOR;
  };

  if (provider !== null) {
    // Narrowed to one vendor: one series, named even if it would otherwise
    // have been pooled, in the colour it has in the full chart.
    return {
      series: [{ key: provider, label: providerLabel(provider), color: colorOf(provider) }],
      values: months.map(({ rows }) => [
        rows.reduce((sum, row) => sum + rowValue(row, measure), 0),
      ]),
    };
  }

  const series: TrendSeries[] = named.map((name) => ({
    key: name,
    label: providerLabel(name),
    color: colorOf(name),
  }));
  if (pooled) {
    series.push({
      key: OTHER_KEY,
      label: `Other (${ranked.length - named.length})`,
      color: OTHER_COLOR,
    });
  }
  const index = new Map(named.map((name, i) => [name, i]));
  return {
    series,
    values: months.map(({ rows }) => {
      const out = series.map(() => 0);
      for (const row of rows) {
        const i = index.get(row.provider) ?? (pooled ? series.length - 1 : -1);
        if (i >= 0) out[i] += rowValue(row, measure);
      }
      return out;
    }),
  };
}

/** The visible part of each month: what the bars stack and the axis scales to. */
export function visibleTotal(
  row: number[],
  series: TrendSeries[],
  hidden: ReadonlySet<string>,
): number {
  return row.reduce((sum, value, i) => (hidden.has(series[i].key) ? sum : sum + value), 0);
}

/** For a provider filter, the vendors in the window ranked as the provider list ranks them. */
export function providerOptions(trend: TrendMonth[]): string[] {
  const totals = new Map<string, number>();
  for (const month of trend) {
    for (const row of month.by_provider ?? []) {
      totals.set(
        row.provider,
        (totals.get(row.provider) ?? 0) + row.build_cost + row.inference_cost,
      );
    }
  }
  return [...totals.entries()].sort((a, b) => b[1] - a[1]).map(([name]) => name);
}

/** A set with one key flipped — the toggle every legend here needs. */
export function toggled(set: ReadonlySet<string>, key: string): Set<string> {
  const next = new Set(set);
  if (next.has(key)) next.delete(key);
  else next.add(key);
  return next;
}
