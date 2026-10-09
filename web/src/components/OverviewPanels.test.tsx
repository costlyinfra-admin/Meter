import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Dashboard } from "../api";
import { KpiRow } from "./OverviewPanels";

const DATA = {
  trend: [],
  unattributed: { build_cost: 0, inference_cost: 0 },
  totals: {
    build_cost: 200,
    inference_cost: 800,
    estimated_inference: 0,
    prev_build_cost: 0,
    prev_inference_cost: 0,
    tokens_in: 1000,
    tokens_out: 500,
  },
} as unknown as Dashboard;

function kpis(data: Dashboard) {
  return (
    <MemoryRouter>
      <KpiRow
        data={data}
        savings={null}
        savingsFailed={false}
        deltaLabel="vs last month"
        onShowSource={() => {}}
      />
    </MemoryRouter>
  );
}

describe("KpiRow figures", () => {
  it("arrive anew when they change, so the new figure fades in — and only then", () => {
    const { rerender } = render(kpis(DATA));
    const first = screen.getByText("$1,000");

    // Same figures, re-rendered (a background refresh that changed nothing):
    // the very same element, so nothing replays.
    rerender(kpis({ ...DATA, totals: { ...DATA.totals } }));
    expect(screen.getByText("$1,000")).toBe(first);

    // A different period: a new element, which the stylesheet fades in.
    rerender(kpis({ ...DATA, totals: { ...DATA.totals, inference_cost: 1800 } }));
    const second = screen.getByText("$2,000");
    expect(second).not.toBe(first);
    expect(second).toHaveClass("kpi-value");
  });
});

describe("the full-coverage milestone", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => vi.unstubAllGlobals());

  const bar = (container: HTMLElement) => container.querySelector(".kpi-bar")!;

  it("marks the first time every dollar is attributed, once per viewer", () => {
    const first = render(kpis(DATA)).container; // nothing unattributed
    expect(bar(first)).toHaveClass("sheen");
    first.remove();

    // The next time, it is just a full bar.
    const again = render(kpis(DATA)).container;
    expect(bar(again)).not.toHaveClass("sheen");
  });

  it("waits for coverage to be complete", () => {
    const partial = render(
      kpis({ ...DATA, unattributed: { build_cost: 0, inference_cost: 50 } } as Dashboard),
    ).container;
    expect(bar(partial)).not.toHaveClass("sheen");
    expect(localStorage.length).toBe(0);
  });

  it("skips the moment, not the bar, where the browser keeps nothing", () => {
    vi.stubGlobal("localStorage", {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    });
    const blocked = render(kpis(DATA)).container;
    expect(bar(blocked)).not.toHaveClass("sheen");
    expect(screen.getByText("Every dollar is tied to a feature")).toBeInTheDocument();
  });
});
