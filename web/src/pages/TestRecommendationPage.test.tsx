import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, type Experiment, type Opportunity } from "../api";
import { TestRecommendationPage } from "./TestRecommendationPage";

vi.mock("../api", async (importActual) => {
  const actual = await importActual<typeof import("../api")>();
  return {
    ...actual,
    api: { featureOpportunities: vi.fn(), startExperiment: vi.fn(), experimentOptions: vi.fn() },
  };
});

function opp(lever: string, over: Partial<Opportunity> = {}): Opportunity {
  return {
    lever,
    title: lever === "duplicate_calls" ? "Repeated request candidates" : "Prompt caching",
    source: "sdk",
    savings_type: "modeled_ceiling",
    confidence: "med",
    confidence_reason: "",
    projected_monthly_savings: 121.5,
    projected_annual_savings: 1458,
    engineering_effort: "medium",
    priority_score: 1,
    evidence: "",
    fix: null,
    validation_guidance: "",
    verification: "",
    status: "detected",
    overlaps: null,
    trail: [],
    testable: true,
    ...over,
  };
}

function load(opportunities: Opportunity[]) {
  vi.mocked(api.featureOpportunities).mockResolvedValue({
    period: "2026-05-01",
    opportunities,
    totals: { measured: 0, modeled_ceiling: 121.5, directional: 0 },
    cache_utilization: null,
    actions: [],
  });
}

function Where() {
  const location = useLocation();
  return (
    <div data-testid="at">
      {location.pathname} {(location.state as { token?: string } | null)?.token ?? ""}
    </div>
  );
}

const OPTIONS = {
  period: "2026-05-01",
  controls: [
    {
      model: "claude-sonnet-4-6",
      provider: "anthropic" as const,
      monthly_spend: 1800,
      default_candidate: "claude-haiku-4-5",
      candidates: [
        { model: "claude-sonnet-5", save_fraction: 0.33 },
        { model: "claude-haiku-4-5", save_fraction: 0.67 },
      ],
    },
    {
      model: "claude-opus-4-8",
      provider: "anthropic" as const,
      monthly_spend: 700,
      default_candidate: "claude-sonnet-4-6",
      candidates: [{ model: "claude-sonnet-4-6", save_fraction: 0.8 }],
    },
  ],
  rule: {
    loss_margin: 0.1,
    min_cases: 20,
    loss_margin_range: [0, 0.5] as [number, number],
    min_cases_range: [10, 500] as [number, number],
  },
};

function renderPage(lever: string) {
  return render(
    <MemoryRouter initialEntries={[`/features/f1/test/${lever}`]}>
      <Routes>
        <Route path="/features/:featureId/test/:lever" element={<TestRecommendationPage />} />
        <Route path="*" element={<Where />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("Test this", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.startExperiment).mockResolvedValue({ id: "e1" } as Experiment);
  });

  it("starts from Meter's assumption and the safe answer about sharing", async () => {
    load([opp("duplicate_calls")]);
    renderPage("duplicate_calls");
    expect(
      await screen.findByText(/assuming an answer can be reused for 10 minutes/),
    ).toBeVisible();
    expect(screen.getByRole("radio", { name: /10 minutes/ })).toBeChecked();
    expect(
      screen.getByRole("checkbox", { name: /Only reuse an answer for the same customer/ }),
    ).toBeChecked();

    fireEvent.click(screen.getByRole("button", { name: "Run the simulation" }));
    await waitFor(() =>
      expect(api.startExperiment).toHaveBeenCalledWith("f1", {
        lever: "duplicate_calls",
        ttl_seconds: 600,
        scoped_only: true,
      }),
    );
    expect(await screen.findByTestId("at")).toHaveTextContent("/experiments/e1");
  });

  it("does not pin a tested figure on Meter's default assumption", async () => {
    load([
      opp("duplicate_calls", {
        projected_monthly_savings: 214.8,
        validation: "simulated",
        experiment: {
          id: "e1",
          status: "completed",
          outcome: "passed",
          setting_label: "answers reused for up to 1 hour",
          tested_on: "2026-05-19",
        },
      }),
    ]);
    renderPage("duplicate_calls");
    expect(await screen.findByText(/Your last test put this at/)).toHaveTextContent(
      "$215/mo, with answers reused for up to 1 hour",
    );
    expect(screen.queryByText(/assuming an answer can be reused for 10 minutes/)).toBeNull();
  });

  it("sends the customer's own choice", async () => {
    load([opp("duplicate_calls")]);
    renderPage("duplicate_calls");
    fireEvent.click(await screen.findByRole("radio", { name: /1 hour/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /same customer/ }));
    fireEvent.click(screen.getByRole("button", { name: "Run the simulation" }));
    await waitFor(() =>
      expect(api.startExperiment).toHaveBeenCalledWith("f1", {
        lever: "duplicate_calls",
        ttl_seconds: 3600,
        scoped_only: false,
      }),
    );
  });

  it("asks prompt caching about the cache lifetime, nothing else", async () => {
    load([opp("prompt_caching")]);
    renderPage("prompt_caching");
    expect(await screen.findByRole("radio", { name: /1 hour/ })).toBeChecked();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("radio", { name: /5 minutes/ }));
    fireEvent.click(screen.getByRole("button", { name: "Run the simulation" }));
    await waitFor(() =>
      expect(api.startExperiment).toHaveBeenCalledWith("f1", {
        lever: "prompt_caching",
        cache_ttl: "5m",
      }),
    );
  });

  it("shows a refusal beside the button that was pressed", async () => {
    load([opp("duplicate_calls")]);
    vi.mocked(api.startExperiment).mockRejectedValue(
      new ApiError(400, "Choose how old a cached answer may be."),
    );
    renderPage("duplicate_calls");
    const button = await screen.findByRole("button", { name: "Run the simulation" });
    fireEvent.click(button);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Choose how old a cached answer may be.");
    expect(alert.parentElement).toBe(button.parentElement);
  });

  it("says plainly when a recommendation cannot be tested", async () => {
    load([opp("prompt_caching", { testable: false })]);
    renderPage("prompt_caching");
    expect(await screen.findByText("This recommendation cannot be tested yet.")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Run the simulation" })).not.toBeInTheDocument();
  });

  it("promises nothing is read or spent", async () => {
    load([opp("duplicate_calls")]);
    renderPage("duplicate_calls");
    expect(
      await screen.findByText(/reads no prompts or responses and calls no model/),
    ).toBeVisible();
  });
});

describe("Test this, for model right-sizing", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.experimentOptions).mockResolvedValue(OPTIONS);
    vi.mocked(api.startExperiment).mockResolvedValue({
      id: "e9",
      token: "mtx_secret",
    } as Experiment);
    load([opp("model_rightsizing", { projected_monthly_savings: 4093 })]);
  });

  it("starts from the recommendation's own target and Meter's rule", async () => {
    renderPage("model_rightsizing");
    expect(await screen.findByLabelText("Model to replace")).toHaveValue("claude-sonnet-4-6");
    expect(screen.getByLabelText("Cheaper model to test in its place")).toHaveValue(
      "claude-haiku-4-5",
    );
    expect(screen.getByLabelText("Allowance for worse answers (%)")).toHaveValue(10);
    expect(screen.getByLabelText("Cases needed, at least")).toHaveValue(20);
    expect(screen.getByText(/Only numbers come back to Meter/)).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "Start the test" }));
    await waitFor(() =>
      expect(api.startExperiment).toHaveBeenCalledWith("f1", {
        lever: "model_rightsizing",
        control_model: "claude-sonnet-4-6",
        candidate_model: "claude-haiku-4-5",
        loss_margin: 0.1,
        min_cases: 20,
      }),
    );
    // The run's token travels with the navigation, and nowhere else.
    expect(await screen.findByTestId("at")).toHaveTextContent("/experiments/e9 mtx_secret");
  });

  it("offers each model its own cheaper replacements", async () => {
    renderPage("model_rightsizing");
    fireEvent.change(await screen.findByLabelText("Model to replace"), {
      target: { value: "claude-opus-4-8" },
    });
    expect(screen.getByLabelText("Cheaper model to test in its place")).toHaveValue(
      "claude-sonnet-4-6",
    );
  });

  it("says out loud when the customer loosens the rule", async () => {
    renderPage("model_rightsizing");
    const margin = await screen.findByLabelText("Allowance for worse answers (%)");
    expect(screen.queryByRole("note")).not.toBeInTheDocument();
    fireEvent.change(margin, { target: { value: "25" } });
    expect(screen.getByRole("note")).toHaveTextContent("Looser than Meter");
    fireEvent.click(screen.getByRole("button", { name: "Start the test" }));
    await waitFor(() =>
      expect(api.startExperiment).toHaveBeenCalledWith(
        "f1",
        expect.objectContaining({ loss_margin: 0.25 }),
      ),
    );
  });

  it("explains when none of the feature's models can be tested", async () => {
    vi.mocked(api.experimentOptions).mockResolvedValue({ ...OPTIONS, controls: [] });
    renderPage("model_rightsizing");
    expect(await screen.findByText(/can be tested yet/)).toBeVisible();
  });
});
