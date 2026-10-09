import { describe, expect, it } from "vitest";
import type { TrendMonth, TrendProvider } from "./api";
import {
  buildTrendView,
  hasProviderSplit,
  NAMED_PROVIDERS,
  OTHER_KEY,
  providerOptions,
  toggled,
  visibleTotal,
} from "./spendTrend";

function vendor(provider: string, over: Partial<TrendProvider> = {}): TrendProvider {
  return {
    provider,
    build_cost: 0,
    inference_cost: 0,
    tokens_in: 0,
    cached_tokens_in: 0,
    tokens_out: 0,
    ...over,
  };
}

function month(period: string, rows: TrendProvider[]): TrendMonth {
  const sum = (k: keyof Omit<TrendProvider, "provider">) => rows.reduce((a, r) => a + r[k], 0);
  return {
    period,
    build_cost: sum("build_cost"),
    inference_cost: sum("inference_cost"),
    tokens_in: sum("tokens_in"),
    cached_tokens_in: sum("cached_tokens_in"),
    tokens_out: sum("tokens_out"),
    cache_rate: 0,
    by_provider: rows,
  };
}

const TREND = [
  month("2026-04-01", [
    vendor("anthropic", {
      inference_cost: 300,
      tokens_in: 1000,
      cached_tokens_in: 400,
      tokens_out: 50,
    }),
    vendor("cursor", { build_cost: 40 }),
  ]),
  month("2026-05-01", [
    vendor("openai", { inference_cost: 500, tokens_in: 900, tokens_out: 100 }),
    vendor("anthropic", { inference_cost: 100, tokens_in: 200, tokens_out: 20 }),
    vendor("cursor", { build_cost: 60 }),
  ]),
];

describe("buildTrendView", () => {
  it("keeps build and inference as two series, build at the bottom", () => {
    const view = buildTrendView(TREND, "cost", "kind", null);
    expect(view.series.map((s) => s.label)).toEqual(["Build", "Inference"]);
    expect(view.values).toEqual([
      [40, 300],
      [60, 600],
    ]);
  });

  it("takes cached input out of input so no token is stacked twice", () => {
    const view = buildTrendView(TREND, "tokens", "kind", null);
    expect(view.series.map((s) => s.key)).toEqual(["input", "cached", "output"]);
    // April: 1000 in, 400 of them cached, 50 out -> 600 + 400 + 50 = 1050.
    expect(view.values[0]).toEqual([600, 400, 50]);
    expect(view.values[0].reduce((a, b) => a + b, 0)).toBe(1000 + 50);
  });

  it("splits by vendor ranked over the whole range, each keeping its place", () => {
    const view = buildTrendView(TREND, "cost", "provider", null);
    // anthropic 400, openai 500, cursor 100 across both months.
    expect(view.series.map((s) => s.key)).toEqual(["openai", "anthropic", "cursor"]);
    expect(view.values).toEqual([
      [0, 300, 40],
      [500, 100, 60],
    ]);
    // A month's vendors add up to the same bar the build/inference view draws.
    const kind = buildTrendView(TREND, "cost", "kind", null);
    view.values.forEach((row, i) =>
      expect(row.reduce((a, b) => a + b, 0)).toBe(kind.values[i].reduce((a, b) => a + b, 0)),
    );
  });

  it("leaves a build tool out of a token split, having no tokens to show", () => {
    const view = buildTrendView(TREND, "tokens", "provider", null);
    expect(view.series.map((s) => s.key)).toEqual(["anthropic", "openai"]);
  });

  it("pools the long tail into Other rather than running out of colours", () => {
    const many = [
      month(
        "2026-05-01",
        Array.from({ length: NAMED_PROVIDERS + 2 }, (_, i) =>
          vendor(`v${i}`, { inference_cost: 100 - i }),
        ),
      ),
    ];
    const view = buildTrendView(many, "cost", "provider", null);
    expect(view.series).toHaveLength(NAMED_PROVIDERS);
    const other = view.series[view.series.length - 1];
    expect(other.key).toBe(OTHER_KEY);
    expect(other.label).toBe("Other (3)");
    // Nothing is dropped: Other carries the three smallest.
    expect(view.values[0][view.series.length - 1]).toBe(95 + 94 + 93);
    expect(view.values[0].reduce((a, b) => a + b, 0)).toBe(many[0].inference_cost);
    // Every named vendor gets a different colour.
    expect(new Set(view.series.map((s) => s.color)).size).toBe(view.series.length);
  });

  it("narrows every view to one vendor's share", () => {
    const kind = buildTrendView(TREND, "cost", "kind", "anthropic");
    expect(kind.values).toEqual([
      [0, 300],
      [0, 100],
    ]);
    const tokens = buildTrendView(TREND, "tokens", "kind", "openai");
    // Absent in April: zeros, not a gap in the month list.
    expect(tokens.values).toEqual([
      [0, 0, 0],
      [900, 0, 100],
    ]);
    const split = buildTrendView(TREND, "cost", "provider", "cursor");
    expect(split.series.map((s) => s.key)).toEqual(["cursor"]);
    expect(split.values).toEqual([[40], [60]]);
    // In the colour it has in the full chart, not the first colour going.
    const full = buildTrendView(TREND, "cost", "provider", null);
    expect(split.series[0].color).toBe(full.series.find((s) => s.key === "cursor")!.color);
    expect(split.series[0].color).not.toBe(full.series[0].color);
  });
});

describe("visibleTotal", () => {
  it("adds only the series still switched on", () => {
    const view = buildTrendView(TREND, "cost", "kind", null);
    expect(visibleTotal(view.values[1], view.series, new Set())).toBe(660);
    expect(visibleTotal(view.values[1], view.series, new Set(["run"]))).toBe(60);
    expect(visibleTotal(view.values[1], view.series, new Set(["run", "build"]))).toBe(0);
  });
});

describe("providerOptions and hasProviderSplit", () => {
  it("lists vendors the way the provider list ranks them", () => {
    expect(providerOptions(TREND)).toEqual(["openai", "anthropic", "cursor"]);
  });

  it("offers no vendor controls when an older server sent no split", () => {
    const bare = TREND.map((m) => ({ ...m, by_provider: undefined }));
    expect(hasProviderSplit(bare)).toBe(false);
    expect(hasProviderSplit(TREND)).toBe(true);
  });
});

describe("toggled", () => {
  it("flips one key and leaves the original alone", () => {
    const before = new Set(["a"]);
    expect([...toggled(before, "b")].sort()).toEqual(["a", "b"]);
    expect([...toggled(before, "a")]).toEqual([]);
    expect([...before]).toEqual(["a"]);
  });
});
