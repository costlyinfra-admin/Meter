import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, type Experiment } from "../api";
import { ExperimentPage } from "./ExperimentPage";

vi.mock("../api", async (importActual) => {
  const actual = await importActual<typeof import("../api")>();
  return { ...actual, api: { experiment: vi.fn(), cancelExperiment: vi.fn() } };
});

function repeats(over: Partial<Experiment> = {}): Experiment {
  return {
    id: "e1",
    feature_id: "f1",
    feature_name: "Report generator",
    lever: "duplicate_calls",
    mode: "simulate",
    status: "completed",
    outcome: "passed",
    outcome_reason:
      "A cache keeping answers for up to 1 hour would have served 760 of 18,000 calls.",
    setting: { ttl_seconds: 3600, scoped_only: true },
    setting_label: "answers reused for up to 1 hour, within a customer or cache scope only",
    period: "2026-05-01",
    baseline: { monthly: 121.5, savings_type: "modeled_ceiling", confidence: "med" },
    result: {
      kind: "repeats",
      calls: 18000,
      unscoped_calls: 0,
      scoped_only: true,
      ttl_seconds: 3600,
      monthly_saving: 214.8,
      hits: 760,
      hit_rate: 0.0422,
      ladder: [
        {
          ttl_seconds: 60,
          label: "1 minute",
          hits: 180,
          hit_rate: 0.01,
          monthly_saving: 50.7,
          lower_bound: false,
        },
        {
          ttl_seconds: 600,
          label: "10 minutes",
          hits: 430,
          hit_rate: 0.0239,
          monthly_saving: 121.5,
          lower_bound: false,
        },
        {
          ttl_seconds: 3600,
          label: "1 hour",
          hits: 760,
          hit_rate: 0.0422,
          monthly_saving: 214.8,
          lower_bound: false,
        },
        {
          ttl_seconds: 86400,
          label: "24 hours",
          hits: 1240,
          hit_rate: 0.0689,
          monthly_saving: 350.25,
          lower_bound: true,
        },
      ],
    },
    created_by: "cto@acme.com",
    created_at: "2026-05-19T15:30:00Z",
    completed_at: "2026-05-19T15:30:00Z",
    cancelled_at: null,
    history: [],
    ...over,
  };
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/experiments/e1"]}>
      <Routes>
        <Route path="/experiments/:id" element={<ExperimentPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("A test's result", () => {
  beforeEach(() => vi.clearAllMocks());

  it("leads with the answer, then the figure before and after", async () => {
    vi.mocked(api.experiment).mockResolvedValue(repeats());
    renderPage();
    const outcome = await screen.findByRole("status");
    expect(outcome).toHaveTextContent("Simulated");
    expect(outcome).toHaveTextContent("would have served 760 of 18,000 calls");
    expect(screen.getByText(/Before this test Meter estimated/)).toHaveTextContent(
      "$122/mo, a ceiling. Under your setting: $215/mo.",
    );
  });

  it("shows every limit, the one chosen, and which are only a minimum", async () => {
    vi.mocked(api.experiment).mockResolvedValue(repeats());
    renderPage();
    const rows = (await screen.findAllByRole("row")).slice(1);
    expect(rows.map((r) => within(r).getAllByRole("cell")[0].textContent)).toEqual([
      "1 minute",
      "10 minutes",
      "1 hour · your choice",
      "24 hours",
    ]);
    expect(rows[2]).toHaveAttribute("aria-current", "true");
    expect(rows[3]).toHaveTextContent("≥ 1,240");
    expect(rows[1]).not.toHaveTextContent("≥");
  });

  it("explains itself without internal names, and says nothing was read", async () => {
    vi.mocked(api.experiment).mockResolvedValue(repeats());
    renderPage();
    const method = (
      await screen.findByRole("heading", { name: "How this was worked out" })
    ).closest("section")!;
    expect(method).toHaveTextContent("No prompt or response was read");
    expect(method.textContent).not.toMatch(/duplicate_calls|ttl_seconds|hits_/);
  });

  it("compares the two cache lifetimes for prompt caching", async () => {
    vi.mocked(api.experiment).mockResolvedValue(
      repeats({
        lever: "prompt_caching",
        setting: { cache_ttl: "1h" },
        setting_label: "a 1-hour prompt cache",
        result: {
          kind: "caching",
          calls: 26000,
          cache_ttl: "1h",
          monthly_saving: 262.0,
          lifetimes: [
            { cache_ttl: "5m", label: "5 minutes", monthly_saving: 254.89, cache_writes: 700 },
            { cache_ttl: "1h", label: "1 hour", monthly_saving: 262.0, cache_writes: 120 },
          ],
        },
      }),
    );
    renderPage();
    expect(await screen.findByRole("heading", { name: "Test: Prompt caching" })).toBeVisible();
    const chosen = screen.getAllByRole("row").find((r) => r.getAttribute("aria-current"))!;
    expect(chosen).toHaveTextContent("1 hour · your choice");
    expect(chosen).toHaveTextContent("120");
    expect(chosen).toHaveTextContent("$262");
  });

  it("can be checked again or cancelled while it waits for data", async () => {
    vi.mocked(api.experiment).mockResolvedValue(
      repeats({
        status: "waiting_for_data",
        outcome: null,
        result: null,
        outcome_reason: "50 of the 200 calls needed have been counted this month.",
      }),
    );
    vi.mocked(api.cancelExperiment).mockResolvedValue(
      repeats({ status: "cancelled", outcome: null, result: null }),
    );
    renderPage();
    expect(await screen.findByText("Waiting for data")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Check again" }));
    await waitFor(() => expect(api.experiment).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole("button", { name: "Cancel the test" }));
    expect(await screen.findByText("Cancelled")).toBeVisible();
    expect(
      screen.getByRole("link", { name: /Test again with a different setting/ }),
    ).toHaveAttribute("href", "/features/f1/test/duplicate_calls");
  });

  it("lists earlier tests of the same recommendation", async () => {
    vi.mocked(api.experiment).mockResolvedValue(
      repeats({
        history: [
          repeats({
            id: "e0",
            outcome: "failed",
            setting_label: "answers reused for up to 1 minute",
            created_at: "2026-05-02T09:00:00Z",
          }),
        ],
      }),
    );
    renderPage();
    const heading = await screen.findByRole("heading", {
      name: "Earlier tests of this recommendation",
    });
    const list = heading.closest("section")!;
    expect(within(list).getByRole("link", { name: "May 2, 2026" })).toHaveAttribute(
      "href",
      "/experiments/e0",
    );
    expect(list).toHaveTextContent("Tested — did not hold");
  });

  it("says a test that is not there is not there", async () => {
    vi.mocked(api.experiment).mockRejectedValue(new ApiError(404, "Test not found"));
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent("This test does not exist");
  });
});

function offline(over: Partial<Experiment> = {}): Experiment {
  return repeats({
    lever: "model_rightsizing",
    mode: "offline",
    setting: {
      provider: "anthropic",
      control_model: "claude-sonnet-4-6",
      candidate_model: "claude-haiku-4-5",
      loss_margin: 0.1,
      min_cases: 20,
    },
    setting_label: "claude-haiku-4-5 in place of claude-sonnet-4-6",
    results_source: "runner",
    judge_model: "claude-sonnet-4-6",
    test_cost: 3.1,
    outcome_reason: "No broken answers. Better on 6, same on 30, worse on 4 of 40.",
    result: {
      kind: "offline",
      cases: 40,
      compared: 40,
      control_errors: 0,
      broken: 0,
      better: 6,
      same: 30,
      worse: 4,
      unjudged: 0,
      control: {
        model: "claude-sonnet-4-6",
        cost_per_call: 0.0117,
        latency_ms: { p50: 2400, p95: 2780 },
      },
      candidate: {
        model: "claude-haiku-4-5",
        cost_per_call: 0.0037,
        latency_ms: { p50: 1100, p95: 1290 },
      },
      saving_fraction: 0.68,
      control_monthly_spend: 1800,
      monthly_saving: 1224,
      rule: { loss_margin: 0.1, min_cases: 20, relaxed: false },
    },
    ...over,
  });
}

function renderWithState(state: unknown) {
  return render(
    <MemoryRouter initialEntries={[{ pathname: "/experiments/e1", state }]}>
      <Routes>
        <Route path="/experiments/:id" element={<ExperimentPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("An offline test", () => {
  beforeEach(() => vi.clearAllMocks());

  it("shows how to run it, with the token, only on the way in", async () => {
    vi.mocked(api.experiment).mockResolvedValue(
      offline({ status: "waiting_for_results", outcome: null, result: null, test_cost: null }),
    );
    renderWithState({ token: "mtx_secret" });
    expect(await screen.findByText("Waiting for results")).toBeVisible();
    expect(screen.getByText("mtx_secret", { selector: "code" })).toBeVisible();
    const commands = document.querySelector(".test-commands")!.textContent!;
    expect(commands).toContain(`export METER_URL=${window.location.origin}`);
    expect(commands).toContain("export METER_EXPERIMENT_TOKEN=mtx_secret");
    expect(commands).toContain("export ANTHROPIC_API_KEY=<your key>");
    expect(commands).toContain("meter-test run --cases cases.jsonl");
    expect(screen.getByRole("button", { name: "Cancel the test" })).toBeVisible();
  });

  it("never shows the token again once the page is reloaded", async () => {
    vi.mocked(api.experiment).mockResolvedValue(
      offline({ status: "waiting_for_results", outcome: null, result: null }),
    );
    renderWithState(null);
    expect(await screen.findByText(/If you no longer have it, start the test again/)).toBeVisible();
    expect(document.querySelector(".test-commands")!.textContent).toContain(
      "METER_EXPERIMENT_TOKEN=<the token>",
    );
  });

  it("shows what the run found, priced by Meter, and what it cost", async () => {
    vi.mocked(api.experiment).mockResolvedValue(offline());
    renderWithState(null);
    const outcome = await screen.findByRole("status");
    expect(outcome).toHaveTextContent("Tested");
    const found = screen.getByRole("heading", { name: "What the test found" }).closest("section")!;
    expect(found).toHaveTextContent("6 better");
    expect(found).toHaveTextContent("0 broken");
    const tested = within(found)
      .getAllByRole("row")
      .find((r) => r.getAttribute("aria-current"))!;
    expect(tested).toHaveTextContent("claude-haiku-4-5 · tested");
    expect(tested).toHaveTextContent("$0.0037");
    expect(found).toHaveTextContent("68% less per call");
    expect(found).toHaveTextContent("$1,224/mo, counted as tested");
    expect(found).toHaveTextContent("judged by claude-sonnet-4-6");
    expect(found).toHaveTextContent("already part of your provider bill");
    expect(found).not.toHaveTextContent("loosened");
  });

  it("says what decided the verdicts, and compares with the tested model's own part", async () => {
    vi.mocked(api.experiment).mockResolvedValue(
      offline({
        results_source: "promptfoo",
        judge_model: null,
        baseline: { monthly: 1180, savings_type: "modeled_ceiling", confidence: "med" },
      }),
    );
    renderWithState(null);
    const method = (
      await screen.findByRole("heading", { name: "How this was worked out" })
    ).closest("section")!;
    expect(method).toHaveTextContent("Your own promptfoo assertions decided each answer");
    expect(method).not.toHaveTextContent("A judge you chose");
    expect(screen.getByText(/Before this test Meter estimated/)).toHaveTextContent(
      "$1,180/mo for claude-sonnet-4-6, a ceiling.",
    );
  });

  it("names a loosened rule", async () => {
    const exp = offline();
    if (exp.result?.kind === "offline")
      exp.result.rule = { loss_margin: 0.25, min_cases: 10, relaxed: true };
    vi.mocked(api.experiment).mockResolvedValue(exp);
    renderWithState(null);
    expect(await screen.findByText(/This rule was loosened from Meter/)).toBeVisible();
  });
});

function live(over: Partial<Experiment> = {}): Experiment {
  const group = (model: string, calls: number, cost: number, err: number) => ({
    model,
    calls,
    errors: Math.round(calls * err),
    error_rate: err,
    successes: calls,
    cost_per_call: cost,
    latency_p95_ms: 1200,
    other_model_calls: 0,
    scores: 0,
    quality: null,
    first_call_at: "2026-10-01T10:00:00Z",
  });
  return repeats({
    lever: "model_rightsizing",
    mode: "live",
    status: "running",
    outcome: null,
    outcome_reason: "120 and 118 of the 500 calls each group needs, 1 of 7 days.",
    created_at: new Date(Date.now() - 86_400_000).toISOString(),
    setting: {
      provider: "anthropic",
      control_model: "claude-sonnet-4-6",
      candidate_model: "claude-haiku-4-5",
      traffic_share: 10,
      min_calls: 500,
      min_days: 7,
      max_error_increase: 0.01,
      max_latency_increase: 0.25,
      quality_margin: 0.05,
    },
    setting_label: "claude-haiku-4-5 in place of claude-sonnet-4-6 on 10% of live traffic",
    result: {
      kind: "live",
      groups: {
        control: group("claude-sonnet-4-6", 120, 0.0117, 0.008),
        candidate: group("claude-haiku-4-5", 118, 0.0037, 0.0085),
      },
      guardrails: [],
      saving_fraction: 0.68,
      control_monthly_spend: 1800,
      monthly_saving: 0,
      rule: {
        traffic_share: 10,
        min_calls: 500,
        min_days: 7,
        max_error_increase: 0.01,
        max_latency_increase: 0.25,
        quality_margin: 0.05,
        relaxed: false,
      },
      provisional: true,
    },
    ...over,
  });
}

describe("A live test", () => {
  beforeEach(() => vi.clearAllMocks());

  it("shows how to send it traffic, and its progress as provisional", async () => {
    vi.mocked(api.experiment).mockResolvedValue(live());
    renderWithState(null);
    expect(await screen.findByText("Running")).toBeVisible();
    const code = document.querySelector(".test-commands")!.textContent!;
    expect(code).toContain('experiment="e1", group="control"');
    expect(code).toContain('experiment="e1", group="candidate"');
    expect(code).toContain("meter.score(");
    expect(code).toContain("npm install costlyinfra-meter");
    const found = screen.getByRole("heading", { name: "So far" }).closest("section")!;
    expect(found).toHaveTextContent("Provisional: no verdict before 500 calls in each group");
    expect(within(found).getAllByRole("row")[2]).toHaveTextContent("118 / 500");
    expect(screen.getByRole("button", { name: "End the test" })).toBeVisible();
  });

  it("names a breached guardrail as an alert", async () => {
    const exp = live({ status: "completed", outcome: "failed" });
    if (exp.result?.kind === "live") {
      exp.result.guardrails = ["claude-haiku-4-5 failed 9.0% of calls against 0.8%."];
      exp.result.provisional = false;
    }
    vi.mocked(api.experiment).mockResolvedValue(exp);
    renderWithState(null);
    expect(await screen.findByRole("alert")).toHaveTextContent("failed 9.0% of calls");
    expect(screen.getByRole("heading", { name: "What the test found" })).toBeVisible();
    expect(document.querySelector(".test-commands")).toBeNull(); // no longer running
  });

  it("warns when calls tagged to a group ran a different model", async () => {
    const exp = live();
    if (exp.result?.kind === "live") exp.result.groups.candidate.other_model_calls = 12;
    vi.mocked(api.experiment).mockResolvedValue(exp);
    renderWithState(null);
    expect(
      await screen.findByText(/12 calls tagged candidate ran a different model/),
    ).toBeVisible();
  });
});
