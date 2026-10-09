import { describe, expect, it } from "vitest";
import { OTHER_COLOR, OTHER_KEY, seriesFromSplits, visibleSum, type Part } from "./chartSeries";

const p = (key: string, amount: number): Part => ({ key, label: key.toUpperCase(), amount });

describe("seriesFromSplits", () => {
  it("names parts largest first across the whole range, one value per period", () => {
    const series = seriesFromSplits([[p("a", 10), p("b", 1)], [p("b", 30)]]);
    expect(series.map((s) => [s.key, s.label, s.values])).toEqual([
      ["b", "B", [1, 30]],
      ["a", "A", [10, 0]],
    ]);
    expect(series[0].color).not.toBe(series[1].color);
  });

  it("pools the tail into Other, so every month still adds up to its bar", () => {
    const split = [[p("a", 9), p("b", 8), p("c", 7), p("d", 6)]];
    const series = seriesFromSplits(split, { limit: 3 });
    expect(series.map((s) => s.key)).toEqual(["a", "b", OTHER_KEY]);
    expect(series[2]).toMatchObject({ label: "Other (2)", color: OTHER_COLOR, muted: true });
    expect(series[2].values).toEqual([13]);
    expect(visibleSum(series, 0, new Set())).toBe(30);
  });

  it("leaves a part hidden elsewhere out of Other too", () => {
    const split = [[p("a", 9), p("b", 8), p("c", 7), p("d", 6)]];
    const series = seriesFromSplits(split, { limit: 3, hidden: new Set(["d"]) });
    expect(series[2].values).toEqual([7]); // c only
  });

  it("draws a residual grey, without spending a colour on it", () => {
    const series = seriesFromSplits([[p("Unattributed", 50), p("dev", 10)]], {
      residualKeys: ["Unattributed"],
    });
    expect(series[0]).toMatchObject({ key: "Unattributed", color: OTHER_COLOR, muted: true });
    expect(series[1].color).toBe("var(--chart-1)");
  });

  it("drops parts that never had anything in them", () => {
    expect(seriesFromSplits([[p("a", 0)]])).toEqual([]);
  });
});

describe("visibleSum", () => {
  it("adds only what is still shown", () => {
    const series = seriesFromSplits([[p("a", 5), p("b", 3)]]);
    expect(visibleSum(series, 0, new Set(["a"]))).toBe(3);
  });
});
