import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, type BudgetForecast, type ProviderSpend } from "../api";
import { AuthProvider } from "../auth/AuthContext";
import { Dashboard } from "./Dashboard";

vi.mock("../api", async (importActual) => {
  const actual = await importActual<typeof import("../api")>();
  return {
    ...actual,
    api: {
      copilotOverview: vi.fn(),
      me: vi.fn(),
      dashboard: vi.fn(),
      providerSpend: vi.fn(),
      budgetForecast: vi.fn(),
      discoveryScope: vi.fn(),
      runDiscovery: vi.fn(),
      customerSpend: vi.fn(),
      refreshInference: vi.fn(),
    },
  };
});

const TRIAGE = {
  feature_id: "f1",
  name: "AI threat triage",
  category: "api",
  category_source: "discovery",
  build_cost: 181,
  inference_cost: 4200,
  active_users: 540,
  cost_per_user: 7.77,
  requests: 320000,
  worth_it: "healthy",
  confidence: "med",
};

/** An empty ProviderSpend — spread it and override only what a test cares about.
 *  Typed, so a field added to the payload shows up here rather than silently
 *  widening every literal in it. */
const EMPTY_SPEND: ProviderSpend = {
  start: "2026-05-01",
  end: "2026-05-01",
  total: 0,
  by_provider: [],
  trend: [],
  daily_trend: [],
  build_total: 0,
  build_by_tool: [],
  developer_activity: [],
  activity_coverage: {
    github_connected: true,
    dated_prs: 12,
    undated_prs: 0,
    first_merged: "2026-04-02",
    last_merged: "2026-05-21",
    runs: 1,
    covered_from: "2026-04-01",
    covered_to: "2026-05-21",
    last_run_at: "2026-05-21T09:00:00Z",
    last_run_status: "success",
    last_run_trigger: "manual",
  },
  build_by_developer: [],
  build_trend: [],
  customer_total: 0,
  by_customer: [],
  token_total: 0,
  by_token_type: [],
  workspace_total: 0,
  by_workspace: [],
};

const DATA = {
  period: "2026-05-01",
  start: "2026-05-01",
  end: "2026-05-01",
  months: 1,
  features: [TRIAGE],
  unattributed: { build_cost: 30, inference_cost: 760 },
  highlights: { most_expensive: TRIAGE, optimization: null, highest_cost_per_user: TRIAGE },
  insights: [
    {
      kind: "spike",
      text: "May 9 was the costliest day at $300 — 15x the $20.00 median day this period.",
      detail: "",
    },
    {
      kind: "concentration",
      text: "AI threat triage represents 54% of all AI spend ($4,381).",
      detail: "",
    },
    {
      kind: "governance",
      text: "Unattributed spend represents 9.7% of total AI costs ($790).",
      detail: "",
    },
  ],
  actions: [
    {
      kind: "unattributed",
      title: "Resolve $790 unattributed spend",
      detail: "9.7% of AI spend is not tied to a feature.",
      href: "/cost-sources",
      tone: "warn" as const,
    },
  ],
  trend: [
    {
      period: "2026-04-01",
      build_cost: 100,
      inference_cost: 2000,
      tokens_in: 500_000,
      cached_tokens_in: 50_000,
      tokens_out: 120_000,
      cache_rate: 10,
    },
    {
      period: "2026-05-01",
      build_cost: 211,
      inference_cost: 4960,
      tokens_in: 1_200_000,
      cached_tokens_in: 180_000,
      tokens_out: 300_000,
      cache_rate: 15,
    },
  ],
  providers: [
    { provider: "anthropic", build_cost: 0, inference_cost: 4000, amount: 4000, share: 77.4 },
    { provider: "copilot", build_cost: 211, inference_cost: 0, amount: 211, share: 4.1 },
  ],
  // When cost was last INGESTED (not when the page loaded).
  data_updated_at: "2026-08-20T16:07:00Z",
  inference_updated_at: "2026-08-20T16:07:00Z",
  build_updated_at: "2026-08-19T09:00:00Z",
  totals: {
    build_cost: 211,
    inference_cost: 4960,
    estimated_inference: 0,
    prev_build_cost: 180,
    prev_inference_cost: 5200,
    tokens_in: 1_200_000,
    tokens_out: 300_000,
  },
};

function renderDashboard() {
  return render(
    <MemoryRouter>
      <AuthProvider>
        <Dashboard />
      </AuthProvider>
    </MemoryRouter>,
  );
}

/**
 * Everything on the Budget & forecast card is computed on the server, so the
 * fixture is a server response — the page's job is to choose a state and draw.
 * These figures match DATA.trend: 2,100 in April and 5,171 in May.
 */
const FORECAST: BudgetForecast = {
  status: "open",
  as_of: "2026-05-21",
  as_of_is_fixed: false,
  window_start: "2026-04-01",
  window_end: "2026-05-31",
  actual: 7271,
  actual_build: 311,
  actual_inference: 6960,
  budget: 12000,
  budget_detail: {
    amount: 12000,
    method: "monthly",
    covered_days: 61,
    window_days: 61,
    covered_start: "2026-04-01",
    covered_end: "2026-05-31",
    fully_covered: true,
  },
  budget_cadence: "monthly",
  currency: "USD",
  forecast: 13500,
  forecast_optimized: 11660,
  identified_savings: 1840,
  variance: 1500,
  variance_pct: 12.5,
  method: "recent_weighted",
  confidence: "high",
  observed_days: 21,
};

describe("Dashboard (Overview)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.budgetForecast).mockResolvedValue({ ...FORECAST });
    // The activity empty state asks for the discovery scope; default to none,
    // which is the state where it points at Features rather than offering a run.
    vi.mocked(api.discoveryScope).mockResolvedValue({ owner: null, repos: [] });
    vi.mocked(api.me).mockResolvedValue({
      id: "u1",
      tenant_id: "t1",
      email: "cto@acme.com",
      org_name: "Transilience AI",
    });
    vi.mocked(api.dashboard).mockResolvedValue(DATA);
    vi.mocked(api.copilotOverview).mockResolvedValue({
      totals: { measured: 1200, modeled_ceiling: 640, directional: 999 },
      verified_monthly_savings: 300,
      verified_annual_savings: 3600,
      by_feature: [
        {
          feature_id: "f1",
          name: "AI threat triage",
          measured: 1200,
          modeled_ceiling: 640,
          directional: 0,
        },
      ],
    } as never);
    vi.mocked(api.refreshInference).mockResolvedValue({
      providers: 1,
      synced: [{ provider: "anthropic", total: 4960 }],
      errors: [],
      total: 4960,
    });
  });

  it("leads with the four headline figures", async () => {
    renderDashboard();
    await screen.findByText("Key insights");

    // Total AI spend is the one place build and inference are added together.
    const spend = screen.getByRole("heading", { name: "Total AI spend" }).closest("article")!;
    expect(within(spend).getByText("$5,171")).toBeInTheDocument(); // 211 + 4960
    // …and the split is still shown, because they answer different questions.
    // Each half is a control now, so the text sits in its own button.
    expect(within(spend).getByRole("button", { name: "$211 build" })).toBeInTheDocument();
    expect(within(spend).getByRole("button", { name: "$4,960 run" })).toBeInTheDocument();

    // Tokens keep their own card and their own split.
    const tokens = screen.getByRole("heading", { name: "Total tokens" }).closest("article")!;
    expect(within(tokens).getByText("1.5M")).toBeInTheDocument();
    expect(within(tokens).getByText(/1\.2M in · 300K out/)).toBeInTheDocument();

    // Coverage is derived from what is unattributed, not asserted separately.
    const coverage = screen
      .getByRole("heading", { name: "Attribution coverage" })
      .closest("article")!;
    expect(within(coverage).getByText("84.7%")).toBeInTheDocument();
    expect(within(coverage).getByText(/\$790 unattributed/)).toBeInTheDocument();
  });

  it("shows savings as unknown until the Optimize engine answers", async () => {
    vi.mocked(api.copilotOverview).mockImplementation(() => new Promise(() => {}));
    renderDashboard();
    await screen.findByText("Key insights");

    // Not zero: an unknown figure and a figure of nothing are different answers.
    const potential = screen
      .getByRole("heading", { name: "Potential savings" })
      .closest("article")!;
    expect(within(potential).getByText("Calculating…")).toBeInTheDocument();
    expect(within(potential).queryByText("$0.00")).not.toBeInTheDocument();
  });

  it("fills the savings cards in once it does", async () => {
    renderDashboard();

    const potential = (await screen.findByRole("heading", { name: "Potential savings" })).closest(
      "article",
    )!;
    expect(await within(potential).findByText("$1,840")).toBeInTheDocument();
    const realized = screen.getByRole("heading", { name: "Savings realized" }).closest("article")!;
    expect(within(realized).getByText("$300")).toBeInTheDocument();
    expect(within(realized).getByText(/\$3,600 annualized/)).toBeInTheDocument();
  });

  it("keeps the Overview standing when Optimize fails", async () => {
    // The savings pair is the only part of this page that depends on another
    // subsystem, so its failure must stay in its own two cards.
    vi.mocked(api.copilotOverview).mockRejectedValue(new ApiError(500, "boom"));
    renderDashboard();

    // The failure lands in its own two cards and nowhere else.
    expect(await screen.findAllByText("Unavailable")).toHaveLength(2);
    expect(screen.getByText("Key insights")).toBeInTheDocument();
    expect(screen.getByText("$5,171")).toBeInTheDocument();
  });

  it("shows which part of the product each feature belongs to", async () => {
    vi.mocked(api.dashboard).mockResolvedValue({
      ...DATA,
      features: [
        TRIAGE,
        { ...TRIAGE, feature_id: "f2", name: "SSO login", category: "auth" },
        // Nobody has tagged this one — it asks for a tag rather than guessing.
        {
          ...TRIAGE,
          feature_id: "f3",
          name: "Misc work",
          category: null,
          category_source: null,
        },
      ],
    });
    renderDashboard();
    await screen.findByText("Key insights");

    // Scope to the By Feature table — the summary tiles name features too.
    const table = screen.getByRole("table");
    const triage = within(table).getByText("AI threat triage").closest("tr")!;
    expect(within(triage).getByText("API")).toBeInTheDocument();
    expect(
      within(within(table).getByText("SSO login").closest("tr")!).getByText("Auth"),
    ).toBeInTheDocument();
    const misc = within(table).getByText("Misc work").closest("tr")!;
    expect(within(misc).getByText("Untagged")).toBeInTheDocument();
    // The basis is on hover, because a keyword guess isn't a human tag.
    expect(within(triage).getByText("API")).toHaveAttribute(
      "title",
      expect.stringContaining("Guessed from keywords"),
    );
  });

  it("opens on the last three months", async () => {
    // One month has no shape: the trend is a lone bar and the only comparison
    // is the delta. Three is the shortest window where the charts say anything.
    renderDashboard();
    await waitFor(() => expect(api.dashboard).toHaveBeenCalledWith({ kind: "last_3_months" }));
    expect(await screen.findByRole("button", { name: /2026/ })).toBeInTheDocument();
  });

  it("draws the spend trend against a dollar axis", async () => {
    renderDashboard();
    await screen.findByText("Spend trend");
    const chart = screen.getByRole("img", { name: /Build and inference cost per month/ });

    // Dotted gridlines with a round ceiling above the tallest month ($5,171).
    expect(chart.querySelectorAll(".trend-grid-line")).toHaveLength(5);
    const labels = [...chart.querySelectorAll(".trend-axis-label")].map((e) => e.textContent);
    expect(labels.slice(0, 5)).toEqual(["$0", "$2,500", "$5,000", "$7,500", "$10,000"]);
    // Build and inference are drawn as separate bars, never one.
    expect(chart.querySelectorAll(".trend-bar-build")).toHaveLength(2);
    expect(chart.querySelectorAll(".trend-bar-run")).toHaveLength(2);
  });

  it("shows the forecast, the budget and the variance the server computed", async () => {
    renderDashboard();
    const panel = (await screen.findByText("Budget & forecast")).closest("section")!;

    // Every figure is the server's. The page rounds for display and nothing else.
    await within(panel).findByText("13% over budget");
    expect(panel.querySelector(".budget-headline")!.textContent).toBe("Forecast: $13.5K");
    expect(within(panel).getByText("$12K")).toBeInTheDocument(); // budget
    expect(within(panel).getByText("$7.3K")).toBeInTheDocument(); // spent so far
    expect(within(panel).getByText("$11.7K")).toBeInTheDocument(); // with savings
    expect(within(panel).getByText("13% over budget")).toBeInTheDocument();

    // Spend to date is solid; the projection is a separate dashed line, never
    // continuous with it and never added into the figure of what was spent.
    expect(panel.querySelector(".budget-actual")).toBeInTheDocument();
    expect(panel.querySelector(".budget-projected.over")).toBeInTheDocument();
    expect(panel.querySelector(".budget-optimized")).toBeInTheDocument();
    expect(panel.querySelector(".budget-line")).toBeInTheDocument();
    expect(
      within(panel).getByText(/would bring forecasted spend within budget/),
    ).toBeInTheDocument();
  });

  it("sends build and run to the half of the breakdown that explains them", async () => {
    vi.mocked(api.providerSpend).mockResolvedValue({
      ...EMPTY_SPEND,
      total: 4960,
      by_provider: [
        { provider: "anthropic", amount: 4960, pct: 100, requests: 10, by_model: [] },
      ],
      build_total: 211,
      build_by_tool: [{ tool: "cursor", amount: 211, pct: 100 }],
    });
    renderDashboard();
    await screen.findByText("Key insights");
    const spend = screen.getByRole("heading", { name: "Total AI spend" }).closest("article")!;

    // "run" opens By Provider on the inference half…
    fireEvent.click(within(spend).getByRole("button", { name: "$4,960 run" }));
    expect(await screen.findByText("Inference (run) cost by provider")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "By Provider" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tab", { name: "Inference cost" })).toHaveAttribute(
      "aria-selected",
      "true",
    );

    // …and "build" opens the other one, which is a different question.
    fireEvent.click(within(spend).getByRole("button", { name: "$211 build" }));
    expect(await screen.findByText("Build cost by tool")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Build cost" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.queryByText("Inference (run) cost by provider")).not.toBeInTheDocument();
  });

  it("names the comparison window once, not twice", async () => {
    renderDashboard();
    await screen.findByText("Key insights");
    // The labels already read "vs prev 3 months"; the card used to prefix
    // another "vs" onto them.
    expect(screen.getByText(/vs prev 3 months/)).toBeInTheDocument();
    expect(screen.queryByText(/vs vs/)).not.toBeInTheDocument();
  });

  it("opens a breakdown card the moment the pointer enters a spend-trend month", async () => {
    renderDashboard();
    await screen.findByText("Spend trend");
    const chart = screen.getByRole("img", { name: /Build and inference cost per month/ });

    // Nothing until the pointer arrives. The card is ours, not a native SVG
    // <title> — those wait about a second and cannot hold a breakdown.
    expect(document.querySelector(".trend-hover-card")).toBeNull();
    expect(chart.querySelector("title")).toBeNull();

    // One invisible band per month, so a short bar is as easy to hit as a tall one.
    const bands = chart.querySelectorAll('rect[fill="transparent"]');
    expect(bands).toHaveLength(2);

    fireEvent.mouseEnter(bands[1]);
    const card = document.querySelector(".trend-hover-card")!;
    expect(card.textContent).toContain("May");
    expect(card.textContent).toContain("Build");
    expect(card.textContent).toContain("$211"); // build
    expect(card.textContent).toContain("$4,960"); // inference
    // Build and inference stay separate in the card, as everywhere else.
    expect(card.textContent).toContain("Inference");
    // The other month dims so the hovered one reads on its own.
    expect(document.querySelectorAll(".trend-bar-group.dim")).toHaveLength(1);

    fireEvent.mouseLeave(chart.parentElement!);
    expect(document.querySelector(".trend-hover-card")).toBeNull();
  });

  it("breaks the budget line down by month, and carries the forecast on the last one", async () => {
    renderDashboard();
    await screen.findByText("Budget & forecast");
    const chart = await screen.findByRole("img", { name: /Cumulative spend against budget/ });
    const bands = chart.querySelectorAll('rect[fill="transparent"]');
    expect(bands).toHaveLength(2);

    // An earlier month: cumulative to there, and what that month itself cost.
    fireEvent.mouseEnter(bands[0]);
    let card = document.querySelector(".trend-hover-card")!;
    expect(card.textContent).toContain("Apr");
    expect(card.textContent).toContain("cumulative");
    expect(card.textContent).toContain("$2,100");
    expect(card.textContent).not.toContain("Forecast at month end");

    // The final month is where the projection lives, so it carries the forecast.
    fireEvent.mouseEnter(bands[1]);
    card = document.querySelector(".trend-hover-card")!;
    expect(card.textContent).toContain("Forecast at month end");
    expect(card.textContent).toContain("$13,500");
    expect(card.textContent).toContain("With savings");
    expect(card.textContent).toContain("Budget");
  });

  it("asks for the forecast on the window the page is showing", async () => {
    renderDashboard();
    await waitFor(() =>
      expect(api.budgetForecast).toHaveBeenCalledWith({ kind: "last_3_months" }),
    );
  });

  it("offers to set a budget rather than inventing one when none exists", async () => {
    vi.mocked(api.budgetForecast).mockResolvedValue({
      ...FORECAST,
      budget: null,
      budget_detail: null,
      budget_cadence: null,
      currency: null,
      variance: null,
      variance_pct: null,
    });
    renderDashboard();
    const panel = (await screen.findByText("Budget & forecast")).closest("section")!;

    expect(await within(panel).findByText("No budget configured")).toBeInTheDocument();
    expect(within(panel).getByRole("link", { name: /Set a budget/ })).toHaveAttribute(
      "href",
      "/settings#budgets",
    );
    // No chart, and above all no number standing in for a budget nobody set.
    expect(panel.querySelector(".budget-line")).not.toBeInTheDocument();
    expect(within(panel).queryByText(/over budget|under budget/)).not.toBeInTheDocument();
  });

  it("reports a closed period as final spend, with no forecast", async () => {
    vi.mocked(api.budgetForecast).mockResolvedValue({
      ...FORECAST,
      status: "closed",
      forecast: 7271,
      forecast_optimized: null,
      variance: -4729,
      variance_pct: -39.41,
      method: "closed",
      confidence: "final",
    });
    renderDashboard();
    const panel = (await screen.findByText("Budget & forecast")).closest("section")!;

    await within(panel).findByText("39% under budget");
    // "Final spend", not "Forecast" — the period is over.
    expect(panel.querySelector(".budget-headline")!.textContent).toBe("Final spend: $7.3K");
    expect(within(panel).getByText("39% under budget")).toBeInTheDocument();
    expect(within(panel).getByText(/no forecast applies/)).toBeInTheDocument();
    // A closed period draws no dashed tail: there is nothing left to project.
    expect(panel.querySelector(".budget-projected")).not.toBeInTheDocument();
    expect(panel.querySelector(".budget-optimized")).not.toBeInTheDocument();
    expect(panel.querySelector(".budget-actual")).toBeInTheDocument();
  });

  it("says the forecast is unavailable rather than projecting from nothing", async () => {
    vi.mocked(api.budgetForecast).mockResolvedValue({
      ...FORECAST,
      status: "insufficient",
      forecast: null,
      forecast_optimized: null,
      variance: null,
      variance_pct: null,
      method: "none",
      confidence: "none",
      observed_days: 0,
    });
    renderDashboard();
    const panel = (await screen.findByText("Budget & forecast")).closest("section")!;

    expect(await within(panel).findByText("Forecast unavailable")).toBeInTheDocument();
    // What IS known still gets said: the budget and the spend are both real.
    expect(within(panel).getByText(/nothing to project from/)).toBeInTheDocument();
    expect(panel.querySelector(".budget-projected")).not.toBeInTheDocument();
  });

  it("shows a loading state, then an error state if the forecast never lands", async () => {
    let reject: (e: Error) => void = () => {};
    vi.mocked(api.budgetForecast).mockReturnValue(
      new Promise((_, r) => {
        reject = r;
      }),
    );
    renderDashboard();
    const panel = (await screen.findByText("Budget & forecast")).closest("section")!;
    expect(within(panel).getByText("Calculating…")).toBeInTheDocument();

    reject(new ApiError(500, "boom"));
    expect(await within(panel).findByText(/unavailable right now/)).toBeInTheDocument();
  });

  it("shows the top three providers, and folds the rest behind a disclosure", async () => {
    const many = [
      { provider: "anthropic", build_cost: 0, inference_cost: 4000, amount: 4000, share: 50 },
      { provider: "openai", build_cost: 0, inference_cost: 2000, amount: 2000, share: 25 },
      { provider: "self_hosted", build_cost: 0, inference_cost: 1000, amount: 1000, share: 12 },
      { provider: "cursor", build_cost: 600, inference_cost: 0, amount: 600, share: 8 },
      { provider: "copilot", build_cost: 400, inference_cost: 0, amount: 400, share: 5 },
    ];
    vi.mocked(api.dashboard).mockResolvedValue({ ...DATA, providers: many });
    renderDashboard();
    const panel = (await screen.findByText("Provider spend")).closest("section")!;

    expect(within(panel).getByText("Anthropic")).toBeInTheDocument();
    expect(within(panel).getByText("Self hosted")).toBeInTheDocument();
    // The remainder is summed on the button, so the panel still accounts for
    // every dollar without listing every vendor.
    const toggle = within(panel).getByRole("button", { name: /2 more · \$1,000 \(13%\)/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(toggle);
    expect(within(panel).getByRole("button", { name: /Show fewer/ })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });

  it("offers no disclosure when there is nothing folded away", async () => {
    renderDashboard();
    const panel = (await screen.findByText("Provider spend")).closest("section")!;
    // The fixture has two providers, which is fewer than the three shown.
    expect(within(panel).queryByRole("button")).not.toBeInTheDocument();
  });

  it("shows one period-over-period delta on total spend", async () => {
    renderDashboard();
    await screen.findByText("Key insights");
    // 5171 against 5380 in the window before: down 3.9%. The label names the
    // window being compared, which is three months by default.
    expect(screen.getByText(/vs prev 3 months/)).toBeInTheDocument();
    expect(screen.getByText(/▼ 3\.9%/)).toBeInTheDocument();
  });

  it("labels the estimated (not-yet-billed) portion of inference cost", async () => {
    vi.mocked(api.dashboard).mockResolvedValue({
      ...DATA,
      totals: { ...DATA.totals, inference_cost: 4960, estimated_inference: 89.42 },
    });
    renderDashboard();
    expect(await screen.findByText(/incl\. ~\$89\.42 estimated/)).toBeInTheDocument();
  });

  it("shows the setup checklist until features + build + inference all exist", async () => {
    // No features, no cost yet -> all three items pending.
    vi.mocked(api.dashboard).mockResolvedValue({
      ...DATA,
      features: [],
      totals: {
        build_cost: 0,
        inference_cost: 0,
        estimated_inference: 0,
        prev_build_cost: 0,
        prev_inference_cost: 0,
        tokens_in: 0,
        tokens_out: 0,
      },
    });
    renderDashboard();

    expect(await screen.findByText("Finish setting up")).toBeInTheDocument();
    expect(screen.getByText("0 of 3 done")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Discover features/ })).toHaveAttribute(
      "href",
      "/features",
    );
    expect(screen.getByRole("link", { name: /Add build cost/ })).toHaveAttribute(
      "href",
      "/cost-sources",
    );
  });

  it("hides the checklist once setup is complete", async () => {
    renderDashboard(); // DATA has features + build + inference cost
    await screen.findByText("Key insights");
    expect(screen.queryByText("Finish setting up")).not.toBeInTheDocument();
  });

  it("switches to the By provider tab and shows provider spend + trend", async () => {
    vi.mocked(api.providerSpend).mockResolvedValue({
      start: "2026-05-01",
      end: "2026-05-01",
      total: 5450,
      by_provider: [
        {
          provider: "openai",
          amount: 4200,
          pct: 77.06,
          requests: 320000,
          by_model: [{ model: "gpt-4o", amount: 4200, pct: 100 }],
        },
        {
          provider: "anthropic",
          amount: 1250,
          pct: 22.94,
          requests: 60000,
          by_model: [{ model: "claude-sonnet-4-6", amount: 1250, pct: 100 }],
        },
      ],
      trend: [
        {
          period: "2026-05-01",
          total: 5450,
          production: 3000,
          development: 1450,
          internal: 500,
          unclassified: 500,
        },
      ],
      daily_trend: [],
      build_total: 270,
      build_by_tool: [
        { tool: "claude_code", amount: 181, pct: 67.04 },
        { tool: "cursor", amount: 89, pct: 32.96 },
      ],
      developer_activity: [],
      activity_coverage: { ...EMPTY_SPEND.activity_coverage },
      build_by_developer: [
        {
          developer_id: "erin",
          label: "Erin (erin)",
          amount: 181,
          pct: 67.04,
          by_tool: [{ tool: "claude_code", amount: 181, pct: 100 }],
        },
        {
          developer_id: "frank",
          label: "Frank (frank)",
          amount: 89,
          pct: 32.96,
          by_tool: [{ tool: "cursor", amount: 89, pct: 100 }],
        },
      ],
      build_trend: [{ period: "2026-05-01", amount: 270 }],
      customer_total: 900,
      by_customer: [{ customer_id: "acme", amount: 900, pct: 100, requests: 1200 }],
      token_total: 5450,
      by_token_type: [
        { token_type: "output", label: "Output", amount: 3000, pct: 55.05, tokens: 300000 },
        { token_type: "input", label: "Input", amount: 2450, pct: 44.95, tokens: 1200000 },
      ],
      workspace_total: 1250,
      by_workspace: [
        {
          workspace: "Triage WS",
          amount: 1250,
          pct: 100,
          tokens: 1500000,
          by_key: [{ api_key: "triage-key", amount: 1250, pct: 100, tokens: 1500000 }],
        },
      ],
    });
    renderDashboard();
    // Features tab is the default view.
    await screen.findByText("Key insights");

    fireEvent.click(screen.getByRole("tab", { name: "By Provider" }));
    // Spend by source splits into Inference / Build sub-tabs; Inference is default.
    expect(await screen.findByText("Inference (run) cost by provider")).toBeInTheDocument();
    // Build cost lives on the other sub-tab — hidden until selected.
    expect(screen.queryByText("Build cost by tool")).not.toBeInTheDocument();
    expect(screen.queryByText("Claude Code")).not.toBeInTheDocument();

    // The inference trend is a stacked classification chart with a compact legend.
    expect(screen.getByText("Trend · by classification")).toBeInTheDocument();
    const legend = screen.getByLabelText("Classification legend");
    ["Production", "Dev / Test", "Internal", "Unclassified"].forEach((label) =>
      expect(within(legend).getByText(label)).toBeInTheDocument(),
    );
    // Four stacked segments render for the single month (production/dev/internal/unclassified).
    expect(document.querySelectorAll(".trend-seg-fill").length).toBe(4);
    // The provider tab follows the Overview's selected period (default this month).
    await waitFor(() => expect(api.providerSpend).toHaveBeenCalledWith({ kind: "last_3_months" }));
    expect(screen.getByText("openai")).toBeInTheDocument();
    expect(screen.getByText(/\$4,200 · 77%/)).toBeInTheDocument();
    // Token-type split sits between the provider and workspace breakdowns, and is
    // labelled as a DERIVED split (providers don't bill per token type).
    // Scoped to this section: the Overview's own token panel above uses the
    // same two words for a different thing.
    const tokenSplit = screen
      .getByText("Inference cost by token type")
      .closest("section, div") as HTMLElement;
    expect(within(tokenSplit).getByText("Output")).toBeInTheDocument();
    expect(within(tokenSplit).getByText("Input")).toBeInTheDocument();
    expect(screen.getByText("derived")).toBeInTheDocument();
    // Provider-reported token counts show alongside the dollars.
    expect(screen.getByText("· 300K tok")).toBeInTheDocument();
    expect(screen.getByText("· 1.2M tok")).toBeInTheDocument();
    // Inference sub-tab also carries workspace/API-key and per-customer breakdowns.
    expect(screen.getByText("Inference cost by workspace & API key")).toBeInTheDocument();
    expect(screen.getByText("Triage WS")).toBeInTheDocument();
    expect(screen.getByText("triage-key")).toBeInTheDocument();
    // ...with the provider-reported token count next to each workspace/key amount.
    expect(screen.getAllByText("· 1.5M tok")).toHaveLength(2);
    expect(screen.getByText("Inference cost by customer")).toBeInTheDocument();
    expect(screen.getByText("acme")).toBeInTheDocument();

    // Switch to the Build cost sub-tab: build shows, inference-by-provider hides.
    fireEvent.click(screen.getByRole("tab", { name: "Build cost" }));
    expect(screen.getByText("Build cost by tool")).toBeInTheDocument();
    expect(screen.getByText("Claude Code")).toBeInTheDocument();
    expect(screen.getByText(/\$181 · 67%/)).toBeInTheDocument();
    expect(screen.queryByText("Inference (run) cost by provider")).not.toBeInTheDocument();

    // The summary/insights stay put across tabs; only the breakdown swaps.
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.getByText("Key insights")).toBeInTheDocument();
  });

  it("refetches with the chosen review period", async () => {
    renderDashboard();
    await screen.findByText("Key insights");
    expect(api.dashboard).toHaveBeenCalledWith({ kind: "last_3_months" });

    fireEvent.click(screen.getByRole("button", { name: /Sep 2026|2026/ }));
    fireEvent.click(screen.getByRole("button", { name: "Last 3 months" }));
    await waitFor(() => expect(api.dashboard).toHaveBeenCalledWith({ kind: "last_3_months" }));
  });

  it("switches to the By developer tab and shows build cost per developer", async () => {
    vi.mocked(api.providerSpend).mockResolvedValue({
      start: "2026-05-01",
      end: "2026-05-01",
      total: 0,
      by_provider: [],
      trend: [],
      daily_trend: [],
      build_total: 270,
      build_by_tool: [],
      developer_activity: [],
      activity_coverage: { ...EMPTY_SPEND.activity_coverage },
      build_by_developer: [
        {
          developer_id: "Muzaffar-ni",
          label: "Muzaffar (Muzaffar-ni)",
          amount: 181,
          pct: 67.04,
          by_tool: [{ tool: "claude_code", amount: 181, pct: 100 }],
        },
        {
          // Handle unavailable -> the label is just the name.
          developer_id: "frank",
          label: "Frank",
          amount: 89,
          pct: 32.96,
          by_tool: [{ tool: "cursor", amount: 89, pct: 100 }],
        },
      ],
      build_trend: [{ period: "2026-05-01", amount: 270 }],
      customer_total: 0,
      by_customer: [],
      token_total: 0,
      by_token_type: [],
      workspace_total: 0,
      by_workspace: [],
    });
    renderDashboard();
    await screen.findByText("Key insights");

    fireEvent.click(screen.getByRole("tab", { name: "By Developer" }));
    expect(await screen.findByText("Build cost by developer")).toBeInTheDocument();
    // The combined "Name (handle)" label renders; name-only falls back gracefully.
    expect(screen.getByText("Muzaffar (Muzaffar-ni)")).toBeInTheDocument();
    expect(screen.getByText("Frank")).toBeInTheDocument();
    expect(screen.getByText(/\$181 · 67%/)).toBeInTheDocument();
    // Each developer breaks down by the tool they used.
    expect(screen.getByText("Claude Code")).toBeInTheDocument();
  });

  it("explains an empty activity table instead of hiding the section", async () => {
    // Build cost exists, so the developer tab renders — but no PR evidence lands
    // in this window. Showing nothing left the reader unable to tell "nobody
    // shipped" from "discovery has not covered these months".
    const withBuild = {
      ...EMPTY_SPEND,
      build_total: 270,
      build_by_developer: [
        {
          developer_id: "d1",
          label: "Alice (alice)",
          amount: 270,
          pct: 100,
          by_tool: [{ tool: "cursor", amount: 270, pct: 100 }],
        },
      ],
      developer_activity: [],
    };

    // 1. Nothing connected: point at the connector first, then discovery.
    vi.mocked(api.providerSpend).mockResolvedValue({
      ...withBuild,
      activity_coverage: {
        github_connected: false,
        dated_prs: 0,
        undated_prs: 0,
        first_merged: null,
        last_merged: null,
        runs: 0,
        covered_from: null,
        covered_to: null,
        last_run_at: null,
        last_run_status: null,
        last_run_trigger: null,
      },
    });
    const first = renderDashboard();
    fireEvent.click(await screen.findByRole("tab", { name: "By Developer" }));
    expect(await screen.findByText("No pull-request evidence yet")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Cost sources" })).toHaveAttribute(
      "href",
      "/cost-sources",
    );
    first.unmount();

    // 2. Connected but never discovered: only discovery is missing.
    vi.mocked(api.providerSpend).mockResolvedValue({
      ...withBuild,
      activity_coverage: {
        github_connected: true,
        dated_prs: 0,
        undated_prs: 0,
        first_merged: null,
        last_merged: null,
        runs: 0,
        covered_from: null,
        covered_to: null,
        last_run_at: null,
        last_run_status: null,
        last_run_trigger: null,
      },
    });
    const second = renderDashboard();
    fireEvent.click(await screen.findByRole("tab", { name: "By Developer" }));
    expect(await screen.findByText("Discovery has not run yet")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Features" })).toHaveAttribute("href", "/features");
    second.unmount();
  });

  it("says which months the PR evidence does cover, and counts undated PRs", async () => {
    vi.mocked(api.providerSpend).mockResolvedValue({
      ...EMPTY_SPEND,
      start: "2026-05-01",
      end: "2026-05-01",
      build_total: 270,
      build_by_developer: [
        {
          developer_id: "d1",
          label: "Alice (alice)",
          amount: 270,
          pct: 100,
          by_tool: [{ tool: "cursor", amount: 270, pct: 100 }],
        },
      ],
      developer_activity: [],
      // No runs recorded: this tenant's evidence predates run history, so all
      // that can honestly be said is which merge dates are held.
      activity_coverage: {
        ...EMPTY_SPEND.activity_coverage,
        github_connected: true,
        dated_prs: 40,
        undated_prs: 3,
        first_merged: "2026-01-04",
        last_merged: "2026-03-28",
        runs: 0,
        covered_from: null,
        covered_to: null,
      },
    });
    renderDashboard();
    fireEvent.click(await screen.findByRole("tab", { name: "By Developer" }));

    expect(await screen.findByText("No pull requests merged in May 2026")).toBeInTheDocument();
    // Without run history all that can honestly be said is which merge dates are
    // held — and that nothing records which windows were looked at.
    expect(screen.getByText(/Jan 4, 2026 – Mar 28, 2026/)).toBeInTheDocument();
    expect(screen.getByText(/no record of which windows/)).toBeInTheDocument();
    // Undated PRs are counted, never quietly dropped.
    expect(screen.getByText(/3 pull requests discovered before merge dates/)).toBeInTheDocument();
  });

  it("tracks engineering activity per developer under the cost breakdown", async () => {
    vi.mocked(api.providerSpend).mockResolvedValue({
      ...EMPTY_SPEND,
      build_total: 270,
      build_by_developer: [
        {
          developer_id: "bob",
          label: "Bob (bob)",
          amount: 64,
          pct: 100,
          by_tool: [{ tool: "cursor", amount: 64, pct: 100 }],
        },
      ],
      developer_activity: [
        {
          handle: "bob",
          label: "Bob (bob)",
          prs: 11,
          features: 10,
          commits: 37,
          files_changed: 72,
          additions: 1610,
          deletions: 445,
          build_cost: 64,
          cost_per_pr: 5.82,
        },
        {
          // Discovered before line counts were recorded — unknown, not zero.
          handle: "olddev",
          label: "olddev",
          prs: 2,
          features: 1,
          commits: null,
          files_changed: null,
          additions: null,
          deletions: null,
          build_cost: 0,
          cost_per_pr: null,
        },
      ],
    });
    renderDashboard();
    await screen.findByText("Key insights");

    fireEvent.click(screen.getByRole("tab", { name: "By Developer" }));
    expect(await screen.findByText("Engineering activity")).toBeInTheDocument();

    const activity = screen.getByText("Engineering activity").closest("section")!;
    const bob = within(activity).getByText("Bob (bob)").closest("tr")!;
    expect(within(bob).getByText("11")).toBeInTheDocument(); // PRs
    expect(within(bob).getByText(/\+1\.6K/)).toBeInTheDocument(); // lines added
    expect(within(bob).getByText("$5.82")).toBeInTheDocument(); // cost per PR

    // Missing stats read as an em dash, never as a zero that implies no work.
    const old = within(activity).getByText("olddev").closest("tr")!;
    expect(within(old).getAllByText("—").length).toBeGreaterThan(0);

    // Counting shipped work is not a performance rating, and the page says so.
    expect(screen.getByText(/activity, not performance/)).toBeInTheDocument();
  });

  it("shows spend per customer, with unit economics and coverage of the bill", async () => {
    vi.mocked(api.customerSpend).mockResolvedValue({
      start: "2026-05-01",
      end: "2026-05-01",
      months: 1,
      total: 750,
      customers: [
        {
          customer_id: "acme",
          amount: 600,
          pct: 80,
          requests: 20000,
          cost_per_request: 0.03,
          prev_amount: 400,
          delta_pct: 50,
          months_active: 1,
        },
        {
          customer_id: "globex",
          amount: 150,
          pct: 20,
          requests: 30000,
          cost_per_request: 0.005,
          prev_amount: null,
          delta_pct: null,
          months_active: 1,
        },
      ],
      trend: [{ period: "2026-05-01", amount: 750 }],
      inference_total: 5000,
      coverage_pct: 15,
    });
    renderDashboard();
    await screen.findByText("Key insights");

    fireEvent.click(screen.getByRole("tab", { name: "By Customer" }));
    expect(await screen.findByText("Inference cost by customer")).toBeInTheDocument();

    // Every customer is listed in the table (the bars above show only the top few).
    const table = screen.getByRole("table");
    expect(within(table).getByText("acme")).toBeInTheDocument();
    expect(within(table).getByText("globex")).toBeInTheDocument();
    // Sub-cent unit cost keeps its precision instead of rounding to $0.00.
    expect(screen.getByText("$0.005")).toBeInTheDocument();
    expect(screen.getByText("$0.03")).toBeInTheDocument();
    // A customer with no prior spend reads as new, not as a 0% change.
    expect(screen.getByText("new")).toBeInTheDocument();
    expect(screen.getByText(/▲ 50%/)).toBeInTheDocument();
    // Metered spend is a subset of the bill, and the page says how big a subset.
    expect(screen.getByText(/15% of the \$5,000 inference bill/)).toBeInTheDocument();
  });

  it("explains that per-customer cost needs the SDK when nothing is tagged", async () => {
    vi.mocked(api.customerSpend).mockResolvedValue({
      start: "2026-05-01",
      end: "2026-05-01",
      months: 1,
      total: 0,
      customers: [],
      trend: [],
      inference_total: 5000,
      coverage_pct: 0,
    });
    renderDashboard();
    await screen.findByText("Key insights");

    fireEvent.click(screen.getByRole("tab", { name: "By Customer" }));
    expect(await screen.findByText("No customer-attributed spend yet")).toBeInTheDocument();
    expect(screen.getByText(/never who it was spent on/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Install the SDK" })).toHaveAttribute(
      "href",
      "/install-sdk",
    );
  });

  it("re-pulls the provider/developer breakdowns on refresh", async () => {
    vi.mocked(api.providerSpend).mockResolvedValue({
      start: "2026-05-01",
      end: "2026-05-01",
      total: 0,
      by_provider: [],
      trend: [],
      daily_trend: [],
      build_total: 0,
      build_by_tool: [],
      developer_activity: [],
      activity_coverage: { ...EMPTY_SPEND.activity_coverage },
      build_by_developer: [],
      build_trend: [],
      customer_total: 0,
      by_customer: [],
      token_total: 0,
      by_token_type: [],
      workspace_total: 0,
      by_workspace: [],
    });
    renderDashboard();
    await screen.findByText("Key insights");

    // The inference/build breakdowns live on the By provider tab.
    fireEvent.click(screen.getByRole("tab", { name: "By Provider" }));
    await waitFor(() => expect(api.providerSpend).toHaveBeenCalledTimes(1));

    // Refresh must re-pull them too — not just the summary tiles.
    fireEvent.click(screen.getByRole("button", { name: "Refresh data and alerts" }));
    await waitFor(() => expect(api.providerSpend).toHaveBeenCalledTimes(2));
  });

  it("reports a provider that could not be refreshed", async () => {
    vi.mocked(api.refreshInference).mockResolvedValue({
      providers: 2,
      synced: [{ provider: "anthropic", total: 100 }],
      errors: [{ provider: "openai", error: "Provider rejected the admin key (401)." }],
      total: 100,
    });
    renderDashboard();
    await screen.findByText("Key insights");
    fireEvent.click(screen.getByRole("button", { name: "Refresh data and alerts" }));
    // The failure is surfaced, not swallowed behind unchanged numbers.
    expect(
      await screen.findByText(/Could not refresh openai \(Provider rejected/),
    ).toBeInTheDocument();
  });

  it("stamps when cost was last ingested, not when the page loaded", async () => {
    renderDashboard();
    await screen.findByText("Key insights");
    // Short format, and driven by data_updated_at (Aug 20) — NOT today's date.
    const stamp = screen.getByText(/^Updated [A-Z][a-z]{2} \d{1,2}, \d{1,2}:\d{2} [AP]M$/);
    expect(stamp.textContent).toContain("Aug 20");
    // The tooltip breaks freshness down per source.
    expect(stamp.getAttribute("title")).toMatch(/Inference: Aug 20/);
    expect(stamp.getAttribute("title")).toMatch(/Build: Aug 19/);
  });

  it("hides the stamp until cost has been ingested at least once", async () => {
    vi.mocked(api.dashboard).mockResolvedValue({
      ...DATA,
      data_updated_at: null,
      inference_updated_at: null,
      build_updated_at: null,
    });
    renderDashboard();
    await screen.findByText("Key insights");
    expect(screen.queryByText(/^Updated /)).not.toBeInTheDocument();
  });

  it("refreshes data and signals an alerts refresh when the refresh button is clicked", async () => {
    const dispatch = vi.spyOn(window, "dispatchEvent");
    renderDashboard();
    await screen.findByText("Key insights");
    expect(api.dashboard).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Refresh data and alerts" }));
    // Pulls fresh cost from connected providers, then re-reads the dashboard and
    // fires the alerts-refresh window event.
    await waitFor(() => expect(api.refreshInference).toHaveBeenCalled());
    await waitFor(() => expect(api.dashboard).toHaveBeenCalledTimes(2));
    expect(dispatch.mock.calls.some(([e]) => e.type === "annapurna:refresh-alerts")).toBe(true);
  });
});
