import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, type Opportunity } from "../api";
import { AuthProvider } from "../auth/AuthContext";
import { FeatureDetail } from "./FeatureDetail";

/** Surfaces the router's current query string so tests can assert URL state. */
function LocationProbe() {
  return <div data-testid="loc">{useLocation().search}</div>;
}

vi.mock("../api", async (importActual) => {
  const actual = await importActual<typeof import("../api")>();
  return {
    ...actual,
    api: {
      me: vi.fn(),
      featureDetail: vi.fn(),
      setFeatureCategory: vi.fn(),
      featureCategories: vi.fn(),
      listProducts: vi.fn(),
      setFeatureProduct: vi.fn(),
      featureInference: vi.fn(),
      featureOpportunities: vi.fn(),
      applyOpportunity: vi.fn(),
      unapplyOpportunity: vi.fn(),
      logout: vi.fn(),
    },
  };
});

function opp(over: Partial<Opportunity>): Opportunity {
  const base: Opportunity = {
    lever: "x",
    title: "X",
    source: "sdk",
    savings_type: "measured",
    confidence: "high",
    confidence_reason: "reason",
    projected_monthly_savings: 0,
    projected_annual_savings: 0,
    engineering_effort: "low",
    priority_score: 0,
    evidence: "",
    fix: null,
    validation_guidance: "validate it",
    verification: "we verify it",
    status: "detected",
    overlaps: null,
    trail: [],
  };
  return { ...base, ...over };
}

const OPPORTUNITIES = {
  period: "2026-05-01",
  opportunities: [
    opp({
      lever: "prompt_caching",
      title: "Prompt caching",
      projected_monthly_savings: 264.79,
      evidence: "a 4,100-token static prefix repeated across 23,920 uncached calls",
      fix: "Enable prompt caching (set cache_control on the static system block).",
      trail: [{ fingerprint: "prefix-syste", model: "claude-sonnet-4-6", prefix_tokens: 4100 }],
    }),
    opp({
      lever: "duplicate_calls",
      title: "Duplicate calls",
      projected_monthly_savings: 369.0,
      evidence: "1,240 duplicate calls across 3 distinct requests this month",
      fix: "Add response caching for identical requests (e.g. keyed on the request hash).",
      trail: [{ fingerprint: "dup-alert-fo", model: "claude-sonnet-4-6", call_count: 620 }],
    }),
    opp({
      lever: "model_downgrade_est",
      title: "Model downgrade",
      source: "heuristic",
      savings_type: "directional",
      confidence: "med",
      projected_monthly_savings: 70,
      evidence: "Route cheaper.",
    }),
  ],
  totals: { measured: 633.79, modeled_ceiling: 0, directional: 70 },
  cache_utilization: 0.08,
  actions: [] as {
    lever: string;
    applied_on: string;
    projected_monthly: number;
    current_avoidable: number;
    realized_monthly: number | null;
    status: "pending" | "measured";
  }[],
};

const DETAIL = {
  feature_id: "f1",
  name: "AI threat triage",
  description: "Classifies alerts.",
  status: "confirmed",
  discovery_confidence: "high",
  category: "api",
  category_source: "discovery",
  product_id: "p1",
  product_name: "Threat Platform",
  product_source: "discovery",
  period: "2026-05-01",
  start: "2026-05-01",
  end: "2026-05-01",
  headline: { build_cost: 181, inference_cost: 4200, active_users: 540, avg_latency_ms: 820 },
  build_total: 181,
  build_contributors: 2,
  build_by_developer: [
    {
      developer_id: "alice",
      tool: "claude_code",
      amount: 117,
      confidence: "high",
      prs: 2,
      commits: 14,
      files_changed: 37,
    },
  ],
  evidence: [
    {
      signal_type: "pr",
      external_ref: "acme/core#1421",
      confidence: "high",
      actor: "alice",
      source: "github",
    },
  ],
  inference_sources: ["cost_api"],
  optimization: {
    opportunities: [
      {
        opportunity: "Prompt caching",
        savings: 504,
        confidence: "high",
        rationale: "Cache repeated prompt prefixes.",
      },
      {
        opportunity: "Model downgrade",
        savings: 420,
        confidence: "med",
        rationale: "Route to a cheaper model.",
      },
    ],
    monthly_savings: 924,
    annual_savings: 11088,
  },
};

const INFERENCE = {
  start: "2026-05-01",
  end: "2026-05-01",
  total: 1850,
  by_model: [
    { model: "gpt-4o", amount: 1250, pct: 67.6, requests: 60000 },
    { model: "claude-sonnet-4-6", amount: 400, pct: 21.6, requests: 20000 },
    { model: "claude-haiku-4-5", amount: 200, pct: 10.8, requests: 8000 },
  ],
  trend: [{ period: "2026-05-01", amount: 1850 }],
};

function renderDetail() {
  return render(
    <MemoryRouter initialEntries={["/features/f1"]}>
      <AuthProvider>
        <Routes>
          <Route path="/features/:id" element={<FeatureDetail />} />
        </Routes>
        <LocationProbe />
      </AuthProvider>
    </MemoryRouter>,
  );
}

describe("FeatureDetail", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.me).mockResolvedValue({ id: "u1", tenant_id: "t1", email: "cto@acme.com" });
    vi.mocked(api.featureDetail).mockResolvedValue(DETAIL);
    // The product picker fetches the customer's product list on mount.
    vi.mocked(api.listProducts).mockResolvedValue({ products: [] });
    // The category picker fetches its vocabulary on mount.
    vi.mocked(api.featureCategories).mockResolvedValue({
      categories: [
        { value: "api", label: "API" },
        { value: "auth", label: "Auth" },
      ],
    });
    vi.mocked(api.featureInference).mockResolvedValue(INFERENCE);
    vi.mocked(api.featureOpportunities).mockResolvedValue(OPPORTUNITIES);
    vi.mocked(api.applyOpportunity).mockResolvedValue({
      lever: "duplicate_calls",
      applied_on: "2026-05-01",
    });
    vi.mocked(api.unapplyOpportunity).mockResolvedValue(undefined);
  });

  it("organizes into Developer cost and Inference cost sections", async () => {
    renderDetail();

    expect(await screen.findByRole("heading", { name: "AI threat triage" })).toBeInTheDocument();

    // Avg latency from metered (SDK) calls shows in the header.
    expect(screen.getByText(/820 ms avg latency/)).toBeInTheDocument();

    // Developer cost section: total spend + per-developer breakdown.
    expect(screen.getByText("Developer cost")).toBeInTheDocument();
    expect(screen.getByText("$181")).toBeInTheDocument(); // total build spend
    expect(screen.getByText("alice")).toBeInTheDocument();

    // A single review-period control at the top scopes the page (default this month).
    expect(screen.getByRole("button", { name: /2026/ })).toBeInTheDocument();
    // Inference cost section: connector indicator + in-period total (no /mo window buttons).
    expect(screen.getByText("Inference cost")).toBeInTheDocument();
    expect(screen.getByText(/connector-derived/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Month" })).not.toBeInTheDocument();
    // Engineering activity is labelled as all-time, distinct from the period costs.
    expect(screen.getByText(/commits, PRs, and files are all-time/)).toBeInTheDocument();

    // Pie (by model) + trend chart load from the inference endpoint.
    expect(await screen.findByText("gpt-4o")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Inference cost by model" })).toBeInTheDocument();
    expect(screen.getByText("May")).toBeInTheDocument(); // trend bar label

    // Optimization: measured findings on top, estimated (heuristic) demoted.
    expect(screen.getByText("Optimization opportunities")).toBeInTheDocument();
    expect(await screen.findByText("Prompt caching")).toBeInTheDocument(); // measured lever
    expect(screen.getByText("Duplicate calls")).toBeInTheDocument(); // measured lever
    // Measured evidence sentence + the specific fix render.
    expect(
      screen.getByText(/1,240 duplicate calls across 3 distinct requests/),
    ).toBeInTheDocument();
    expect(screen.getByText("$634/mo")).toBeInTheDocument(); // measured savings headline (rounded)
    // Each measured card shows its engineering-effort chip (how hard the fix is).
    expect(screen.getAllByText("Low effort").length).toBeGreaterThan(0);
    // Current cache utilization context.
    expect(screen.getByText(/8% of input is already cached/)).toBeInTheDocument();
    // Estimated tier is present but clearly separate (the heuristic lever).
    expect(screen.getByText("Model downgrade")).toBeInTheDocument();
    expect(screen.getByText("directional estimate")).toBeInTheDocument();
  });

  it("shows a quality-gated ceiling ('up to') for a modeled_ceiling lever", async () => {
    vi.mocked(api.featureOpportunities).mockResolvedValue({
      ...OPPORTUNITIES,
      opportunities: [
        opp({
          lever: "model_rightsizing",
          title: "Model right-sizing",
          source: "connector",
          savings_type: "modeled_ceiling",
          confidence: "med",
          projected_monthly_savings: 2566,
          evidence: "claude-sonnet-4-6 handles this feature; claude-haiku-4-5 is ~73% cheaper",
          fix: "Move claude-sonnet-4-6 → claude-haiku-4-5 where quality allows — up to $2,566.00/mo.",
          trail: [{ model: "claude-sonnet-4-6 → claude-haiku-4-5", note: "up to $2,566.00/mo" }],
        }),
      ],
      totals: { measured: 0, modeled_ceiling: 2566, directional: 0 },
    });
    renderDetail();
    expect(await screen.findByText("Model right-sizing")).toBeInTheDocument();
    expect(
      screen.getByText(/Move claude-sonnet-4-6 → claude-haiku-4-5 where quality allows/),
    ).toBeInTheDocument();
    // The savings is prefixed "up to" (a quality-gated ceiling)...
    expect(document.querySelector(".opt-ceiling")?.textContent).toContain("up to");
    // ...and the headline reads "Modeled ceiling", NOT guaranteed "Measured savings".
    expect(screen.getByText("Modeled ceiling")).toBeInTheDocument();
    expect(screen.queryByText("Measured savings")).not.toBeInTheDocument();
  });

  it("nudges installing the SDK when there are no measured opportunities", async () => {
    vi.mocked(api.featureOpportunities).mockResolvedValue({
      ...OPPORTUNITIES,
      opportunities: OPPORTUNITIES.opportunities.filter((o) => o.savings_type === "directional"),
      totals: { measured: 0, modeled_ceiling: 0, directional: 70 },
      cache_utilization: null,
    });
    renderDetail();
    expect(await screen.findByText(/Install the metering SDK/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Install SDK/ })).toHaveAttribute(
      "href",
      "/install-sdk",
    );
    // The estimated tier still shows below the nudge.
    expect(screen.getByText("Model downgrade")).toBeInTheDocument();
  });

  it("marks a measured opportunity as applied and reloads", async () => {
    renderDetail();
    // Two measured levers -> two "Mark as applied" buttons.
    const buttons = await screen.findAllByRole("button", { name: "Mark as applied" });
    fireEvent.click(buttons[0]);
    // The first card is the prompt_caching lever (savings 264.79).
    await waitFor(() =>
      expect(api.applyOpportunity).toHaveBeenCalledWith("f1", "prompt_caching", 264.79),
    );
    // Reloads opportunities after applying.
    await waitFor(() => expect(api.featureOpportunities).toHaveBeenCalledTimes(2));
  });

  it("shows projected → realized → verified for an applied optimization", async () => {
    vi.mocked(api.featureOpportunities).mockResolvedValue({
      ...OPPORTUNITIES,
      actions: [
        {
          lever: "duplicate_calls",
          applied_on: "2026-03-01",
          projected_monthly: 500,
          current_avoidable: 369,
          realized_monthly: 131,
          status: "verified",
        },
      ],
    });
    renderDetail();
    expect(await screen.findByText("Applied optimizations")).toBeInTheDocument();
    expect(screen.getByText("$500/mo")).toBeInTheDocument(); // projected
    expect(screen.getByText("$131/mo")).toBeInTheDocument(); // realized
    // Held for 2 periods -> the terminal Prove state, verified.
    expect(screen.getByText(/✓ Verified/)).toBeInTheDocument();
    // The matching measured card shows an "Applied" chip and an Undo control.
    expect(screen.getByText(/✓ Applied Mar 2026/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Undo" })).toBeInTheDocument();
  });

  it("marks a directional estimate superseded by a measured finding", async () => {
    vi.mocked(api.featureOpportunities).mockResolvedValue({
      ...OPPORTUNITIES,
      opportunities: [
        ...OPPORTUNITIES.opportunities,
        opp({
          lever: "prompt_caching_est",
          title: "Prompt caching",
          source: "heuristic",
          savings_type: "directional",
          confidence: "med",
          projected_monthly_savings: 445,
          overlaps: "Prompt caching",
        }),
      ],
    });
    renderDetail();
    // The superseded estimate is shown but flagged as measured elsewhere.
    expect(await screen.findByText(/measured as Prompt caching/)).toBeInTheDocument();
  });

  it("shows validate/verify guidance on each measured card", async () => {
    renderDetail();
    expect(await screen.findAllByText("How to apply & verify")).toHaveLength(2);
    expect(screen.getAllByText("validate it").length).toBeGreaterThan(0);
    expect(screen.getAllByText("we verify it").length).toBeGreaterThan(0);
  });

  it("refetches every section when the review period changes, and reflects it in the URL", async () => {
    renderDetail();
    await screen.findByText("gpt-4o");
    // Default range is this month; all three cost sections fetch with it.
    expect(api.featureDetail).toHaveBeenCalledWith("f1", { kind: "this_month" });
    expect(api.featureInference).toHaveBeenCalledWith("f1", { kind: "this_month" });
    expect(api.featureOpportunities).toHaveBeenCalledWith("f1", { kind: "this_month" });

    fireEvent.click(screen.getByRole("button", { name: /Sep 2026|2026/ }));
    fireEvent.click(screen.getByRole("button", { name: "Last 3 months" }));
    await waitFor(() =>
      expect(api.featureInference).toHaveBeenCalledWith("f1", { kind: "last_3_months" }),
    );
    expect(api.featureDetail).toHaveBeenCalledWith("f1", { kind: "last_3_months" });
    expect(api.featureOpportunities).toHaveBeenCalledWith("f1", { kind: "last_3_months" });
    // The chosen range is preserved in the URL.
    expect(screen.getByTestId("loc").textContent).toContain("range=last_3_months");
  });

  it("reads the initial review period from the URL", async () => {
    render(
      <MemoryRouter initialEntries={["/features/f1?range=last_6_months"]}>
        <AuthProvider>
          <Routes>
            <Route path="/features/:id" element={<FeatureDetail />} />
          </Routes>
        </AuthProvider>
      </MemoryRouter>,
    );
    await screen.findByRole("heading", { name: "AI threat triage" });
    expect(api.featureDetail).toHaveBeenCalledWith("f1", { kind: "last_6_months" });
  });

  it("lets someone retag the feature from its detail page", async () => {
    // Most features are confirmed, and never appear on the Features review list —
    // so the detail page has to carry the control too, or they can't be corrected.
    vi.mocked(api.setFeatureCategory).mockResolvedValue({} as never);
    vi.mocked(api.featureCategories).mockResolvedValue({
      categories: [
        { value: "api", label: "API" },
        { value: "auth", label: "Auth" },
      ],
    });
    renderDetail();
    // The badge, not the picker's <option> of the same name.
    expect(await screen.findByText("API", { selector: ".badge" })).toBeInTheDocument();

    const picker = await screen.findByLabelText("Feature type");
    await waitFor(() => expect(picker).not.toBeDisabled());
    fireEvent.change(picker, { target: { value: "auth" } });
    await waitFor(() => expect(api.setFeatureCategory).toHaveBeenCalledWith("f1", "auth"));
  });
});
