import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
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
