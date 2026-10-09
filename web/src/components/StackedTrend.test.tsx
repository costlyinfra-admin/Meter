import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { StackSeries } from "../chartSeries";
import { SpendBars } from "./SpendBars";
import { StackedTrend } from "./StackedTrend";

const PERIODS = ["2026-04-01", "2026-05-01"];
const SERIES: StackSeries[] = [
  { key: "a", label: "Alpha", color: "var(--chart-1)", values: [100, 200] },
  { key: "b", label: "Beta", color: "var(--chart-2)", values: [4000, 0] },
];

function axisTop(): string | null {
  return [...document.querySelectorAll(".trend-axis-label")].map((e) => e.textContent).pop()!;
}

describe("StackedTrend", () => {
  it("hides a series from its legend and rescales to what is left", () => {
    render(
      <StackedTrend periods={PERIODS} series={SERIES} ariaLabel="Spend" legendLabel="Legend" />,
    );
    const legend = screen.getByRole("group", { name: "Legend" });
    expect(document.querySelectorAll('[data-series="b"]')).toHaveLength(1);
    expect(axisTop()).toBe("$5,000");

    fireEvent.click(within(legend).getByRole("button", { name: /^Beta/ }));
    expect(within(legend).getByRole("button", { name: /^Beta/ })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    expect(document.querySelectorAll('[data-series="b"]')).toHaveLength(0);
    expect(document.querySelectorAll('[data-series="a"]')).toHaveLength(2);
    expect(axisTop()).toBe("$200");

    // The hover card lists only what is shown, and totals only that.
    fireEvent.mouseEnter(screen.getByRole("img").querySelectorAll('rect[fill="transparent"]')[0]);
    const card = document.querySelector(".trend-hover-card")!;
    expect(card.textContent).toContain("Alpha");
    expect(card.textContent).not.toContain("Beta");
    expect(card.querySelector(".trend-hover-total")!.textContent).toBe("$100");

    fireEvent.click(within(legend).getByRole("button", { name: /^Alpha/ }));
    expect(screen.getByText(/Every series is hidden/)).toBeInTheDocument();
    fireEvent.click(within(legend).getByRole("button", { name: "Show all" }));
    expect(document.querySelectorAll('[data-series="b"]')).toHaveLength(1);
  });

  it("previews a series from its legend: the others fade until the pointer leaves", () => {
    render(
      <StackedTrend periods={PERIODS} series={SERIES} ariaLabel="Spend" legendLabel="Legend" />,
    );
    const beta = screen.getByRole("button", { name: /^Beta/ });
    fireEvent.mouseEnter(beta);
    for (const seg of document.querySelectorAll('[data-series="a"]')) {
      expect(seg).toHaveClass("seg-dim");
    }
    expect(document.querySelector('[data-series="b"]')).not.toHaveClass("seg-dim");
    fireEvent.mouseLeave(beta);
    expect(document.querySelectorAll(".seg-dim")).toHaveLength(0);

    // A hidden series has nothing to preview.
    fireEvent.click(beta);
    fireEvent.mouseEnter(beta);
    expect(document.querySelectorAll(".seg-dim")).toHaveLength(0);
  });

  it("says so when there is nothing to draw", () => {
    render(
      <StackedTrend
        periods={PERIODS}
        series={[{ ...SERIES[0], values: [0, 0] }]}
        ariaLabel="Spend"
        legendLabel="Legend"
        emptyText="No build cost in this period."
      />,
    );
    expect(screen.getByText("No build cost in this period.")).toBeInTheDocument();
  });

  it("can share what is hidden with the list beside it", () => {
    const onHiddenChange = vi.fn();
    render(
      <StackedTrend
        periods={PERIODS}
        series={SERIES}
        ariaLabel="Spend"
        legendLabel="Legend"
        hidden={new Set(["a"])}
        onHiddenChange={onHiddenChange}
      />,
    );
    expect(document.querySelectorAll('[data-series="a"]')).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: /^Beta/ }));
    expect(onHiddenChange).toHaveBeenCalledWith(new Set(["a", "b"]));
  });
});

describe("SpendBars", () => {
  const ROWS = [
    { label: "anthropic", amount: 600, pct: 40 },
    { label: "openai", amount: 300, pct: 20 },
    { label: "groq", amount: 100, pct: 6.67 },
  ];

  it("leaves a row out on click, and shares the rest over what is shown", () => {
    render(<SpendBars rows={ROWS} />);
    // Nothing left out: the server's share stands (it may be of a wider total).
    expect(screen.getByText("$300 · 20%")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /anthropic/ }));
    expect(screen.getByRole("button", { name: /anthropic/ })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    expect(screen.getByRole("button", { name: /anthropic/ }).closest("li")).toHaveClass("off");
    // 300 of the 400 still shown.
    expect(screen.getByText("$300 · 75%")).toBeInTheDocument();
    expect(screen.getByText(/1 left out · shares are of the rest/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Show all" }));
    expect(screen.getByText("$300 · 20%")).toBeInTheDocument();
    expect(screen.queryByText(/left out/)).toBeNull();
  });

  it("fades the other rows while one is pointed at, and can share that with a chart", () => {
    const onPreviewChange = vi.fn();
    const { rerender } = render(<SpendBars rows={ROWS} />);
    fireEvent.mouseEnter(screen.getByRole("button", { name: /openai/ }));
    expect(screen.getByRole("button", { name: /anthropic/ }).closest("li")).toHaveClass(
      "previewed-out",
    );
    expect(screen.getByRole("button", { name: /openai/ }).closest("li")).not.toHaveClass(
      "previewed-out",
    );
    fireEvent.mouseLeave(screen.getByRole("button", { name: /openai/ }));
    expect(document.querySelectorAll(".previewed-out")).toHaveLength(0);

    // Shared: the chart beside it says which row to light up, and hears back.
    rerender(<SpendBars rows={ROWS} preview="groq" onPreviewChange={onPreviewChange} />);
    expect(document.querySelectorAll(".previewed-out")).toHaveLength(2);
    fireEvent.mouseEnter(screen.getByRole("button", { name: /anthropic/ }));
    expect(onPreviewChange).toHaveBeenCalledWith("anthropic");
  });

  it("hides sub-rows with their row", () => {
    render(
      <SpendBars
        rows={[{ ...ROWS[0], models: [{ label: "claude-sonnet", amount: 600, pct: 100 }] }]}
      />,
    );
    expect(screen.getByText("claude-sonnet")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /anthropic/ }));
    expect(screen.queryByText("claude-sonnet")).toBeNull();
  });

  it("prints no share where a share means nothing", () => {
    render(<SpendBars rows={ROWS} showShare={false} />);
    expect(screen.getByText("$300")).toBeInTheDocument();
    expect(screen.queryByText(/%/)).toBeNull();
  });
});
