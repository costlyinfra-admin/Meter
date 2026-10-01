import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, type Forecast } from "../api";
import { ForecastPage } from "./ForecastPage";

vi.mock("../api", async (importActual) => {
  const actual = await importActual<typeof import("../api")>();
  return { ...actual, api: { forecast: vi.fn() } };
});

function base(): Forecast {
  return {
    as_of: "2026-05-21",
    as_of_is_fixed: false,
    currency: "USD",
    has_budget: true,
    open_month: {
      month: "2026-05-01",
      days_in_month: 31,
      observed_days: 21,
      inference: {
        actual: 21000,
        projected: 31000,
        low: 30000,
        high: 32000,
        method: "recent_weighted",
        confidence: "high",
      },
      build: { actual: 5000 },
      total_projected: 36000,
      budget: 17000,
      projected_budget_pct: 211.8,
    },
    horizon: { status: "ok", history_months: 6, carried_open_month: true, build_status: "ok" },
    history: [
      { month: "2026-03-01", inference: 7000, build: 800 },
      { month: "2026-04-01", inference: 8000, build: 900 },
    ],
    months: [
      {
        month: "2026-06-01",
        inference: { projected: 22000, low: 12000, high: 32000 },
        build: { projected: 950, low: 900, high: 1000 },
        total_projected: 22950,
        budget: 16400,
        projected_budget_pct: 139.9,
      },
    ],
    drivers: [
      {
        feature_id: "f-triage",
        name: "Threat triage",
        prior_month: 3500,
        projected: 12000,
        confidence: "high",
        change: 8500,
        change_pct: 242.9,
        identified_savings: 4348,
      },
      {
        feature_id: null,
        name: "Unattributed",
        prior_month: 0,
        projected: 1800,
        confidence: "medium",
        change: 1800,
        change_pct: null,
        identified_savings: null,
      },
    ],
  };
}

function renderPage() {
  return render(
    <MemoryRouter>
      <ForecastPage />
    </MemoryRouter>,
  );
}

describe("Forecast", () => {
  beforeEach(() => {
    vi.mocked(api.forecast).mockReset();
    vi.mocked(api.forecast).mockResolvedValue(base());
  });

  it("shows this month and the next ones, inference and build apart", async () => {
    renderPage();
    const card = (await screen.findByText(/May \(this month\)/)).closest(".forecast-card");
    const text = card!.textContent ?? "";
    // Two separate lines, never merged (invariant 2)...
    expect(text).toContain("Inference");
    expect(text).toContain("$31,000");
    expect(text).toContain("Build (to date)");
    expect(text).toContain("$5,000");
    // ...and the one combined figure is labelled as what it is.
    expect(text).toContain("Total vs budget");
    // June appears as a card of its own (and again on the chart axis).
    const labels = [...document.querySelectorAll(".forecast-card-month")].map((e) => e.textContent);
    expect(labels).toEqual(["May (this month)", "Jun"]);
  });

  it("warns before an overspend and offers the alert that catches it next time", async () => {
    renderPage();
    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent("212% of budget");
    expect(status).toHaveTextContent("That is over.");
    // The existing budget alert fires on ACTUAL spend, after the fact. This one
    // is the point of the page.
    expect(
      within(status).getByRole("link", { name: "Alert me before this happens" }),
    ).toHaveAttribute("href", "/alerts/new?metric=combined_cost&condition=forecast_budget_pct");
  });

  it("asks for a budget rather than inventing one", async () => {
    const f = base();
    vi.mocked(api.forecast).mockResolvedValue({
      ...f,
      has_budget: false,
      open_month: { ...f.open_month, budget: null, projected_budget_pct: null },
      months: f.months.map((m) => ({ ...m, budget: null, projected_budget_pct: null })),
    });
    renderPage();
    expect(await screen.findByText(/Set a budget in/)).toBeInTheDocument();
    expect(screen.queryByText("Alert me before this happens")).not.toBeInTheDocument();
    expect(screen.queryByText("Total vs budget")).not.toBeInTheDocument();
  });

  it("lists drivers biggest-increase first and keeps Unattributed as a plain row", async () => {
    renderPage();
    const heading = await screen.findByRole("heading", { name: "What is driving it" });
    const rows = heading.closest("section")!.querySelectorAll("tbody tr");
    expect([...rows].map((r) => r.querySelector("td")?.textContent)).toEqual([
      "Threat triage",
      "Unattributed",
    ]);
    // A real feature links to its page; Unattributed is not a feature to open.
    expect(
      within(rows[0] as HTMLElement).getByRole("link", { name: "Threat triage" }),
    ).toHaveAttribute("href", "/features/f-triage");
    expect(within(rows[1] as HTMLElement).queryByRole("link")).not.toBeInTheDocument();
    // The savings Optimize already found, one click from the overrun.
    expect(within(rows[0] as HTMLElement).getByText("$4,348/mo")).toBeInTheDocument();
  });

  it("explains a missing horizon instead of showing blanks", async () => {
    vi.mocked(api.forecast).mockResolvedValue({
      ...base(),
      horizon: {
        status: "insufficient",
        history_months: 2,
        carried_open_month: false,
        build_status: "insufficient",
      },
      months: [
        {
          month: "2026-06-01",
          inference: { projected: null, low: null, high: null },
          build: { projected: null, low: null, high: null },
          total_projected: null,
          budget: 16400,
          projected_budget_pct: null,
        },
      ],
    });
    renderPage();
    expect(
      await screen.findByText(
        /at least three full months of spend to draw a trend through, and there are 2/,
      ),
    ).toBeInTheDocument();
  });

  it("says why there is no range, rather than drawing one from nothing", async () => {
    const f = base();
    f.open_month.inference = {
      ...f.open_month.inference,
      low: null,
      high: null,
      method: "month_to_date_prorata",
      confidence: "low",
    };
    vi.mocked(api.forecast).mockResolvedValue(f);
    renderPage();
    expect(
      await screen.findByText(/There is no range: that needs daily figures/),
    ).toBeInTheDocument();
  });

  it("describes a mixed month in words, one clause per source", async () => {
    const f = base();
    f.open_month.inference.method = "recent_weighted+month_to_date_prorata+monthly_allocation";
    vi.mocked(api.forecast).mockResolvedValue(f);
    renderPage();
    const section = (
      await screen.findByRole("heading", { name: "How this is worked out" })
    ).closest("section")!;
    const text = section.textContent ?? "";
    expect(text).toContain("self-hosted capacity, already booked for the whole month");
    expect(text).toContain("carried forward at its average so far");
    // No internal method name reaches the reader.
    expect(text).not.toMatch(/monthly_allocation|month_to_date_prorata|recent_weighted/);
  });

  it("is drawn as the Overview draws its spend trend, hover card included", async () => {
    renderPage();
    const svg = await screen.findByRole("img", { name: /Monthly AI spend/ });
    // The Overview's bar classes, so the colours are the Overview's colours.
    // Every bar, not just some: the open month's "so far" segment is excluded
    // because it carries the class too and would hide a bar that lost it.
    const bars = svg.querySelectorAll(".forecast-bar").length;
    expect(bars).toBe(4);
    expect(svg.querySelectorAll(".trend-bar-run:not(.forecast-seg-sofar)")).toHaveLength(bars);
    expect(svg.querySelectorAll(".trend-bar-build")).toHaveLength(bars);
    // Two months of history, so the open month is the third hover band.
    const bands = svg.querySelectorAll('rect[fill="transparent"]');
    fireEvent.mouseEnter(bands[2]);
    const card = document.querySelector(".trend-hover-card")!;
    expect(card).toHaveTextContent("May · this month");
    expect(card).toHaveTextContent("Inference so far$21,000");
    expect(card).toHaveTextContent("Inference projected$31,000");
    // The range is the month's total, build included, and says so.
    expect(card).toHaveTextContent("Total, 80% range$35,000 – $37,000");
  });

  describe("on a narrow screen", () => {
    afterEach(() => vi.restoreAllMocks());

    it("measures itself and shortens the months to their initials", async () => {
      vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
        width: 160,
      } as DOMRect);
      renderPage();
      const svg = await screen.findByRole("img", { name: /Monthly AI spend/ });
      expect(svg.getAttribute("viewBox")).toBe("0 0 160 240");
      const months = [...svg.querySelectorAll(".forecast-bar .trend-axis-label")].map(
        (t) => t.textContent,
      );
      expect(months).toEqual(["M", "A", "M", "J"]);
    });
  });

  it("names the months in full when there is room", async () => {
    renderPage();
    const svg = await screen.findByRole("img", { name: /Monthly AI spend/ });
    const months = [...svg.querySelectorAll(".forecast-bar .trend-axis-label")].map(
      (t) => t.textContent,
    );
    expect(months).toEqual(["Mar", "Apr", "May", "Jun"]);
  });

  it("reports a failure instead of an empty page", async () => {
    vi.mocked(api.forecast).mockRejectedValue(new Error("down"));
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent(/Could not load the forecast/);
  });
});
